import logging
import time
from datetime import datetime, timedelta
import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger("TossTradeBot.Strategy.VR")

class VrStrategy(BaseStrategy):
    """
    VrStrategy: Implementation of Laoor's Value Rebalancing (VR) Investment Strategy.
    
    Key Features:
    - Maintains target baseline portfolio valuation (V) and cash pool (pocket_cash).
    - Supports ACCUMULATE (적립식), LUMP_SUM (거치식), and WITHDRAWAL (인출식) modes.
    - Evaluates rebalancing ONCE DAILY at US Eastern Time 11:00 AM (rebalance_hour_us).
    - Enforces strict Pocket Cash limit during buy rebalancing.
    - Filters tiny trades below min_trade_amount ($10) to minimize fees.
    - Updates V target and pocket cash every cycle_days (default 10 trading days).
    """

    def initialize_state(self):
        """
        Loads persistent VR session state (V, pocket_cash, cycle info) from SQLite database.
        Initializes default values from config if state does not exist in DB.
        """
        self.symbol = self.ticker
        state = self.db_manager.get_vr_session_state(self.symbol)

        # Config defaults
        config_v = self.config.get("v_target")
        config_pocket = self.config.get("pocket_cash")

        if state:
            self.v_target = float(state.get("v_target", config_v if config_v is not None else 500.0))
            self.pocket_cash = float(state.get("pocket_cash", config_pocket if config_pocket is not None else 500.0))
            self.cycle_count = int(state.get("cycle_count", 1))
            self.last_cycle_date = state.get("last_cycle_date")
            self.last_rebalance_date = state.get("last_rebalance_date")
        else:
            self.v_target = float(config_v) if config_v is not None else 500.0
            self.pocket_cash = float(config_pocket) if config_pocket is not None else 500.0
            self.cycle_count = 1
            tz_us = pytz.timezone("America/New_York")
            now_str = datetime.now(tz_us).strftime("%Y-%m-%d")
            self.last_cycle_date = now_str
            self.last_rebalance_date = None
            self._save_session_state()

        mode = self.config.get("mode", "ACCUMULATE")
        band_rate = float(self.config.get("band_rate", 0.15))
        min_trade_amount = float(self.config.get("min_trade_amount", 10.0))
        rebalance_hour = int(self.config.get("rebalance_hour_us", 11))
        g_factor = float(self.config.get("g_factor", 10.0)) if self.config.get("g_factor") is not None else 10.0

        logger.info(
            f"VR Ticker [{self.ticker}] | Mode: {mode} | V Target: ${self.v_target:.2f} | "
            f"Pocket Cash: ${self.pocket_cash:.2f} | Cycle: {self.cycle_count} | "
            f"G Factor: {g_factor} | Band Rate: {band_rate * 100:.1f}% | Min Trade: ${min_trade_amount:.2f} | "
            f"Daily Rebalance Hour: {rebalance_hour}:00 US ET | "
            f"Holdings: {len(self.incomplete_orders)} | Pending Buys: {len(self.pending_buy_orders)}"
        )

    def _save_session_state(self):
        """
        Helper method to persist current VR state into SQLite.
        """
        self.db_manager.save_vr_session_state(
            symbol=self.symbol,
            v_target=self.v_target,
            pocket_cash=self.pocket_cash,
            cycle_count=self.cycle_count,
            last_cycle_date=self.last_cycle_date,
            last_rebalance_date=self.last_rebalance_date
        )

    def evaluate(self, current_price: float):
        # 1. Verify pending buy and sell order executions
        self._verify_buy_executions()

        # Check if disabled
        if not self.config.get("enabled", True):
            logger.debug(f"VR [{self.ticker}] is disabled. Skipping evaluation.")
            return

        # 2. Check market hours and fractional trading window (up to 1h before market close)
        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
        if buy_mode == "AMOUNT":
            if not self.is_fractional_trading_hours():
                return
        else:
            if not self.is_regular_market_hours():
                return

        # 3. Prevent duplicate orders if there is an active pending buy order
        if self.pending_buy_orders:
            logger.info(f"VR [{self.ticker}] - A buy order is already pending. Skipping new rebalance check.")
            return

        # 4. Check & update cycle (every cycle_days trading/calendar days)
        tz_us = pytz.timezone("America/New_York")
        now_us = datetime.now(tz_us)
        today_us_date = now_us.strftime("%Y-%m-%d")
        self._check_and_update_cycle(today_us_date)

        # 5. Perform real-time rebalance evaluation
        self._perform_rebalance(current_price, today_us_date)

    def _check_and_update_cycle(self, today_us_date: str):
        """
        Checks if cycle_days have elapsed since last_cycle_date, and updates V target & pocket cash accordingly.
        """
        cycle_days = int(self.config.get("cycle_days", 10))
        if self.last_cycle_date:
            try:
                last_dt = datetime.strptime(self.last_cycle_date, "%Y-%m-%d")
                curr_dt = datetime.strptime(today_us_date, "%Y-%m-%d")
                days_diff = (curr_dt - last_dt).days
                if days_diff < cycle_days:
                    return
            except Exception as e:
                logger.error(f"Error parsing cycle dates: {e}")

        # Cycle threshold met! Advance cycle
        self.cycle_count += 1
        mode = self.config.get("mode", "ACCUMULATE").upper()
        growth_rate = float(self.config.get("cycle_growth_rate", 0.0025))
        g_factor = float(self.config.get("g_factor", 10.0)) if self.config.get("g_factor") is not None else 10.0
        deposit = float(self.config.get("cycle_deposit", 0.0))
        withdrawal = float(self.config.get("cycle_withdrawal", 0.0))

        old_v = self.v_target
        old_pocket = self.pocket_cash

        if mode == "ACCUMULATE":
            self.pocket_cash += deposit
            if g_factor > 0:
                self.v_target = old_v + (self.pocket_cash / g_factor) + deposit
            else:
                self.v_target = (old_v * (1.0 + growth_rate)) + deposit
        elif mode == "LUMP_SUM":
            if g_factor > 0:
                self.v_target = old_v + (self.pocket_cash / g_factor)
            else:
                self.v_target = old_v * (1.0 + growth_rate)
        elif mode == "WITHDRAWAL":
            self.pocket_cash = max(0.0, old_pocket - withdrawal)
            if g_factor > 0:
                self.v_target = max(0.0, old_v + (self.pocket_cash / g_factor) - withdrawal)
            else:
                self.v_target = max(0.0, (old_v * (1.0 + growth_rate)) - withdrawal)

        self.last_cycle_date = today_us_date
        logger.info(
            f"VR [{self.ticker}] - Advanced to Cycle #{self.cycle_count}! "
            f"V Target: ${old_v:.2f} -> ${self.v_target:.2f} | "
            f"Pocket Cash: ${old_pocket:.2f} -> ${self.pocket_cash:.2f} (G={g_factor})"
        )
        self._save_session_state()

    def _perform_rebalance(self, current_price: float, today_us_date: str):
        """
        Calculates valuation E, compares with V_max and V_min, and executes rebalancing orders.
        """
        # Calculate current total holdings quantity and valuation E
        total_qty = sum(float(order.get("quantity", 0.0)) for order in self.incomplete_orders.values())
        valuation = total_qty * current_price

        band_rate = float(self.config.get("band_rate", 0.15))
        min_trade_amount = float(self.config.get("min_trade_amount", 10.0))

        v_max = self.v_target * (1.0 + band_rate)
        v_min = self.v_target / (1.0 + band_rate)

        logger.info(
            f"VR [{self.ticker}] Daily Evaluation: Holdings={total_qty:.4f} @ ${current_price:.2f} | "
            f"Valuation E=${valuation:.2f} | V=${self.v_target:.2f} | Band=[${v_min:.2f} ~ ${v_max:.2f}] | "
            f"Pocket Cash=${self.pocket_cash:.2f}"
        )

        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()

        if valuation > v_max:
            # Overvaluation (Breached Upper Band) -> Rebalance Sell
            excess_amount = valuation - v_max
            if excess_amount >= min_trade_amount and total_qty > 0:
                sell_qty = excess_amount / current_price
                if sell_qty > total_qty:
                    sell_qty = total_qty
                
                min_sell_qty = float(self.config.get("min_sell_qty", 1.0))
                if sell_qty >= min_sell_qty:
                    logger.info(f"VR [{self.ticker}] - Overvaluation detected (E=${valuation:.2f} > V_max=${v_max:.2f}). Selling {sell_qty:.4f} shares...")
                    self._execute_vr_sell(current_price, sell_qty, excess_amount)
                else:
                    logger.info(f"VR [{self.ticker}] - Overvaluation detected (E=${valuation:.2f} > V_max=${v_max:.2f}) but calculated sell qty ({sell_qty:.4f}) is less than {min_sell_qty} share. Skipping sell.")
        elif valuation < v_min:
            # Undervaluation (Breached Lower Band) -> Rebalance Buy
            deficit_amount = self.v_target - valuation
            actual_buy_amount = min(deficit_amount, self.pocket_cash)

            if actual_buy_amount >= min_trade_amount:
                logger.info(f"VR [{self.ticker}] - Undervaluation detected (E=${valuation:.2f} < V_min=${v_min:.2f}). Buying ${actual_buy_amount:.2f}...")
                self._execute_vr_buy(current_price, actual_buy_amount)
            elif self.pocket_cash < min_trade_amount:
                logger.warning(f"VR [{self.ticker}] - Undervaluation detected but Pocket Cash is depleted (${self.pocket_cash:.2f} < ${min_trade_amount:.2f}). Holding.")
        else:
            # Within Band (V_min <= E <= V_max) -> Strict Band Rebalancing (No Action)
            logger.info(f"VR [{self.ticker}] - Valuation E=${valuation:.2f} is within normal band [${v_min:.2f} ~ ${v_max:.2f}]. No rebalancing needed today.")

        # Mark daily rebalance date complete and persist state
        self.last_rebalance_date = today_us_date
        self._save_session_state()

    def _execute_vr_buy(self, current_price: float, buy_amount: float):
        """
        Executes a VR buy order (quantity or amount based) and updates Pocket Cash.
        """
        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
        if buy_mode == "QTY":
            qty = max(1, int(round(buy_amount / current_price)))
            order_amount = qty * current_price
            if order_amount > self.pocket_cash:
                qty = int(self.pocket_cash // current_price)
                order_amount = qty * current_price
            if qty <= 0:
                logger.warning(f"VR [{self.ticker}] - QTY buy order calculation yielded 0 shares. Skipping.")
                return

            res = self.api_client.place_limit_order(self.ticker, "BUY", qty, current_price)
            if res and "orderId" in res:
                oid = res["orderId"]
                self.pending_buy_orders[oid] = {
                    "orderId": oid,
                    "symbol": self.ticker,
                    "quantity": qty,
                    "price": current_price,
                    "orderedAt": datetime.now().isoformat(),
                    "isAmountBased": False,
                    "orderAmount": order_amount
                }
                self.db_manager.add_vr_pending_buy_order(oid, self.ticker, qty, current_price, is_amount_based=False, order_amount=order_amount)
                self.pocket_cash = max(0.0, self.pocket_cash - order_amount)
                self._save_session_state()
                logger.info(f"VR [{self.ticker}] - Placed QTY Buy Order ID={oid}, Qty={qty}, Price=${current_price:.2f}. Pocket Cash left: ${self.pocket_cash:.2f}")
        else:
            # AMOUNT based (fractional share)
            actual_amount = min(buy_amount, self.pocket_cash)
            est_qty = actual_amount / current_price

            res = self.api_client.place_amount_market_order(self.ticker, "BUY", actual_amount)
            if res and "orderId" in res:
                oid = res["orderId"]
                self.pending_buy_orders[oid] = {
                    "orderId": oid,
                    "symbol": self.ticker,
                    "quantity": est_qty,
                    "price": current_price,
                    "orderedAt": datetime.now().isoformat(),
                    "isAmountBased": True,
                    "orderAmount": actual_amount
                }
                self.db_manager.add_vr_pending_buy_order(oid, self.ticker, est_qty, current_price, is_amount_based=True, order_amount=actual_amount)
                self.pocket_cash = max(0.0, self.pocket_cash - actual_amount)
                self._save_session_state()
                logger.info(f"VR [{self.ticker}] - Placed AMOUNT Buy Order ID={oid}, Amount=${actual_amount:.2f}, Est Qty={est_qty:.4f}. Pocket Cash left: ${self.pocket_cash:.2f}")

    def _execute_vr_sell(self, current_price: float, sell_qty: float, approx_sell_amount: float):
        """
        Executes a VR sell order (supports fractional shares if sell_qty >= min_sell_qty) and adds expected proceeds to Pocket Cash.
        """
        min_sell_qty = float(self.config.get("min_sell_qty", 1.0))
        if sell_qty < min_sell_qty:
            logger.info(f"VR [{self.ticker}] - Calculated sell qty {sell_qty:.4f} is below minimum {min_sell_qty} share. Skipping sell.")
            return

        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
        if buy_mode == "QTY":
            exec_qty = float(int(sell_qty))
            if exec_qty < 1.0:
                return
            res = self.api_client.place_limit_order(self.ticker, "SELL", int(exec_qty), current_price)
        else:
            exec_qty = sell_qty
            res = self.api_client.place_market_order(self.ticker, "SELL", exec_qty)

        if res and "orderId" in res:
            oid = res["orderId"]
            proceeds = exec_qty * current_price
            self.pocket_cash += proceeds
            self._save_session_state()

            # Record trade history
            total_cost = sum(float(o.get("price", 0.0)) * float(o.get("quantity", 0.0)) for o in self.incomplete_orders.values())
            total_qty = sum(float(o.get("quantity", 0.0)) for o in self.incomplete_orders.values())
            avg_buy_price = (total_cost / total_qty) if total_qty > 0 else current_price
            profit = (current_price - avg_buy_price) * exec_qty

            self.db_manager.add_vr_trade_history(self.ticker, exec_qty, avg_buy_price, current_price, profit, oid)

            # Reduce incomplete orders proportionally or remove
            remaining_to_deduct = float(exec_qty)
            for order_key in list(self.incomplete_orders.keys()):
                ord_qty = float(self.incomplete_orders[order_key].get("quantity", 0.0))
                if ord_qty <= remaining_to_deduct:
                    remaining_to_deduct -= ord_qty
                    del self.incomplete_orders[order_key]
                    self.db_manager.remove_vr_incomplete_order(order_key)
                else:
                    new_qty = ord_qty - remaining_to_deduct
                    self.incomplete_orders[order_key]["quantity"] = str(new_qty)
                    self.db_manager.add_vr_incomplete_order(order_key, self.incomplete_orders[order_key])
                    remaining_to_deduct = 0
                    break

            logger.info(f"VR [{self.ticker}] - Executed Sell Order ID={oid}, Qty={exec_qty:.4f} shares, Price=${current_price:.2f}, Profit=${profit:.2f}. New Pocket Cash: ${self.pocket_cash:.2f}")

    def _verify_buy_executions(self):
        """
        Verifies pending VR buy orders against Toss OpenAPI and moves filled orders to holdings.
        """
        if not self.pending_buy_orders:
            return

        for oid, pending_info in list(self.pending_buy_orders.items()):
            order_detail = self.api_client.get_order_details(oid)
            if not order_detail:
                continue

            status = order_detail.get("status", "").upper()
            if status in ["FILLED", "SUCCESS", "COMPLETED"]:
                exec_qty = float(order_detail.get("executedQuantity", pending_info.get("quantity", 0.0)))
                exec_price = float(order_detail.get("executedPrice", pending_info.get("price", 0.0)))

                holding_data = {
                    "symbol": self.ticker,
                    "price": exec_price,
                    "quantity": exec_qty,
                    "orderedAt": pending_info.get("orderedAt", datetime.now().isoformat())
                }
                self.incomplete_orders[oid] = holding_data
                self.db_manager.add_vr_incomplete_order(oid, holding_data)
                self.db_manager.remove_vr_pending_buy_order(oid)
                del self.pending_buy_orders[oid]
                logger.info(f"VR [{self.ticker}] - Buy Order {oid} FILLED! Exec Qty={exec_qty:.4f}, Price=${exec_price:.2f}")
            elif status in ["CANCELLED", "REJECTED", "FAILED"]:
                # Refund pocket cash if order failed/cancelled
                refund_amount = pending_info.get("orderAmount", 0.0)
                self.pocket_cash += refund_amount
                self._save_session_state()
                self.db_manager.remove_vr_pending_buy_order(oid)
                del self.pending_buy_orders[oid]
                logger.warning(f"VR [{self.ticker}] - Buy Order {oid} {status}. Refunded ${refund_amount:.2f} to Pocket Cash.")
