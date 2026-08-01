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
        Loads persistent DCA session state (has_partial_cut, max_buys_offset) and displays diagnostics on bot start.
        """
        state = self.db_manager.get_dca_session_state(self.ticker)
        has_partial_cut = state.get("has_partial_cut", 0)
        max_buys_offset = state.get("max_buys_offset", 0)

        min_session_buys = int(self.config.get("min_session_buys", 6))
        min_sell_qty = float(self.config.get("min_sell_qty", 1.0))
        yield_target = float(self.config.get("yield_target", 0.10))
        base_max_buys = int(self.config.get("max_session_buys", 40))
        current_max_buys = base_max_buys + max_buys_offset

        logger.info(
            f"DCA Ticker [{self.ticker}] | Active Buys: {len(self.incomplete_orders)} | "
            f"Pending Buys: {len(self.pending_buy_orders)} | "
            f"Max Buys: {current_max_buys} (Base: {base_max_buys}, Offset: +{max_buys_offset}) | "
            f"Min Buys Limit: {min_session_buys} | Min Qty Limit: {min_sell_qty:.2f} | "
            f"Base Target Yield: {yield_target*100:.1f}% | Partial Cut Done: {bool(has_partial_cut)}"
        )
        for oid, order in self.incomplete_orders.items():
            logger.info(f"  DCA Holding: ID={oid}, Price={order.get('price')}, Qty={order.get('quantity')}")
        for oid, order in self.pending_buy_orders.items():
            logger.info(f"  DCA Pending Buy: ID={oid}, Price={order.get('price')}, Qty={order.get('quantity')}")

    def evaluate(self, current_price: float):
        # 1. Reconcile buy executions
        self._verify_buy_executions()

        # 2. Check profit target / partial cut / liquidation
        self._evaluate_profit_target(current_price)

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

        # 6. Enforce N session buy limit (current_max_buys)
        state = self.db_manager.get_dca_session_state(self.ticker)
        max_buys_offset = state.get("max_buys_offset", 0)
        base_max_buys = int(self.config.get("max_session_buys", 40))
        current_max_buys = base_max_buys + max_buys_offset
        current_buys = len(self.incomplete_orders)

        # Rule 3: Final count reached -> On time slot matched, liquidate entire session to close
        if current_buys >= current_max_buys:
            total_buy_qty = sum(float(order["quantity"]) for order in self.incomplete_orders.values())
            total_buy_cost = sum(float(order["quantity"]) * float(order["price"]) for order in self.incomplete_orders.values())
            
            accumulated_cut_qty = state.get("cut_quantity", 0.0)
            accumulated_cut_cost = state.get("cut_total_cost", 0.0)
            
            current_holding_qty = total_buy_qty - accumulated_cut_qty
            current_holding_cost = total_buy_cost - accumulated_cut_cost
            avg_buy_price = (current_holding_cost / current_holding_qty) if current_holding_qty > 0 else current_price

            logger.warning(
                f"★★ DCA [{self.ticker}] - Final max session purchases reached ({current_buys}/{current_max_buys}). "
                f"Time slot matched -> Liquidating session ({current_holding_qty:.4f} shares) to exit..."
            )
            self._liquidate_session(current_holding_qty, avg_buy_price)
            return

        # 7. Place new DCA buy order
        logger.info(f"DCA [{self.ticker}] - Time slot matched (KST {current_hour:02d}:{current_minute:02d}). Placing buy order ({current_buys + 1}/{current_max_buys})...")
        self._place_dca_buy(current_price)

    def _evaluate_profit_target(self, current_price: float):
        """
        Evaluates dynamic target yield, 1/4 partial cut, and session extension rules.
        """
        if not self.incomplete_orders:
            return

        # Calculate original total buy quantity and total cost
        total_buy_qty = 0.0
        total_buy_cost = 0.0
        for order in self.incomplete_orders.values():
            qty = float(order["quantity"])
            price = float(order["price"])
            total_buy_qty += qty
            total_buy_cost += qty * price

        if total_buy_qty == 0.0:
            return

        buy_count = len(self.incomplete_orders)

        # Load session DB state
        state = self.db_manager.get_dca_session_state(self.ticker)
        has_partial_cut = state.get("has_partial_cut", 0)
        max_buys_offset = state.get("max_buys_offset", 0)
        accumulated_cut_qty = state.get("cut_quantity", 0.0)
        accumulated_cut_cost = state.get("cut_total_cost", 0.0)

        # Deduct partial cuts to get actual current holdings and cost
        current_holding_qty = total_buy_qty - accumulated_cut_qty
        current_holding_cost = total_buy_cost - accumulated_cut_cost

        if current_holding_qty <= 0.0:
            return

        base_max_buys = int(self.config.get("max_session_buys", 40))
        current_max_buys = base_max_buys + max_buys_offset

        # Check minimum buy count threshold
        min_session_buys = int(self.config.get("min_session_buys", 6))
        if buy_count < min_session_buys:
            logger.info(
                f"DCA Profit Check [{self.ticker}] | Buy Count: {buy_count}/{min_session_buys} "
                f"(Below min_session_buys={min_session_buys}). Skipping profit check."
            )
            return

        # Check minimum sell quantity threshold
        min_sell_qty = float(self.config.get("min_sell_qty", 1.0))
        if current_holding_qty < min_sell_qty:
            logger.info(
                f"DCA Profit Check [{self.ticker}] | Holding Qty: {current_holding_qty:.6f}/{min_sell_qty:.6f} "
                f"(Below min_sell_qty={min_sell_qty}). Skipping profit check."
            )
            return

        average_buy_price = current_holding_cost / current_holding_qty
        current_yield = (current_price - average_buy_price) / average_buy_price
        base_target = float(self.config.get("yield_target", 0.10))

        # 1) Calculate Dynamic Target Yield
        half_buys = current_max_buys / 2.0
        if buy_count <= half_buys:
            effective_target_yield = base_target
        else:
            decay_factor = (current_max_buys + 10 - buy_count) / float(current_max_buys)
            effective_target_yield = base_target * decay_factor

        logger.info(
            f"DCA Evaluation [{self.ticker}] | Buy Count: {buy_count}/{current_max_buys} | "
            f"Holding Qty: {current_holding_qty:.4f} | Avg Buy: ${average_buy_price:.2f} | "
            f"Current Price: ${current_price:.2f} | Yield: {current_yield*100:.2f}% (Dynamic Target: {effective_target_yield*100:.2f}%) | "
            f"Partial Cut: {bool(has_partial_cut)}"
        )

        # 2) Check Profit Target -> Full Liquidation
        if current_yield >= effective_target_yield:
            logger.warning(
                f"★ DCA [{self.ticker}] - Dynamic Target Yield Met! "
                f"Current yield {current_yield*100:.2f}% >= Target {effective_target_yield*100:.2f}%. Liquidating session..."
            )
            self._liquidate_session(current_holding_qty, average_buy_price)
            return

        # 3) Check 30% Partial Cut & 20% Session Extension Rule
        three_quarter_buys = current_max_buys * 0.75
        if buy_count >= three_quarter_buys and current_yield < 0 and has_partial_cut == 0:
            # Sell 30% of current holding quantity (at least 1.0 share if current_holding_qty >= 1.0)
            cut_qty = current_holding_qty * 0.30
            if cut_qty < 1.0 and current_holding_qty >= 1.0:
                cut_qty = 1.0
            if cut_qty > current_holding_qty:
                cut_qty = current_holding_qty

            cut_cost = cut_qty * average_buy_price
            new_cut_quantity = accumulated_cut_qty + cut_qty
            new_cut_total_cost = accumulated_cut_cost + cut_cost

            # Extension: 20% (1/5) of base max_session_buys
            extension = int(base_max_buys * (1.0 / 5.0))
            new_offset = max_buys_offset + extension

            # Save state to DB
            self.db_manager.save_dca_session_state(
                self.ticker,
                is_trailing=0,
                peak_price=0.0,
                has_partial_cut=1,
                max_buys_offset=new_offset,
                cut_quantity=new_cut_quantity,
                cut_total_cost=new_cut_total_cost
            )

            logger.warning(
                f"★★ DCA [{self.ticker}] - 3/4 Progress ({buy_count}/{current_max_buys}) Negative Yield ({current_yield*100:.2f}%) Detected! "
                f"Executing 30% Partial Cut ({cut_qty:.4f} shares @ ${average_buy_price:.2f}) and extending max buys by +{extension} (New limit: {base_max_buys + new_offset})..."
            )
            self._execute_partial_cut(cut_qty, current_price, average_buy_price)

    def _execute_partial_cut(self, cut_qty: float, current_price: float, avg_buy_price: float):
        """
        Executes a 30% partial cut market sell order and records trade history while preserving original buy records.
        """
        try:
            buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
            if buy_mode == "AMOUNT":
                sell_res = self.api_client.place_market_order(self.ticker, "SELL", cut_qty)
            else:
                exec_qty = max(1, int(cut_qty))
                sell_res = self.api_client.place_market_order(self.ticker, "SELL", exec_qty)
                cut_qty = float(exec_qty)

            if sell_res and "orderId" in sell_res:
                oid = sell_res["orderId"]
                profit = (current_price - avg_buy_price) * cut_qty

                # Record trade history
                self.db_manager.add_dca_trade_history(
                    self.ticker,
                    cut_qty,
                    avg_buy_price,
                    current_price,
                    profit,
                    len(self.incomplete_orders),
                    oid
                )

                logger.warning(
                    f"★★★ DCA [{self.ticker}] - 30% Partial Cut Completed! "
                    f"Order ID: {oid} | Sold Qty: {cut_qty:.4f} @ ${current_price:.2f} | Profit: ${profit:.2f} | DB original order records preserved ({len(self.incomplete_orders)} buys)."
                )
        except Exception as e:
            logger.error(f"Failed to execute partial cut for DCA [{self.ticker}]: {e}")

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

            # Record in trade history and archive individual session buy orders
            profit = (sell_price - average_buy_price) * total_qty
            buy_count = len(self.incomplete_orders)
            session_id = f"SESS_{self.ticker}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            
            self.db_manager.add_dca_trade_history(
                self.ticker,
                total_qty,
                average_buy_price,
                sell_price,
                profit,
                buy_count,
                sell_order_id,
                session_id
            )

            # Archive individual buy records before clearing
            self.db_manager.archive_dca_session_buys(session_id, self.ticker, self.incomplete_orders)

            # Clear DB and memory states
            self.db_manager.clear_dca_incomplete_orders(self.ticker)
            self.db_manager.clear_dca_session_state(self.ticker)
            self.incomplete_orders.clear()
            
            logger.warning(
                f"★★★ DCA [{self.ticker}] - Liquidation Completed! Session ID: {session_id} | "
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
