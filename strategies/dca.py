import time
import logging
from datetime import datetime
import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger("TossTradeBot.Strategy.DCA")

class DcaStrategy(BaseStrategy):
    """
    DCA (Dollar-Cost Averaging) strategy inspired by Rao's Infinite Purchase Method.
    Accumulates shares at specific KST time slots up to N times per session.
    Triggers a Trailing Stop exit once the target yield is reached.
    """
    def initialize_state(self):
        """
        Loads the persistent trailing stop state and displays diagnostics on bot start.
        """
        state = self.db_manager.get_dca_session_state(self.ticker)
        is_trailing = state.get("is_trailing", 0)
        peak_price = state.get("peak_price", 0.0)

        logger.info(
            f"DCA Ticker [{self.ticker}] | Active Buys: {len(self.incomplete_orders)} | "
            f"Pending Buys: {len(self.pending_buy_orders)} | "
            f"Trailing Active: {bool(is_trailing)} | Peak Price: {peak_price:.2f}"
        )
        for oid, order in self.incomplete_orders.items():
            logger.info(f"  DCA Holding: ID={oid}, Price={order.get('price')}, Qty={order.get('quantity')}")
        for oid, order in self.pending_buy_orders.items():
            logger.info(f"  DCA Pending Buy: ID={oid}, Price={order.get('price')}, Qty={order.get('quantity')}")

    def evaluate(self, current_price: float):
        # 1. Reconcile buy executions
        self._verify_buy_executions()

        # 2. Check and process trailing stop status / liquidation
        self._evaluate_trailing_stop(current_price)

        # If the ticker is disabled, block any new buys
        if not self.config.get("enabled", True):
            logger.debug(f"DCA Ticker [{self.ticker}] is disabled. Skipping new buy checks.")
            return

        # 3. Check time slots for buying (in KST timezone)
        tz_kst = pytz.timezone("Asia/Seoul")
        now_kst = datetime.now(tz_kst)
        current_hour = now_kst.hour
        current_minute = now_kst.minute

        is_slot = False
        market = self.config.get("market", "US").upper()
        if market == "US":
            # US Market DCA buying slots: KST 23:00, 01:00, 03:00
            if (current_hour == 23 and 0 <= current_minute <= 2) or \
               (current_hour == 1 and 0 <= current_minute <= 2) or \
               (current_hour == 3 and 0 <= current_minute <= 2):
                is_slot = True
        else: # KR
            # KR Market DCA buying slots: KST 10:00, 12:30, 15:00
            if (current_hour == 10 and 0 <= current_minute <= 2) or \
               (current_hour == 12 and 30 <= current_minute <= 32) or \
               (current_hour == 15 and 0 <= current_minute <= 2):
                is_slot = True

        if not is_slot:
            return

        # 4. Prevent duplicate orders in the same 3-minute window
        has_recent_buy = False
        for order in list(self.pending_buy_orders.values()) + list(self.incomplete_orders.values()):
            ordered_at_str = order.get("orderedAt")
            if ordered_at_str:
                try:
                    ordered_at = datetime.fromisoformat(ordered_at_str)
                    # If an order was placed within the last 10 minutes, skip
                    if (datetime.now() - ordered_at).total_seconds() < 600:
                        has_recent_buy = True
                        break
                except Exception:
                    pass

        if has_recent_buy:
            return

        # 5. Skip buying if there is already an active pending buy order
        if self.pending_buy_orders:
            logger.info(f"DCA [{self.ticker}] - A buy order is already pending. Skipping new buy.")
            return

        # 6. Enforce N session buy limit (max_session_buys)
        max_buys = int(self.config.get("max_session_buys", 40))
        current_buys = len(self.incomplete_orders)
        if current_buys >= max_buys:
            logger.info(f"DCA [{self.ticker}] - Max session purchases reached ({current_buys}/{max_buys}). Skipping buy.")
            return

        # 7. Place new DCA buy order
        logger.info(f"DCA [{self.ticker}] - Time slot matched (KST {current_hour:02d}:{current_minute:02d}). Placing buy order...")
        self._place_dca_buy(current_price)

    def _evaluate_trailing_stop(self, current_price: float):
        """
        Calculates session average price, checks profit trigger, and updates trailing stop state.
        """
        if not self.incomplete_orders:
            return

        # Load session state from DB
        state = self.db_manager.get_dca_session_state(self.ticker)
        is_trailing = state.get("is_trailing", 0)
        peak_price = state.get("peak_price", 0.0)

        # Calculate average buy price and total quantity
        total_qty = 0.0
        total_cost = 0.0
        for order in self.incomplete_orders.values():
            qty = float(order["quantity"])
            price = float(order["price"])
            total_qty += qty
            total_cost += qty * price

        if total_qty == 0.0:
            return

        average_buy_price = total_cost / total_qty
        current_yield = (current_price - average_buy_price) / average_buy_price
        yield_target = float(self.config.get("yield_target", 0.10))
        trailing_drop_rate = float(self.config.get("trailing_drop_rate", 0.01))

        logger.info(
            f"DCA Trailing Check [{self.ticker}] | Avg Buy: {average_buy_price:.2f} | "
            f"Current Yield: {current_yield*100:.2f}% (Target: {yield_target*100:.2f}%) | "
            f"Trailing Active: {bool(is_trailing)} | Peak Price: {peak_price:.2f}"
        )

        if not is_trailing:
            # Check if target yield is met to activate trailing stop
            if current_yield >= yield_target:
                is_trailing = 1
                peak_price = current_price
                self.db_manager.save_dca_session_state(self.ticker, is_trailing, peak_price)
                logger.warning(
                    f"★ DCA [{self.ticker}] - Trailing Stop Activated! "
                    f"Target yield met ({current_yield*100:.2f}% >= {yield_target*100:.2f}%). "
                    f"Initial Peak Price set to: {peak_price:.2f}"
                )
        else:
            # Trailing stop is active. Update peak if current price is higher
            if current_price > peak_price:
                peak_price = current_price
                self.db_manager.save_dca_session_state(self.ticker, is_trailing, peak_price)
                logger.info(f"★ DCA [{self.ticker}] - Trailing Stop: Peak price updated to {peak_price:.2f}")

            # Check if price dropped from peak by trailing_drop_rate or more
            drop_price_threshold = peak_price * (1 - trailing_drop_rate)
            if current_price <= drop_price_threshold:
                logger.warning(
                    f"★★ DCA [{self.ticker}] - Trailing Stop Triggered! "
                    f"Current price {current_price:.2f} fell below drop threshold {drop_price_threshold:.2f} "
                    f"(-{trailing_drop_rate*100:.2f}% from peak {peak_price:.2f}). Liquidating..."
                )
                self._liquidate_session(total_qty, average_buy_price)

    def _liquidate_session(self, total_qty: float, average_buy_price: float):
        """
        Liquidates the entire DCA session holdings via a single market sell order.
        """
        try:
            buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
            if buy_mode == "AMOUNT":
                sell_res = self.api_client.place_market_order(self.ticker, "SELL", total_qty)
            else:
                sell_res = self.api_client.place_market_order(self.ticker, "SELL", int(total_qty))
                
            sell_order_id = sell_res["orderId"]
            logger.info(f"DCA [{self.ticker}] - Liquidation market sell order placed: {sell_order_id}")

            # Poll for immediate execution (up to 5 attempts, every 2s)
            filled = False
            sell_price = 0.0
            for attempt in range(5):
                time.sleep(2)
                try:
                    details = self.api_client.get_order_details(sell_order_id)
                    status = details.get("status")
                    logger.info(f"Polling Liquidation Sell [{self.ticker}] | Attempt {attempt + 1}/5 | Status: {status}")
                    
                    if status == "FILLED":
                        execution = details.get("execution", {})
                        avg_price_str = execution.get("averageFilledPrice")
                        sell_price = float(avg_price_str) if avg_price_str else (float(p) if (p := details.get("price")) is not None else 0.0)
                        filled = True
                        break
                except Exception as poll_err:
                    logger.error(f"Error checking order status in polling: {poll_err}")

            if not filled:
                logger.warning(f"DCA [{self.ticker}] - Liquidation sell status check timed out. Forcing DB matching as COMPLETED.")
                try:
                    details = self.api_client.get_order_details(sell_order_id)
                    execution = details.get("execution", {})
                    avg_price_str = execution.get("averageFilledPrice")
                    sell_price = float(avg_price_str) if avg_price_str else (float(p) if (p := details.get("price")) is not None else 0.0)
                except Exception:
                    pass

            if sell_price == 0.0:
                prices = self.api_client.get_current_prices([self.ticker])
                sell_price = prices.get(self.ticker, average_buy_price * (1 + float(self.config.get("yield_target", 0.10))))

            # Record in trade history
            profit = (sell_price - average_buy_price) * total_qty
            buy_count = len(self.incomplete_orders)
            
            self.db_manager.add_dca_trade_history(
                self.ticker,
                total_qty,
                average_buy_price,
                sell_price,
                profit,
                buy_count,
                sell_order_id
            )

            # Clear DB and memory states
            self.db_manager.clear_dca_incomplete_orders(self.ticker)
            self.db_manager.clear_dca_session_state(self.ticker)
            self.incomplete_orders.clear()
            
            logger.warning(
                f"★★★ DCA [{self.ticker}] - Liquidation Completed! "
                f"Liquidated Qty: {total_qty:.6f} | Avg Buy: {average_buy_price:.2f} | "
                f"Sell Price: {sell_price:.2f} | Profit: {profit:.2f} | Buy Count: {buy_count}"
            )
        except Exception as e:
            logger.error(f"Failed to liquidate DCA session for [{self.ticker}]: {e}")

    def _place_dca_buy(self, price: float):
        """
        Places a buy order, registers it under pending buy, and polls for 10 seconds for immediate execution.
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
            
            self.db_manager.add_dca_pending_buy_order(buy_order_id, buy_order_data)
            self.pending_buy_orders[buy_order_id] = buy_order_data
            
            logger.info(f"DCA Buy order placed for [{self.ticker}]: {buy_order_id}. Polling for immediate fill...")
            for attempt in range(5):
                time.sleep(2)
                try:
                    details = self.api_client.get_order_details(buy_order_id)
                    status = details.get("status")
                    logger.info(f"Polling DCA Buy [{self.ticker}] | Attempt {attempt + 1}/5 | Status: {status}")
                    
                    if status == "FILLED":
                        self._handle_filled_buy(buy_order_id, details)
                        break
                    elif status in ["CANCELED", "REJECTED"]:
                        execution = details.get("execution", {})
                        filled_qty_str = execution.get("filledQuantity", "0")
                        filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
                        
                        if filled_qty > 0:
                            logger.info(f"Polling: DCA Buy order cancelled with partial fill of {filled_qty} shares.")
                            partial_details = details.copy()
                            partial_details["quantity"] = str(filled_qty)
                            if "execution" in partial_details:
                                partial_details["execution"]["filledQuantity"] = str(filled_qty)
                            self._handle_filled_buy(buy_order_id, partial_details)
                        else:
                            logger.info(f"Polling: DCA Buy order closed with 0 execution. Removing from pending.")
                            self.db_manager.remove_dca_pending_buy_order(buy_order_id)
                            if buy_order_id in self.pending_buy_orders:
                                del self.pending_buy_orders[buy_order_id]
                        break
                except Exception as poll_err:
                    logger.error(f"Error checking order status in polling: {poll_err}")
                    
        except Exception as place_err:
            logger.error(f"Failed to place new DCA buy order for [{self.ticker}] at {price:.2f}: {place_err}")

    def _verify_buy_executions(self):
        """
        Reconciles pending DCA buy orders with exchange state.
        """
        if not self.pending_buy_orders:
            return

        logger.info(f"DCA Ticker [{self.ticker}] - Checking {len(self.pending_buy_orders)} pending buy orders...")
        pending_ids = list(self.pending_buy_orders.keys())
        
        for order_id in pending_ids:
            try:
                details = self.api_client.get_order_details(order_id)
                status = details.get("status")
                logger.info(f"  Checking DCA Buy Order {order_id} | Status: {status}")
                
                if status == "FILLED":
                    self._handle_filled_buy(order_id, details)
                    
                elif status in ["CANCELED", "REJECTED"]:
                    self._process_canceled_buy(order_id, details)
                    
                elif status in ["PENDING_CANCEL", "PENDING_REPLACE"]:
                    logger.info(f"  DCA Buy order {order_id} is in intermediate state {status}. Waiting...")
                    
                elif status in ["PENDING", "PARTIAL_FILLED"]:
                    logger.info(f"  DCA Buy order {order_id} is still pending with status {status}. Cancelling...")
                    try:
                        self.api_client.cancel_order(order_id)
                        new_details = self.api_client.get_order_details(order_id)
                        new_status = new_details.get("status")
                        logger.info(f"  Cancelled status check for {order_id}: {new_status}")
                        
                        if new_status in ["CANCELED", "REJECTED"]:
                            self._process_canceled_buy(order_id, new_details)
                        else:
                            logger.warning(
                                f"  Failed to confirm cancel for order {order_id} immediately (state: {new_status})."
                            )
                    except Exception as cancel_err:
                        logger.error(f"  Failed to cancel DCA buy order {order_id}: {cancel_err}")
                        
                        is_cancel_restricted = False
                        if hasattr(cancel_err, 'response') and cancel_err.response is not None:
                            try:
                                err_json = cancel_err.response.json()
                                if err_json.get("error", {}).get("code") == "cancel-restricted":
                                    is_cancel_restricted = True
                            except Exception:
                                pass
                                
                        if is_cancel_restricted:
                            logger.info(f"  DCA Buy order {order_id} cancel restricted. Fetching latest details...")
                            try:
                                check_details = self.api_client.get_order_details(order_id)
                                check_status = check_details.get("status")
                                execution = check_details.get("execution", {})
                                filled_qty = float(execution.get("filledQuantity", "0") or "0")
                                
                                if check_status == "FILLED" or filled_qty > 0:
                                    self._handle_filled_buy(order_id, check_details)
                                else:
                                    self._process_canceled_buy(order_id, check_details)
                            except Exception as check_err:
                                logger.error(f"  Failed to process details for DCA buy order {order_id} after restricted cancel: {check_err}")
                                
                else:
                    logger.warning(f"  DCA Buy order {order_id} has unexpected status: {status}. Doing nothing.")
                            
            except Exception as e:
                logger.error(f"Error verifying DCA pending buy order {order_id}: {e}")
                is_not_found = False
                if hasattr(e, 'response') and e.response is not None:
                    if e.response.status_code in [400, 404]:
                        is_not_found = True
                if is_not_found:
                    logger.warning(f"DCA Buy order {order_id} not found on exchange. Removing record from DB/memory.")
                    self.db_manager.remove_dca_pending_buy_order(order_id)
                    if order_id in self.pending_buy_orders:
                        del self.pending_buy_orders[order_id]

    def _process_canceled_buy(self, order_id: str, details: dict):
        execution = details.get("execution", {})
        filled_qty_str = execution.get("filledQuantity", "0")
        filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
        
        if filled_qty > 0:
            logger.info(
                f"DCA Buy order {order_id} was cancelled, but has partial fill of {filled_qty} shares. "
                f"Saving to incomplete orders..."
            )
            partial_details = details.copy()
            partial_details["quantity"] = str(filled_qty)
            if "execution" in partial_details:
                partial_details["execution"]["filledQuantity"] = str(filled_qty)
            self._handle_filled_buy(order_id, partial_details)
        else:
            logger.info(f"DCA Buy order {order_id} cancelled with 0 execution. Removing.")
            self.db_manager.remove_dca_pending_buy_order(order_id)
            if order_id in self.pending_buy_orders:
                del self.pending_buy_orders[order_id]

    def _handle_filled_buy(self, buy_order_id: str, details: dict):
        execution = details.get("execution", {})
        avg_price_str = execution.get("averageFilledPrice")
        buy_price = float(avg_price_str) if avg_price_str else float(details.get("price"))
        
        qty_str = execution.get("filledQuantity") or details.get("quantity")
        if not qty_str or float(qty_str) == 0.0:
            logger.warning(f"DCA Buy order {buy_order_id} filled but execution quantity is zero/null.")
            return
            
        qty = float(qty_str)
        market = self.config.get("market", "US")
        
        logger.info(f"$$$$ DCA BUY ORDER FILLED $$$$ | Ticker: {self.ticker} | ID: {buy_order_id} | Buy Price: {buy_price:.4f} | Qty: {qty}")
        
        buy_order_data = {
            "symbol": self.ticker,
            "price": self.api_client.format_price(buy_price, market),
            "quantity": str(qty),
            "orderedAt": datetime.now().isoformat()
        }
        self.db_manager.add_dca_incomplete_order(buy_order_id, buy_order_data)
        self.incomplete_orders[buy_order_id] = buy_order_data
        
        self.db_manager.remove_dca_pending_buy_order(buy_order_id)
        if buy_order_id in self.pending_buy_orders:
            del self.pending_buy_orders[buy_order_id]
