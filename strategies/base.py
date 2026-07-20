import logging
import time
from datetime import datetime, timedelta
from toss_api import TossAPIClient
from sqlite_manager import SQLiteManager

logger = logging.getLogger("TossTradeBot.Strategy")

class BaseStrategy:
    """
    Abstract base class for all trading strategies.
    Implements shared order reconciliation logic (verifying buys/sells)
    and Toss OpenAPI helper interfaces.
    """
    def __init__(self, ticker: str, api_client: TossAPIClient, db_manager: SQLiteManager, config: dict):
        self.ticker = ticker
        self.api_client = api_client
        self.db_manager = db_manager
        self.config = config
        
        # Memory caches (keyed by order_id)
        self.incomplete_orders = {}
        self.pending_buy_orders = {}
        
        # Cooldown state for consecutive buys
        self.cooldown_state = {"consecutive_buys": 0, "cooldown_until": None}

    def initialize_state(self):
        """
        Displays initial state diagnostics at startup.
        """
        logger.info(f"Ticker [{self.ticker}] | Active Sells: {len(self.incomplete_orders)} | Pending Buys: {len(self.pending_buy_orders)}")
        for oid, order in self.incomplete_orders.items():
            order_type = "Synthetic Sell" if order.get("isSynthetic") else "Limit Sell"
            logger.info(
                f"  Sell Order ({order_type}): ID={oid}, TargetPrice={order.get('price')}, "
                f"BuyPrice={order.get('buyPrice')}, Qty={order.get('quantity')}"
            )
        for oid, order in self.pending_buy_orders.items():
            logger.info(f"  Pending Buy: ID={oid}, Price={order.get('price')}, Qty={order.get('quantity')}")

    def evaluate(self, current_price: float):
        """
        Core logic evaluated on every scheduler tick. Must be overridden by subclasses.
        """
        raise NotImplementedError("Each strategy must implement the evaluate method.")

    def _is_in_cooldown(self) -> bool:
        """
        Checks if the ticker is currently locked under consecutive buy cooldown limit.
        """
        max_buys = self.config.get("max_consecutive_buys")
        cooldown_mins = self.config.get("cooldown_minutes")
        
        if max_buys is None or cooldown_mins is None:
            return False
            
        cooldown_until = self.cooldown_state["cooldown_until"]
        
        if cooldown_until:
            if datetime.now() < cooldown_until:
                logger.info(
                    f"Ticker [{self.ticker}] - Buy blocked. Consecutive buys: {self.cooldown_state['consecutive_buys']}/{max_buys}. "
                    f"Cooldown active until {cooldown_until.strftime('%H:%M:%S')}"
                )
                return True
            else:
                logger.info(f"Ticker [{self.ticker}] - Cooldown expired. Resetting consecutive buy counter to 0.")
                self.cooldown_state["consecutive_buys"] = 0
                self.cooldown_state["cooldown_until"] = None
                
        return False

    def _reset_consecutive_buys(self):
        """
        Resets consecutive buy counter upon a successful sell.
        """
        if self.cooldown_state["consecutive_buys"] > 0 or self.cooldown_state["cooldown_until"] is not None:
            logger.info(f"Ticker [{self.ticker}] - Successful sell execution detected. Resetting consecutive buys and lifting cooldown.")
            self.cooldown_state["consecutive_buys"] = 0
            self.cooldown_state["cooldown_until"] = None

    def _extract_filled_price(self, details: dict) -> float | None:
        """
        Safely extracts actual filled price from order details.
        """
        execution = details.get("execution", {})
        avg_price_str = execution.get("averageFilledPrice")
        if avg_price_str:
            try:
                return float(avg_price_str)
            except (ValueError, TypeError):
                pass
        price_val = details.get("price")
        if price_val is not None:
            try:
                return float(price_val)
            except (ValueError, TypeError):
                pass
        return None

    def _verify_sell_executions(self, current_price: float):
        """
        Reconciles incomplete sell orders with exchange state.
        """
        if not self.incomplete_orders:
            return

        market = self.config.get("market", "US")
        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
        sell_order_ids = list(self.incomplete_orders.keys())
        
        for order_id in sell_order_ids:
            order = self.incomplete_orders.get(order_id)
            if not order:
                continue
                
            exchange_order_id = order.get("exchangeOrderId", "")
            target_price = float(order["price"])
            qty = float(order["quantity"])
            
            if exchange_order_id:
                try:
                    details = self.api_client.get_order_details(exchange_order_id)
                    status = details.get("status")
                    logger.info(f"  Checking exchange sell order {exchange_order_id} | Status: {status}")
                    
                    if status == "FILLED":
                        actual_sell_price = self._extract_filled_price(details)
                        logger.info(f"$$$$ SELL ORDER FILLED $$$$ | Ticker: {self.ticker} | ID: {order_id} | Target Price: {target_price:.2f} | Actual: {actual_sell_price}")
                        self.db_manager.remove_incomplete_order(order_id, actual_sell_price)
                        self._reset_consecutive_buys()
                        if order_id in self.incomplete_orders:
                            del self.incomplete_orders[order_id]
                            
                    elif status in ["CANCELED", "REJECTED"]:
                        self._process_canceled_sell(order_id, order, details)
                        
                    elif status in ["PENDING_CANCEL", "PENDING_REPLACE"]:
                        logger.info(f"  Exchange sell order {exchange_order_id} is in intermediate state {status}. Waiting...")
                        
                    elif status in ["PENDING", "PARTIAL_FILLED"]:
                        logger.info(f"  Exchange sell order {exchange_order_id} is unfilled ({status}). Cancelling...")
                        try:
                            self.api_client.cancel_order(exchange_order_id)
                            new_details = self.api_client.get_order_details(exchange_order_id)
                            new_status = new_details.get("status")
                            
                            if new_status in ["CANCELED", "REJECTED"]:
                                self._process_canceled_sell(order_id, order, new_details)
                            else:
                                logger.warning(f"  Failed to confirm cancel for sell {exchange_order_id} immediately. State: {new_status}")
                        except Exception as cancel_err:
                            logger.error(f"  Failed to cancel exchange sell order {exchange_order_id}: {cancel_err}")
                            
                            is_cancel_restricted = False
                            if hasattr(cancel_err, 'response') and cancel_err.response is not None:
                                try:
                                    err_json = cancel_err.response.json()
                                    if err_json.get("error", {}).get("code") == "cancel-restricted":
                                        is_cancel_restricted = True
                                except Exception:
                                    pass
                                    
                            if is_cancel_restricted:
                                logger.info(f"  Sell order {exchange_order_id} cancel restricted. Fetching latest details...")
                                try:
                                    check_details = self.api_client.get_order_details(exchange_order_id)
                                    check_status = check_details.get("status")
                                    
                                    if check_status == "FILLED":
                                        actual_sell_price = self._extract_filled_price(check_details)
                                        logger.info(f"$$$$ SELL ORDER FILLED $$$$ | Ticker: {self.ticker} | ID: {order_id} | Target Price: {target_price:.2f} | Actual: {actual_sell_price}")
                                        self.db_manager.remove_incomplete_order(order_id, actual_sell_price)
                                        self._reset_consecutive_buys()
                                        if order_id in self.incomplete_orders:
                                            del self.incomplete_orders[order_id]
                                    else:
                                        logger.info(f"  Treating restricted cancel sell order {exchange_order_id} as CANCELED/inactive.")
                                        self._process_canceled_sell(order_id, order, check_details)
                                except Exception as check_err:
                                    logger.error(f"  Failed to process details for sell order {exchange_order_id} after restricted cancel: {check_err}")
                                    
                    else:
                        logger.warning(f"  Exchange sell order {exchange_order_id} has unexpected status: {status}. Doing nothing.")
                            
                except Exception as api_err:
                    logger.error(f"  Failed to verify status for exchange sell order {exchange_order_id}: {api_err}")
                    is_not_found = False
                    if hasattr(api_err, 'response') and api_err.response is not None:
                        if api_err.response.status_code in [400, 404]:
                            is_not_found = True
                    if is_not_found:
                        logger.warning(f"  Exchange sell order {exchange_order_id} not found on exchange. Resetting back to standby.")
                        self.db_manager.update_incomplete_order_exchange_id(order_id, "")
                        order["exchangeOrderId"] = ""
            
            else:
                if current_price >= target_price:
                    logger.info(
                        f"Target met for [{self.ticker}] sell! Current {current_price:.2f} >= Target {target_price:.2f}. "
                        f"Submitting real sell order..."
                    )
                    try:
                        if buy_mode == "AMOUNT":
                            sell_res = self.api_client.place_market_order(self.ticker, "SELL", qty)
                        else:
                            sell_res = self.api_client.place_limit_order(self.ticker, "SELL", int(qty), target_price)
                            
                        new_exchange_id = sell_res["orderId"]
                        logger.info(f"  Submitted real sell order to exchange. Exchange ID: {new_exchange_id}")
                        
                        self.db_manager.update_incomplete_order_exchange_id(order_id, new_exchange_id)
                        order["exchangeOrderId"] = new_exchange_id
                        
                        for attempt in range(3):
                            time.sleep(1.5)
                            details = self.api_client.get_order_details(new_exchange_id)
                            status = details.get("status")
                            logger.info(f"  Polling fresh sell [{self.ticker}] | Attempt {attempt + 1}/3 | Status: {status}")
                            if status == "FILLED":
                                actual_sell_price = self._extract_filled_price(details)
                                logger.info(f"$$$$ SELL ORDER FILLED IN POLLING $$$$ | Ticker: {self.ticker} | ID: {order_id} | Target Price: {target_price:.2f} | Actual: {actual_sell_price}")
                                self.db_manager.remove_incomplete_order(order_id, actual_sell_price)
                                self._reset_consecutive_buys()
                                if order_id in self.incomplete_orders:
                                    del self.incomplete_orders[order_id]
                                break
                            elif status in ["CANCELED", "REJECTED"]:
                                self._process_canceled_sell(order_id, order, details)
                                break
                                
                    except Exception as place_err:
                        logger.error(f"  Failed to submit real sell order for {order_id}: {place_err}")

    def _process_canceled_sell(self, order_id: str, order: dict, details: dict):
        """
        Handles canceled exchange sell orders (partially filled/fully canceled).
        """
        execution = details.get("execution", {})
        filled_qty_str = execution.get("filledQuantity", "0")
        filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
        
        if filled_qty > 0:
            logger.info(f"Sell order {order_id} cancel confirmed with partial execution of {filled_qty} shares.")
            total_qty = float(order["quantity"])
            remaining_qty = total_qty - filled_qty
            actual_sell_price = self._extract_filled_price(details)
            
            self.db_manager.update_incomplete_order_quantity(order_id, filled_qty)
            self.db_manager.remove_incomplete_order(order_id, actual_sell_price)
            self._reset_consecutive_buys()
            if order_id in self.incomplete_orders:
                del self.incomplete_orders[order_id]
                
            if remaining_qty > 0.000001:
                new_sell_id = f"synthetic_sell_rem_{int(time.time())}_{order_id}"
                buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
                if buy_mode == "AMOUNT":
                    qty_formatted = f"{remaining_qty:.6f}".rstrip('0').rstrip('.')
                else:
                    qty_formatted = str(int(remaining_qty))
                    
                new_sell_data = {
                    "orderId": new_sell_id,
                    "symbol": self.ticker,
                    "price": order["price"],
                    "quantity": qty_formatted,
                    "buyPrice": order["buyPrice"],
                    "orderedAt": order["orderedAt"],
                    "isSynthetic": True,
                    "exchangeOrderId": ""
                }
                
                self.db_manager.add_incomplete_order(new_sell_id, new_sell_data)
                self.incomplete_orders[new_sell_id] = new_sell_data
                logger.info(f"  Re-registered remaining {qty_formatted} shares as new synthetic sell {new_sell_id}")
        else:
            logger.info(f"Sell order {order_id} cancel confirmed with 0 execution. Resetting to standby.")
            self.db_manager.update_incomplete_order_exchange_id(order_id, "")
            order["exchangeOrderId"] = ""

    def _verify_buy_executions(self):
        """
        Reconciles pending buy orders with exchange state.
        """
        if not self.pending_buy_orders:
            return

        logger.info(f"Ticker [{self.ticker}] - Checking {len(self.pending_buy_orders)} pending buy orders...")
        pending_ids = list(self.pending_buy_orders.keys())
        
        for order_id in pending_ids:
            try:
                details = self.api_client.get_order_details(order_id)
                status = details.get("status")
                logger.info(f"  Checking Buy Order {order_id} | Status: {status}")
                
                if status == "FILLED":
                    self._handle_filled_buy(order_id, details)
                    
                elif status in ["CANCELED", "REJECTED"]:
                    self._process_canceled_buy(order_id, details)
                    
                elif status in ["PENDING_CANCEL", "PENDING_REPLACE"]:
                    logger.info(f"  Buy order {order_id} is in intermediate state {status}. Waiting...")
                    
                elif status in ["PENDING", "PARTIAL_FILLED"]:
                    logger.info(f"  Buy order {order_id} is still pending with status {status}. Cancelling to reload in next tick...")
                    try:
                        self.api_client.cancel_order(order_id)
                        new_details = self.api_client.get_order_details(order_id)
                        new_status = new_details.get("status")
                        logger.info(f"  Cancelled status check for {order_id}: {new_status}")
                        
                        if new_status in ["CANCELED", "REJECTED"]:
                            self._process_canceled_buy(order_id, new_details)
                        else:
                            logger.warning(
                                f"  Failed to confirm cancel for order {order_id} immediately (state: {new_status}). "
                                f"Will retry in next tick."
                            )
                    except Exception as cancel_err:
                        logger.error(f"  Failed to cancel buy order {order_id}: {cancel_err}")
                        
                        is_cancel_restricted = False
                        if hasattr(cancel_err, 'response') and cancel_err.response is not None:
                            try:
                                err_json = cancel_err.response.json()
                                if err_json.get("error", {}).get("code") == "cancel-restricted":
                                    is_cancel_restricted = True
                            except Exception:
                                pass
                                
                        if is_cancel_restricted:
                            logger.info(f"  Buy order {order_id} cancel restricted. Fetching latest details...")
                            try:
                                check_details = self.api_client.get_order_details(order_id)
                                check_status = check_details.get("status")
                                execution = check_details.get("execution", {})
                                filled_qty = float(execution.get("filledQuantity", "0") or "0")
                                
                                if check_status == "FILLED" or filled_qty > 0:
                                    logger.info(f"  Treating restricted cancel buy order {order_id} as FILLED/partially filled (qty: {filled_qty})")
                                    self._handle_filled_buy(order_id, check_details)
                                else:
                                    logger.info(f"  Treating restricted cancel buy order {order_id} as CANCELED/inactive.")
                                    self._process_canceled_buy(order_id, check_details)
                            except Exception as check_err:
                                logger.error(f"  Failed to process details for buy order {order_id} after restricted cancel: {check_err}")
                                
                else:
                    logger.warning(f"  Buy order {order_id} has unexpected status: {status}. Doing nothing.")
                            
            except Exception as e:
                logger.error(f"Error verifying pending buy order {order_id}: {e}")
                is_not_found = False
                if hasattr(e, 'response') and e.response is not None:
                    if e.response.status_code in [400, 404]:
                        is_not_found = True
                if is_not_found:
                    logger.warning(f"Buy order {order_id} not found on exchange (HTTP {e.response.status_code}). Removing stale record from DB/memory.")
                    self.db_manager.remove_pending_buy_order(order_id)
                    if order_id in self.pending_buy_orders:
                        del self.pending_buy_orders[order_id]

    def _process_canceled_buy(self, order_id: str, details: dict):
        """
        Cleans up canceled buy order, creating target sells if partial execution occurred.
        """
        execution = details.get("execution", {})
        filled_qty_str = execution.get("filledQuantity", "0")
        filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
        
        if filled_qty > 0:
            logger.info(
                f"Buy order {order_id} was cancelled, but has partial fill of {filled_qty} shares. "
                f"Placing target sell order..."
            )
            partial_details = details.copy()
            partial_details["quantity"] = str(filled_qty)
            if "execution" in partial_details:
                partial_details["execution"]["filledQuantity"] = str(filled_qty)
            self._handle_filled_buy(order_id, partial_details)
        else:
            logger.info(f"Buy order {order_id} cancelled with 0 execution. Removing.")
            self.db_manager.remove_pending_buy_order(order_id)
            if order_id in self.pending_buy_orders:
                del self.pending_buy_orders[order_id]

    def _handle_filled_buy(self, buy_order_id: str, details: dict):
        """
        Registers a synthetic standby sell order in DB on filled buy.
        """
        execution = details.get("execution", {})
        avg_price_str = execution.get("averageFilledPrice")
        buy_price = float(avg_price_str) if avg_price_str else float(details.get("price"))
        
        qty_str = execution.get("filledQuantity") or details.get("quantity")
        if not qty_str or float(qty_str) == 0.0:
            logger.warning(f"Buy order {buy_order_id} filled but execution quantity is zero/null.")
            return
            
        qty = float(qty_str)
        
        market = self.config.get("market", "US")
        yield_target = self.config.get("yield_target", 0.02)
        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
        
        sell_price = buy_price * (1 + yield_target)
        
        logger.info(f"$$$$ BUY ORDER FILLED $$$$ | Ticker: {self.ticker} | ID: {buy_order_id} | Buy Price: {buy_price:.4f} | Qty: {qty}")
        
        sell_order_id = f"synthetic_sell_{buy_order_id}"
        
        if buy_mode == "AMOUNT":
            qty_formatted = f"{qty:.6f}".rstrip('0').rstrip('.')
        else:
            qty_formatted = str(int(qty))
            
        sell_order_data = {
            "orderId": sell_order_id,
            "symbol": self.ticker,
            "price": self.api_client.format_price(sell_price, market),
            "quantity": qty_formatted,
            "buyPrice": self.api_client.format_price(buy_price, market),
            "orderedAt": datetime.now().isoformat(),
            "isSynthetic": True,
            "exchangeOrderId": ""
        }
        
        self.db_manager.add_incomplete_order(sell_order_id, sell_order_data)
        self.incomplete_orders[sell_order_id] = sell_order_data
        
        self.db_manager.remove_pending_buy_order(buy_order_id)
        if buy_order_id in self.pending_buy_orders:
            del self.pending_buy_orders[buy_order_id]
            
        logger.info(f"Registered synthetic target sell order in DB: {sell_order_id} at {sell_price:.2f} for {qty_formatted} shares.")
        
        max_buys = self.config.get("max_consecutive_buys")
        cooldown_mins = self.config.get("cooldown_minutes")
        if max_buys is not None and cooldown_mins is not None:
            self.cooldown_state["consecutive_buys"] += 1
            logger.info(f"Ticker [{self.ticker}] - Consecutive buy count increased: {self.cooldown_state['consecutive_buys']}/{max_buys}")
            
            if self.cooldown_state["consecutive_buys"] >= int(max_buys):
                cooldown_until = datetime.now() + timedelta(minutes=int(cooldown_mins))
                self.cooldown_state["cooldown_until"] = cooldown_until
                logger.warning(
                    f"Ticker [{self.ticker}] - Reached maximum consecutive buys ({max_buys}). "
                    f"Cooldown activated. Buying suspended until {cooldown_until.strftime('%Y-%m-%d %H:%M:%S')}."
                )

    def _place_grid_buy(self, price: float):
        """
        Places a buy order (quantity or amount based), records it in DB, and polls immediately.
        """
        try:
            market = self.config.get("market", "US")
            buy_mode = self.config.get("buy_mode", "AMOUNT")
            buy_qty = self.config.get("buy_qty", 1)
            buy_amount = self.config.get("buy_amount", 10.0)
            
            if buy_mode == "AMOUNT":
                buy_res = self.api_client.place_amount_market_order(self.ticker, "BUY", buy_amount)
                buy_order_id = buy_res["orderId"]
                buy_order_data = {
                    "orderId": buy_order_id,
                    "symbol": self.ticker,
                    "quantity": "0",
                    "price": self.api_client.format_price(price, market),
                    "orderedAt": datetime.now().isoformat(),
                    "isAmountBased": True,
                    "orderAmount": str(buy_amount)
                }
            else:
                buy_res = self.api_client.place_limit_order(self.ticker, "BUY", buy_qty, price)
                buy_order_id = buy_res["orderId"]
                buy_order_data = {
                    "orderId": buy_order_id,
                    "symbol": self.ticker,
                    "quantity": str(buy_qty),
                    "price": self.api_client.format_price(price, market),
                    "orderedAt": datetime.now().isoformat()
                }
            
            self.db_manager.add_pending_buy_order(buy_order_id, buy_order_data)
            self.pending_buy_orders[buy_order_id] = buy_order_data
            
            logger.info(f"Buy order placed for [{self.ticker}]: {buy_order_id}. Polling for immediate fill...")
            for attempt in range(5):
                time.sleep(2)
                try:
                    details = self.api_client.get_order_details(buy_order_id)
                    status = details.get("status")
                    logger.info(f"Polling Buy [{self.ticker}] | Attempt {attempt + 1}/5 | Status: {status}")
                    
                    if status == "FILLED":
                        self._handle_filled_buy(buy_order_id, details)
                        break
                    elif status in ["CANCELED", "REJECTED"]:
                        execution = details.get("execution", {})
                        filled_qty_str = execution.get("filledQuantity", "0")
                        filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
                        
                        if filled_qty > 0:
                            logger.info(f"Polling: Buy order cancelled with partial fill of {filled_qty} shares.")
                            partial_details = details.copy()
                            partial_details["quantity"] = str(filled_qty)
                            if "execution" in partial_details:
                                partial_details["execution"]["filledQuantity"] = str(filled_qty)
                            self._handle_filled_buy(buy_order_id, partial_details)
                        else:
                            logger.info(f"Polling: Buy order closed with 0 execution. Removing from pending.")
                            self.db_manager.remove_pending_buy_order(buy_order_id)
                            if buy_order_id in self.pending_buy_orders:
                                del self.pending_buy_orders[buy_order_id]
                        break
                except Exception as poll_err:
                    logger.error(f"Error checking order status in polling: {poll_err}")
                    
        except Exception as place_err:
            logger.error(f"Failed to place new grid buy order for [{self.ticker}] at {price:.2f}: {place_err}")
