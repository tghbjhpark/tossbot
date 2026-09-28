import logging
import time
from datetime import datetime
import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger("TossTradeBot.Strategy.TVR")

class TvrStrategy(BaseStrategy):
    """
    TvrStrategy: Target Value Rebalancing (TVR) Strategy without Pocket cash pool.
    
    Key Rules:
    - Rebalances every `cycle` (in days) towards a designated `target_value`.
    - No pocket cash pool: orders directly adjust holdings to meet target_value.
    - Directly queries current holdings quantity from Toss OpenAPI (/api/v1/holdings).
    - Checks rebalancing condition starting 20 minutes after market open:
      * US Market: 09:50 AM EST (Regular open 09:30 + 20m)
      * KR Market: 09:20 AM KST (Regular open 09:00 + 20m)
    - Compares current valuation E = current_qty * current_price with Target Value T:
      * If E > T * (1 + band): Overvalued -> SELL excess (E - T)
      * If E < T * (1 - band): Undervalued -> BUY deficit (T - E)
      * If within band: No rebalancing trade needed
    """

    def initialize_state(self):
        """
        Loads persistent TVR session state (cycle_count, last_cycle_date, last_rebalance_date)
        from SQLite database.
        """
        self.symbol = self.ticker
        state = self.db_manager.get_tvr_session_state(self.symbol)

        if state:
            self.cycle_count = int(state.get("cycle_count", 1))
            self.last_cycle_date = state.get("last_cycle_date")
            self.last_rebalance_date = state.get("last_rebalance_date")
        else:
            self.cycle_count = 1
            self.last_cycle_date = None
            self.last_rebalance_date = None
            self._save_session_state()

        target_val = self.get_target_value()
        cycle = self.get_cycle()
        band = self.get_band()
        delay_mins = int(self.config.get("rebalance_delay_minutes", 20))
        min_trade = float(self.config.get("min_trade_amount", 10.0))
        buy_mode = self.config.get("buy_mode", "AMOUNT" if self.config.get("market", "US").upper() == "US" else "QTY").upper()

        logger.info(
            f"TVR Ticker [{self.ticker}] | Target Value: ${target_val:.2f} | "
            f"Cycle: {cycle} days (Count #{self.cycle_count}) | Band: {band * 100:.1f}% | "
            f"Buy Mode: {buy_mode} | Min Trade: ${min_trade:.2f} | "
            f"Rebalance Delay: {delay_mins}m after open | "
            f"Last Cycle: {self.last_cycle_date} | Last Rebalance: {self.last_rebalance_date} | "
            f"Pending Buys: {len(self.pending_buy_orders)}"
        )

    def get_target_value(self) -> float:
        """
        Returns target value configured for this ticker.
        Supports both 'target_value' and 'v_target' keys.
        """
        tv = self.config.get("target_value")
        if tv is not None:
            return float(tv)
        vt = self.config.get("v_target")
        if vt is not None:
            return float(vt)
        return 1000.0

    def get_cycle(self) -> int:
        """
        Returns rebalancing cycle in days.
        Supports both 'cycle' and 'cycle_days' keys.
        """
        c = self.config.get("cycle")
        if c is not None:
            return int(c)
        cd = self.config.get("cycle_days")
        if cd is not None:
            return int(cd)
        return 10

    def get_band(self) -> float:
        """
        Returns band threshold as a float ratio (e.g. 0.10 for 10%).
        Supports both 'band' and 'band_rate' keys, normalizing percentages > 1.0.
        """
        b = self.config.get("band")
        if b is None:
            b = self.config.get("band_rate", 0.10)
        val = float(b)
        if val > 1.0:
            val = val / 100.0
        return val

    def _save_session_state(self):
        """
        Persists current TVR cycle state into SQLite.
        """
        self.db_manager.save_tvr_session_state(
            symbol=self.symbol,
            cycle_count=self.cycle_count,
            last_cycle_date=self.last_cycle_date,
            last_rebalance_date=self.last_rebalance_date
        )

    def is_active(self) -> bool:
        """
        Determines if TVR needs market data and evaluation in the current scheduler tick:
        1. If there are pending buy orders, active during trading hours for reconciliation.
        2. If in execution window (>= 20m post-market open), not yet evaluated today, and cycle due.
        """
        # If disabled and no pending orders, remain idle
        if not self.config.get("enabled", True):
            if self.pending_buy_orders:
                return self.is_regular_market_hours()
            return False

        # Reconcile pending orders if any
        if self.pending_buy_orders:
            return self.is_regular_market_hours()

        # Check execution window (20m after market open)
        in_window, today_str = self._is_execution_time()
        if not in_window:
            return False

        # If already evaluated today, skip
        if self.last_rebalance_date == today_str:
            return False

        # Check if cycle days have passed
        if not self._is_cycle_due(today_str):
            return False

        return True

    def _is_execution_time(self) -> tuple[bool, str]:
        """
        Checks if current time is within the trading window starting 20 minutes after market open.
        - US: Market opens at 09:30 EST. Start at 09:50 EST (09:30 + delay_mins).
        - KR: Market opens at 09:00 KST. Start at 09:20 KST (09:00 + delay_mins).
        """
        market = self.config.get("market", "US").upper()
        delay_mins = int(self.config.get("rebalance_delay_minutes", 20))
        buy_mode = self.config.get("buy_mode", "AMOUNT" if market == "US" else "QTY").upper()

        if market == "KR":
            tz_kr = pytz.timezone("Asia/Seoul")
            now = datetime.now(tz_kr)
            today_str = now.strftime("%Y-%m-%d")
            if now.weekday() >= 5:
                return False, today_str
            open_hour, open_min = 9, 0
            start_min = open_min + delay_mins
            start_hour = open_hour + (start_min // 60)
            start_min = start_min % 60
            start_time = now.replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
            close_hour, close_min = (14, 20) if buy_mode == "AMOUNT" else (15, 20)
            end_time = now.replace(hour=close_hour, minute=close_min, second=0, microsecond=0)
        else:
            tz_us = pytz.timezone("America/New_York")
            now = datetime.now(tz_us)
            today_str = now.strftime("%Y-%m-%d")
            if now.weekday() >= 5:
                return False, today_str
            open_hour, open_min = 9, 30
            start_min = open_min + delay_mins
            start_hour = open_hour + (start_min // 60)
            start_min = start_min % 60
            start_time = now.replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
            close_hour, close_min = (15, 0) if buy_mode == "AMOUNT" else (16, 0)
            end_time = now.replace(hour=close_hour, minute=close_min, second=0, microsecond=0)

        in_window = start_time <= now <= end_time
        return in_window, today_str

    def _is_cycle_due(self, today_str: str) -> bool:
        """
        Checks if the cycle interval in days has passed since last_cycle_date.
        Always returns True if last_cycle_date is not set (first run).
        """
        if not self.last_cycle_date:
            return True

        cycle_days = self.get_cycle()
        try:
            last_dt = datetime.strptime(self.last_cycle_date, "%Y-%m-%d")
            curr_dt = datetime.strptime(today_str, "%Y-%m-%d")
            days_diff = (curr_dt - last_dt).days
            return days_diff >= cycle_days
        except Exception as e:
            logger.error(f"Error checking cycle due dates ({self.last_cycle_date} vs {today_str}): {e}")
            return True

    def evaluate(self, current_price: float):
        """
        Core logic evaluated on scheduler ticks.
        """
        # 1. Reconcile any in-flight pending buy orders
        self._verify_pending_buys()

        if not self.config.get("enabled", True):
            logger.debug(f"TVR [{self.ticker}] is disabled. Skipping evaluation.")
            return

        # 2. Check trading hours
        market = self.config.get("market", "US").upper()
        buy_mode = self.config.get("buy_mode", "AMOUNT" if market == "US" else "QTY").upper()
        if buy_mode == "AMOUNT":
            if not self.is_fractional_trading_hours():
                return
        else:
            if not self.is_regular_market_hours():
                return

        # 3. Check execution time window (20m post open)
        in_window, today_str = self._is_execution_time()
        if not in_window:
            logger.debug(f"TVR [{self.ticker}] outside 20m-post-open execution window.")
            return

        # 4. Skip if already rebalanced today
        if self.last_rebalance_date == today_str:
            logger.debug(f"TVR [{self.ticker}] already completed rebalancing for today ({today_str}).")
            return

        # 5. Check if cycle is due
        if not self._is_cycle_due(today_str):
            logger.debug(f"TVR [{self.ticker}] cycle ({self.get_cycle()}d) not yet due since {self.last_cycle_date}.")
            return

        # 6. Prevent duplicate orders if a buy is already pending
        if self.pending_buy_orders:
            logger.info(f"TVR [{self.ticker}] - A buy order is pending. Waiting for fill before rebalance.")
            return

        # 7. Fetch current stock holding quantity directly from Toss OpenAPI
        try:
            current_qty = self.api_client.get_holding_quantity(self.ticker)
        except Exception as e:
            logger.error(f"TVR [{self.ticker}] Failed to fetch current holdings from Toss OpenAPI: {e}. Aborting.")
            return

        # 8. Execute rebalancing logic
        self._perform_rebalance(current_price, current_qty, today_str)

    def _perform_rebalance(self, current_price: float, current_qty: float, today_str: str):
        """
        Compares valuation with target_value and band, and executes buy or sell.
        """
        target_val = self.get_target_value()
        band = self.get_band()
        min_trade = float(self.config.get("min_trade_amount", 10.0))
        min_sell_qty = float(self.config.get("min_sell_qty", 0.0001))

        valuation = current_qty * current_price
        v_max = target_val * (1.0 + band)
        v_min = target_val * (1.0 - band)

        logger.info(
            f"TVR [{self.ticker}] Rebalance Check: Holdings={current_qty:.4f} @ ${current_price:.2f} | "
            f"Valuation E=${valuation:.2f} | Target=${target_val:.2f} | Band=[${v_min:.2f} ~ ${v_max:.2f}] (±{band * 100:.1f}%)"
        )

        if valuation > v_max:
            # Overvalued: Sell excess to bring valuation down to target_val
            excess_amount = valuation - target_val
            if excess_amount >= min_trade and current_qty > 0:
                sell_qty = excess_amount / current_price
                if sell_qty > current_qty:
                    sell_qty = current_qty

                if sell_qty >= min_sell_qty:
                    logger.info(
                        f"TVR [{self.ticker}] - Overvaluation detected (E=${valuation:.2f} > V_max=${v_max:.2f}). "
                        f"Selling ${excess_amount:.2f} ({sell_qty:.4f} shares) to restore target value ${target_val:.2f}..."
                    )
                    self._execute_tvr_sell(current_price, sell_qty, excess_amount)
                else:
                    logger.info(f"TVR [{self.ticker}] - Calculated sell qty ({sell_qty:.4f}) below min ({min_sell_qty}). Skipping trade.")
            else:
                logger.info(f"TVR [{self.ticker}] - Excess amount ${excess_amount:.2f} below min trade ${min_trade:.2f}. Skipping trade.")

        elif valuation < v_min:
            # Undervalued: Buy deficit to bring valuation up to target_val
            deficit_amount = target_val - valuation
            if deficit_amount >= min_trade:
                logger.info(
                    f"TVR [{self.ticker}] - Undervaluation detected (E=${valuation:.2f} < V_min=${v_min:.2f}). "
                    f"Buying ${deficit_amount:.2f} to restore target value ${target_val:.2f}..."
                )
                self._execute_tvr_buy(current_price, deficit_amount)
            else:
                logger.info(f"TVR [{self.ticker}] - Deficit amount ${deficit_amount:.2f} below min trade ${min_trade:.2f}. Skipping trade.")

        else:
            # Within band: No rebalance trade required
            logger.info(f"TVR [{self.ticker}] - Valuation E=${valuation:.2f} is within normal band [${v_min:.2f} ~ ${v_max:.2f}]. No trade needed.")

        # Mark cycle and today's rebalance completed
        self.last_cycle_date = today_str
        self.last_rebalance_date = today_str
        self.cycle_count += 1
        self._save_session_state()
        logger.info(f"TVR [{self.ticker}] - Advanced to Cycle #{self.cycle_count}. Next rebalance in {self.get_cycle()} days.")

    def _execute_tvr_buy(self, current_price: float, buy_amount: float):
        """
        Executes a rebalance BUY order (AMOUNT or QTY based).
        """
        market = self.config.get("market", "US").upper()
        buy_mode = self.config.get("buy_mode", "AMOUNT" if market == "US" else "QTY").upper()

        if buy_mode == "QTY":
            qty = max(1, int(round(buy_amount / current_price)))
            order_amount = qty * current_price
            logger.info(f"TVR [{self.ticker}] Placing QTY Buy: {qty} shares @ ${current_price:.2f} (Total: ${order_amount:.2f})")
            res = self.api_client.place_limit_order(self.ticker, "BUY", qty, current_price)
            if res and "orderId" in res:
                oid = res["orderId"]
                order_data = {
                    "orderId": oid,
                    "symbol": self.ticker,
                    "quantity": qty,
                    "price": current_price,
                    "orderedAt": datetime.now().isoformat(),
                    "isAmountBased": False,
                    "orderAmount": order_amount
                }
                self.pending_buy_orders[oid] = order_data
                self.db_manager.add_tvr_pending_buy_order(oid, self.ticker, qty, current_price, is_amount_based=False, order_amount=order_amount)
                self._poll_order_fill(oid, "BUY", qty, current_price, order_amount)
        else:
            # AMOUNT based
            est_qty = buy_amount / current_price
            logger.info(f"TVR [{self.ticker}] Placing AMOUNT Buy: ${buy_amount:.2f} (Est Qty: {est_qty:.4f})")
            res = self.api_client.place_amount_market_order(self.ticker, "BUY", buy_amount)
            if res and "orderId" in res:
                oid = res["orderId"]
                order_data = {
                    "orderId": oid,
                    "symbol": self.ticker,
                    "quantity": est_qty,
                    "price": current_price,
                    "orderedAt": datetime.now().isoformat(),
                    "isAmountBased": True,
                    "orderAmount": buy_amount
                }
                self.pending_buy_orders[oid] = order_data
                self.db_manager.add_tvr_pending_buy_order(oid, self.ticker, est_qty, current_price, is_amount_based=True, order_amount=buy_amount)
                self._poll_order_fill(oid, "BUY", est_qty, current_price, buy_amount)

    def _execute_tvr_sell(self, current_price: float, sell_qty: float, approx_sell_amount: float):
        """
        Executes a rebalance SELL order.
        """
        market = self.config.get("market", "US").upper()
        buy_mode = self.config.get("buy_mode", "AMOUNT" if market == "US" else "QTY").upper()

        if buy_mode == "QTY":
            exec_qty = float(int(sell_qty))
            if exec_qty < 1.0:
                logger.info(f"TVR [{self.ticker}] QTY Sell shares ({exec_qty}) < 1. Skipping.")
                return
            logger.info(f"TVR [{self.ticker}] Placing QTY Sell: {int(exec_qty)} shares @ ${current_price:.2f}")
            res = self.api_client.place_limit_order(self.ticker, "SELL", int(exec_qty), current_price)
        else:
            exec_qty = sell_qty
            logger.info(f"TVR [{self.ticker}] Placing MARKET Sell: {exec_qty:.4f} shares (approx ${approx_sell_amount:.2f})")
            res = self.api_client.place_market_order(self.ticker, "SELL", exec_qty)

        if res and "orderId" in res:
            oid = res["orderId"]
            self._poll_order_fill(oid, "SELL", exec_qty, current_price, approx_sell_amount)

    def _poll_order_fill(self, order_id: str, side: str, expected_qty: float, expected_price: float, expected_amount: float):
        """
        Polls order status immediately after submission to capture fills quickly.
        """
        for attempt in range(5):
            time.sleep(1.5)
            try:
                details = self.api_client.get_order_details(order_id)
                status = details.get("status", "").upper()
                logger.info(f"TVR [{self.ticker}] Polling {side} order {order_id} | Attempt {attempt + 1}/5 | Status: {status}")

                if status in ["FILLED", "SUCCESS", "COMPLETED"]:
                    execution = details.get("execution", {})
                    filled_qty = float(execution.get("filledQuantity") or details.get("executedQuantity") or expected_qty)
                    avg_price_str = execution.get("averageFilledPrice") or details.get("executedPrice")
                    filled_price = float(avg_price_str) if avg_price_str else expected_price
                    total_amt = filled_qty * filled_price

                    logger.info(f"$$$$ TVR {side} ORDER FILLED $$$$ | Ticker: {self.ticker} | ID: {order_id} | Qty: {filled_qty:.4f} @ ${filled_price:.2f}")
                    self.db_manager.add_tvr_trade_history(order_id, self.ticker, side, filled_qty, filled_price, total_amt, "FILLED")

                    if side == "BUY":
                        self.db_manager.remove_tvr_pending_buy_order(order_id)
                        if order_id in self.pending_buy_orders:
                            del self.pending_buy_orders[order_id]
                    break

                elif status in ["CANCELED", "REJECTED", "FAILED"]:
                    logger.warning(f"TVR [{self.ticker}] {side} order {order_id} {status}.")
                    if side == "BUY":
                        self.db_manager.remove_tvr_pending_buy_order(order_id)
                        if order_id in self.pending_buy_orders:
                            del self.pending_buy_orders[order_id]
                    break

            except Exception as e:
                logger.error(f"Error polling order details for {order_id}: {e}")

    def _verify_pending_buys(self):
        """
        Checks outstanding pending buy orders against Toss OpenAPI and resolves them.
        """
        if not self.pending_buy_orders:
            return

        for oid in list(self.pending_buy_orders.keys()):
            try:
                details = self.api_client.get_order_details(oid)
                status = details.get("status", "").upper()
                if status in ["FILLED", "SUCCESS", "COMPLETED"]:
                    execution = details.get("execution", {})
                    filled_qty = float(execution.get("filledQuantity") or details.get("executedQuantity") or 0.0)
                    avg_price_str = execution.get("averageFilledPrice") or details.get("executedPrice")
                    filled_price = float(avg_price_str) if avg_price_str else float(details.get("price", 0.0))
                    total_amt = filled_qty * filled_price

                    logger.info(f"$$$$ TVR PENDING BUY FILLED $$$$ | Ticker: {self.ticker} | ID: {oid} | Qty: {filled_qty:.4f} @ ${filled_price:.2f}")
                    self.db_manager.add_tvr_trade_history(oid, self.ticker, "BUY", filled_qty, filled_price, total_amt, "FILLED")
                    self.db_manager.remove_tvr_pending_buy_order(oid)
                    del self.pending_buy_orders[oid]

                elif status in ["CANCELED", "REJECTED", "FAILED"]:
                    logger.info(f"TVR Pending buy {oid} resolved as {status}. Removing.")
                    self.db_manager.remove_tvr_pending_buy_order(oid)
                    del self.pending_buy_orders[oid]

            except Exception as e:
                logger.error(f"Error verifying pending buy {oid}: {e}")
