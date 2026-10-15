import time
import logging
from datetime import datetime, timedelta
import pytz

from config import update_stop_loss_count
from strategies.base import BaseStrategy

logger = logging.getLogger("TossTradeBot.Strategy.Grid")

class GridStrategy(BaseStrategy):
    """
    Standard Grid Trading strategy.
    Buys when price drops below the threshold calculated from the lowest active sell target,
    or chases upward rises to fill missing grid gaps.
    """
    def is_active(self) -> bool:
        """
        Determines if Grid strategy needs evaluation in the current scheduler tick:
        1. If disabled and no pending buy orders and no holdings to sell, remain idle.
        2. If active orders in-flight exist, active during regular market hours for reconciliation.
        3. Check trading window:
           - QTY mode: Regular market hours starting 15 minutes after open, ending 10 minutes before close.
             * US: 09:45 EST ~ 15:50 EST (Mon-Fri)
             * KR: 09:15 KST ~ 15:10 KST (Mon-Fri)
           - AMOUNT mode: Fractional trading window:
             * US: 09:40 EST ~ 14:50 EST (Mon-Fri)
             * KR: 09:00 KST ~ 14:20 KST (Mon-Fri)
        """
        # If disabled and no pending orders and no holdings to sell, stay idle
        if not self.config.get("enabled", True):
            if self.pending_buy_orders or any(o.get("exchangeOrderId") for o in self.incomplete_orders.values()):
                return self.is_regular_market_hours()
            if not self.incomplete_orders:
                return False

        # Active orders in-flight need reconciliation during regular market hours
        if self.pending_buy_orders or any(o.get("exchangeOrderId") for o in self.incomplete_orders.values()):
            return self.is_regular_market_hours()

        # Check if currently within trading window
        return self._is_trading_window()

    def _is_trading_window(self) -> bool:
        """
        Checks if current time is within the allowed trading window:
        - For QTY mode:
          Regular market hours starting open_delay_minutes (default 15m) after market open,
          closing close_buffer_minutes (default 10m) before market close.
          * US: 09:45 ~ 15:50 EST (Monday-Friday)
          * KR: 09:15 ~ 15:10 KST (Monday-Friday)
        - For AMOUNT mode:
          Fractional share trading hours:
          * US: 09:40 ~ 14:50 EST (Monday-Friday)
          * KR: 09:00 ~ 14:20 KST (Monday-Friday)
        """
        market = self.config.get("market", "US").upper()
        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()
        open_delay_mins = int(self.config.get("open_delay_minutes", 15))
        close_buffer_mins = int(self.config.get("close_buffer_minutes", 10))

        if market == "KR":
            tz_kr = pytz.timezone("Asia/Seoul")
            now = datetime.now(tz_kr)
            if now.weekday() >= 5:
                return False

            if buy_mode == "QTY":
                open_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
                start_time = open_time + timedelta(minutes=open_delay_mins)
                close_time = now.replace(hour=15, minute=20, second=0, microsecond=0)
                end_time = close_time - timedelta(minutes=close_buffer_mins)
            else:
                start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
                end_time = now.replace(hour=14, minute=20, second=0, microsecond=0)

            return start_time <= now <= end_time
        else:
            tz_us = pytz.timezone("America/New_York")
            now = datetime.now(tz_us)
            if now.weekday() >= 5:
                return False

            if buy_mode == "QTY":
                open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
                start_time = open_time + timedelta(minutes=open_delay_mins)
                close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
                end_time = close_time - timedelta(minutes=close_buffer_mins)
            else:
                start_time = now.replace(hour=9, minute=40, second=0, microsecond=0)
                end_time = now.replace(hour=14, minute=50, second=0, microsecond=0)

            return start_time <= now <= end_time

    def evaluate(self, current_price: float):
        # Step 1: Reconcile Sell Executions
        self._verify_sell_executions(current_price)
        
        # Step 2: Reconcile Pending Buy Executions
        self._verify_buy_executions()

        # Check trading window (for QTY: open+15m to close-10m)
        if not self._is_trading_window():
            logger.debug(f"Grid Ticker [{self.ticker}] is outside trading window. Skipping new evaluations.")
            return

        # Step 2.5: Check for Stop-Loss request (Regular market hours only)
        stop_loss_count = int(self.config.get("stop_loss_count", 0))
        if stop_loss_count > 0:
            if self.is_regular_market_hours():
                self._process_stop_loss(stop_loss_count, current_price)
                return
            else:
                logger.info(
                    f"Instance [{self.instance_key}] - Stop loss requested (count={stop_loss_count}), "
                    f"but outside regular market hours. Waiting for regular market session..."
                )
        
        # If the ticker is disabled, block any new buys
        if not self.config.get("enabled", True):
            logger.debug(f"Grid Ticker [{self.ticker}] is disabled. Skipping new buy checks.")
            return
        
        # Step 3: Evaluate grid buying triggers
        if self._is_in_cooldown():
            return
            
        # If there is already a pending buy order, skip evaluation to prevent duplication
        if self.pending_buy_orders:
            return

        yield_target = self.config.get("yield_target", 0.02)
        grid_interval = self.config.get("grid_interval", 0.01)
        fill_grid_on_rise = self.config.get("fill_grid_on_rise", True)
        
        # Check if grid is empty
        if not self.incomplete_orders:
            logger.info(f"No active sells and no pending buys for [{self.ticker}]. Placing initial seed buy order...")
            self._place_grid_buy(current_price)
            return

        # 1. 상승 중 비어있는 그리드 격자 메우기 전략 (fill_grid_on_rise)
        if fill_grid_on_rise:
            target_sell_price = current_price * (1 + yield_target)
            range_min = target_sell_price * (1 - grid_interval)
            range_max = target_sell_price * (1 + grid_interval)
            
            # Check if any incomplete sell order price lies within target_sell_price +- grid_interval
            has_matching_sell = False
            for sell_order in self.incomplete_orders.values():
                sell_p = float(sell_order["price"])
                if range_min <= sell_p <= range_max:
                    has_matching_sell = True
                    break
                    
            if not has_matching_sell:
                logger.warning(
                    f"Ticker [{self.ticker}] - Rise grid gap detected. No active sell targets around "
                    f"target sell price {target_sell_price:.2f} (Checked range: {range_min:.2f} ~ {range_max:.2f}). "
                    f"Placing chase buy to fill the grid."
                )
                self._place_grid_buy(current_price)
                return

        # 2. 기존 최저 매도 목표가 대비 하락 매수 (Fall grid buying)
        # Find lowest active target sell price
        sorted_sells = sorted(
            self.incomplete_orders.values(),
            key=lambda x: float(x.get("price", 0.0))
        )
        lowest_sell_order = sorted_sells[0]
        lowest_sell_price = float(lowest_sell_order["price"])
        
        # Calculate target trigger price
        required_drop = yield_target + grid_interval
        trigger_price = lowest_sell_price * (1 - required_drop)
        
        logger.info(
            f"Grid Check [{self.ticker}] | Lowest Sell: {lowest_sell_price:.2f} | "
            f"Drop Threshold: {required_drop * 100}% | Trigger Buy Price <= {trigger_price:.2f}"
        )
        
        if current_price <= trigger_price:
            logger.info(
                f"Price target met for [{self.ticker}]! Current {current_price:.2f} is below target {trigger_price:.2f}. "
                f"Triggering buy."
            )
            # Verify if we already have a pending buy near or at this price to avoid duplicates
            for pending_buy in self.pending_buy_orders.values():
                p_price = float(pending_buy.get("price", 0.0))
                if abs(p_price - current_price) / current_price < 0.002:
                    logger.info(f"A pending buy order is already open at a similar price for [{self.ticker}]. Skipping duplicate.")
                    return
            
            self._place_grid_buy(current_price)
        else:
            logger.info(f"Price target not met for [{self.ticker}]. No new buy orders triggered.")

    def _execute_single_stop_loss(self, order: dict, current_price: float) -> tuple[float | None, float]:
        """
        Executes a market/limit sell to stop-loss a single position.
        Cancels any existing open limit order on exchange first.
        Returns (actual_sell_price, filled_qty).
        """
        order_id = order.get("orderId") or order.get("order_id")
        exchange_order_id = order.get("exchangeOrderId", "")
        target_price = float(order.get("price", 0.0))
        qty = float(order.get("quantity", 0.0))
        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()

        logger.info(
            f"  Executing Stop Loss for Order ID: {order_id} | Target Price: {target_price:.2f} | "
            f"Qty: {qty} | Current Market Price: {current_price:.2f}"
        )

        # 1. Cancel active limit order on exchange if open
        if exchange_order_id:
            try:
                logger.info(f"  Cancelling open exchange sell order {exchange_order_id} for stop loss...")
                self.api_client.cancel_order(exchange_order_id)
            except Exception as cancel_err:
                logger.error(f"  Failed to cancel exchange order {exchange_order_id} during stop loss: {cancel_err}")

        # 2. Execute market/limit sell for stop loss
        actual_sell_price = current_price
        filled_qty = 0.0
        new_exchange_id = ""

        try:
            if buy_mode == "AMOUNT":
                sell_res = self.api_client.place_market_order(self.ticker, "SELL", qty)
            else:
                sell_res = self.api_client.place_limit_order(self.ticker, "SELL", int(qty), current_price)

            new_exchange_id = sell_res.get("orderId", "")
            if new_exchange_id:
                for attempt in range(3):
                    time.sleep(1.0)
                    try:
                        details = self.api_client.get_order_details(new_exchange_id)
                        status = details.get("status")
                        if status == "FILLED":
                            filled_p = self._extract_filled_price(details)
                            if filled_p:
                                actual_sell_price = filled_p
                            filled_qty = qty
                            break
                        elif status in ["CANCELED", "REJECTED"]:
                            logger.warning(f"  Stop loss order {new_exchange_id} status: {status}")
                            break
                    except Exception as poll_err:
                        logger.error(f"  Error checking status for stop loss order {new_exchange_id}: {poll_err}")
        except Exception as order_err:
            logger.error(f"  Error submitting stop loss sell order for {order_id}: {order_err}")

        if filled_qty > 0:
            self.db_manager.remove_incomplete_order(order_id, actual_sell_price, status="STOP_LOSS")
            self._reset_consecutive_buys()
            if order_id in self.incomplete_orders:
                del self.incomplete_orders[order_id]
            logger.info(f"  Stop Loss completed for order {order_id} at price {actual_sell_price:.2f}. DB updated with status=STOP_LOSS.")
            return actual_sell_price, filled_qty
        else:
            # If not filled within polling window, cancel the order on exchange
            logger.warning(
                f"  Stop loss sell order for {order_id} (Exchange ID: {new_exchange_id}) was not filled within polling window. "
                f"Cancelling open exchange order..."
            )
            if new_exchange_id:
                try:
                    self.api_client.cancel_order(new_exchange_id)
                    check_details = self.api_client.get_order_details(new_exchange_id)
                    if check_details.get("status") == "FILLED":
                        filled_p = self._extract_filled_price(check_details)
                        if filled_p:
                            actual_sell_price = filled_p
                        self.db_manager.remove_incomplete_order(order_id, actual_sell_price, status="STOP_LOSS")
                        self._reset_consecutive_buys()
                        if order_id in self.incomplete_orders:
                            del self.incomplete_orders[order_id]
                        logger.info(f"  Stop Loss order {new_exchange_id} filled right before cancel. DB updated.")
                        return actual_sell_price, qty
                    else:
                        execution = check_details.get("execution", {})
                        filled_qty_str = execution.get("filledQuantity", "0")
                        part_filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
                        if part_filled_qty > 0:
                            remaining_qty = qty - part_filled_qty
                            self.db_manager.update_incomplete_order_quantity(order_id, remaining_qty)
                            order["quantity"] = str(remaining_qty)
                            filled_p = self._extract_filled_price(check_details)
                            if filled_p:
                                actual_sell_price = filled_p
                            logger.info(f"  Partial fill detected ({part_filled_qty} shares) for {order_id}. Remaining: {remaining_qty}")
                            self.db_manager.update_incomplete_order_exchange_id(order_id, "")
                            order["exchangeOrderId"] = ""
                            return actual_sell_price, part_filled_qty
                        self.db_manager.update_incomplete_order_exchange_id(order_id, "")
                        order["exchangeOrderId"] = ""
                except Exception as cancel_retry_err:
                    logger.error(f"  Failed to cancel unfilled stop loss order {new_exchange_id}: {cancel_retry_err}")
                    self.db_manager.update_incomplete_order_exchange_id(order_id, "")
                    order["exchangeOrderId"] = ""
            return None, 0.0

    def _process_stop_loss(self, stop_loss_count: int, current_price: float):
        """
        Executes stop loss for the top N positions with the highest target sell price.
        If an order fails to fill (e.g. price plunges rapidly), cancels the open exchange order,
        preserves the remaining position, and decrements stop_loss_count only for filled sales
        so unfilled stop losses are retried on the next tick at the updated market price.
        """
        logger.warning(f"Ticker [{self.ticker}] - Stop Loss Triggered! Requested count: {stop_loss_count}")
        
        if not self.incomplete_orders:
            logger.warning(f"Ticker [{self.ticker}] - No active sell positions available for stop loss.")
            update_stop_loss_count(self.instance_key, 0)
            self.config["stop_loss_count"] = 0
            return

        # Sort incomplete orders by target sell price descending (최상단: 매도예정가가 가장 높은 순)
        sorted_sells = sorted(
            list(self.incomplete_orders.values()),
            key=lambda x: float(x.get("price", 0.0)),
            reverse=True
        )
        
        target_sells = sorted_sells[:stop_loss_count]
        logger.info(f"Ticker [{self.ticker}] - Selected {len(target_sells)} top positions for stop loss.")

        successfully_filled_count = 0
        for order in target_sells:
            actual_price, filled_q = self._execute_single_stop_loss(order, current_price)
            if filled_q > 0:
                successfully_filled_count += 1

        # Remaining stop_loss_count to retry on next tick
        remaining_count = max(0, stop_loss_count - successfully_filled_count)
        update_stop_loss_count(self.instance_key, remaining_count)
        self.config["stop_loss_count"] = remaining_count
        logger.info(
            f"Ticker [{self.ticker}] - Stop Loss step completed. "
            f"Filled: {successfully_filled_count}/{len(target_sells)}. Remaining stop_loss_count: {remaining_count}."
        )

    def _is_first_trading_day_of_month(self, now_dt: datetime) -> bool:
        """
        Checks if the given market datetime represents the first trading day of the month.
        Trading days are Monday through Friday, skipping weekends and standard New Year's holiday (Jan 1).
        """
        if now_dt.weekday() >= 5:
            return False

        for day in range(1, now_dt.day):
            if now_dt.month == 1 and day == 1:
                continue
            check_date = now_dt.replace(day=day)
            if check_date.weekday() < 5:
                return False
        return True

    def _parse_order_time(self, ordered_at_str: str) -> datetime | None:
        """
        Parses ISO or standard datetime strings from SQLite into UTC naive datetime.
        """
        if not ordered_at_str:
            return None
        try:
            clean_str = str(ordered_at_str).replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_str)
            if dt.tzinfo is not None:
                dt = dt.astimezone(pytz.utc).replace(tzinfo=None)
            return dt
        except Exception:
            try:
                return datetime.strptime(str(ordered_at_str)[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None

    def _process_monthly_stop_loss(self, current_price: float):
        """
        Monthly stop loss logic executed on the first trading day of each month:
        1. Executed once per month prior to evaluating new grid buys.
        2. Calculates net profit of grid trading in the previous month (excluding stop losses).
        3. Caps cumulative stop loss to 10% of the previous month's net profit.
        4. Targets incomplete positions waiting 30+ days (ordered from oldest to newest).
        5. Calculates loss against current_price; sells if position incurs loss.
        6. Repeats selling next eligible oldest positions until cumulative loss reaches or exceeds 10% budget.
        """
        if not self.config.get("monthly_stop_loss_enabled", True):
            return

        market = self.config.get("market", "US").upper()
        tz = pytz.timezone("Asia/Seoul" if market == "KR" else "America/New_York")
        now_market = datetime.now(tz)

        # 1. Check if today is the first trading day of the month
        if not self._is_first_trading_day_of_month(now_market):
            return

        # 2. Check if monthly stop loss was already executed this month
        today_str = now_market.strftime("%Y-%m-%d")
        current_month_str = now_market.strftime("%Y-%m")
        if self.last_monthly_stop_loss_date and self.last_monthly_stop_loss_date.startswith(current_month_str):
            logger.debug(
                f"Ticker [{self.ticker}] - Monthly stop loss already performed for {current_month_str} "
                f"on {self.last_monthly_stop_loss_date}. Skipping."
            )
            return

        # 3. Calculate previous month (YYYY-MM)
        if now_market.month == 1:
            prev_year = now_market.year - 1
            prev_month = 12
        else:
            prev_year = now_market.year
            prev_month = now_market.month - 1
        prev_month_str = f"{prev_year:04d}-{prev_month:02d}"

        # 4. Query net profit for GRID strategy in previous month (profitable completed trades only)
        prev_profit = self.db_manager.get_grid_monthly_profit(self.ticker, prev_month_str)
        logger.info(
            f"Ticker [{self.ticker}] - Monthly Stop Loss Check: First trading day of {current_month_str} ({today_str}). "
            f"Previous Month ({prev_month_str}) Grid Profit: {prev_profit:.2f}"
        )

        if prev_profit <= 0:
            logger.info(
                f"Ticker [{self.ticker}] - Previous month profit is {prev_profit:.2f} <= 0. "
                f"No stop loss budget available. Marking monthly stop loss as completed."
            )
            self.last_monthly_stop_loss_date = today_str
            self.db_manager.save_grid_session_state(self.ticker, today_str)
            return

        # 5. Calculate loss budget (default 10% of previous month's profit)
        loss_rate = float(self.config.get("monthly_stop_loss_rate", 0.10))
        loss_budget = prev_profit * loss_rate
        min_holding_days = int(self.config.get("monthly_stop_loss_holding_days", 30))

        logger.info(
            f"Ticker [{self.ticker}] - Monthly Stop Loss Active! "
            f"Profit: {prev_profit:.2f} | Loss Budget ({loss_rate * 100:.1f}%): {loss_budget:.2f} | "
            f"Min Holding Days: {min_holding_days}"
        )

        # 6. Find eligible positions: holding >= 30 days and in loss (buy_price > current_price)
        if not self.incomplete_orders:
            logger.info(f"Ticker [{self.ticker}] - No incomplete orders to evaluate for monthly stop loss.")
            self.last_monthly_stop_loss_date = today_str
            self.db_manager.save_grid_session_state(self.ticker, today_str)
            return

        now_utc = datetime.now(pytz.utc).replace(tzinfo=None)
        candidates = []

        for order_id, order in list(self.incomplete_orders.items()):
            ordered_at_str = order.get("orderedAt") or order.get("ordered_at")
            ordered_dt = self._parse_order_time(ordered_at_str)
            if not ordered_dt:
                continue

            holding_days = (now_utc - ordered_dt).total_seconds() / 86400.0
            if holding_days < min_holding_days:
                continue

            buy_price = float(order.get("buyPrice") or order.get("buy_price", 0.0))
            qty = float(order.get("quantity", 0.0))

            # Only positions that incur loss when sold at current_price
            if buy_price <= current_price:
                continue

            expected_loss = (buy_price - current_price) * qty
            candidates.append({
                "order": order,
                "order_id": order_id,
                "buy_price": buy_price,
                "qty": qty,
                "ordered_dt": ordered_dt,
                "holding_days": holding_days,
                "expected_loss": expected_loss
            })

        if not candidates:
            logger.info(
                f"Ticker [{self.ticker}] - No aged positions (>= {min_holding_days} days) incurring loss "
                f"at current price {current_price:.2f}. Monthly stop loss completed."
            )
            self.last_monthly_stop_loss_date = today_str
            self.db_manager.save_grid_session_state(self.ticker, today_str)
            return

        # Sort candidates by ordered_dt ascending (가장 오래된 대기 물량부터)
        candidates.sort(key=lambda x: x["ordered_dt"])
        logger.info(
            f"Ticker [{self.ticker}] - Found {len(candidates)} eligible aged position(s) for monthly stop loss."
        )

        # 7. Execute stop loss sequentially and stop when cumulative loss >= loss_budget
        total_loss = 0.0
        for cand in candidates:
            order = cand["order"]
            order_id = cand["order_id"]
            buy_price = cand["buy_price"]

            logger.info(
                f"Ticker [{self.ticker}] - Processing Monthly Stop Loss for Order: {order_id} | "
                f"Holding Days: {cand['holding_days']:.1f} | Buy Price: {buy_price:.2f} | "
                f"Current Price: {current_price:.2f} | Expected Loss: {cand['expected_loss']:.2f}"
            )

            actual_sell_price, filled_qty = self._execute_single_stop_loss(order, current_price)

            if filled_qty > 0 and actual_sell_price is not None:
                realized_loss = (buy_price - actual_sell_price) * filled_qty
                if realized_loss > 0:
                    total_loss += realized_loss
                logger.info(
                    f"Ticker [{self.ticker}] - Stop Loss filled for {order_id} at {actual_sell_price:.2f}. "
                    f"Realized Loss: {realized_loss:.2f} | Cumulative Loss: {total_loss:.2f} / Budget: {loss_budget:.2f}"
                )
            else:
                logger.warning(f"Ticker [{self.ticker}] - Stop Loss order {order_id} was not filled.")

            # 매도후 손실금액이 이전월 수익의 10%를 넘으면 중단
            if total_loss >= loss_budget:
                logger.info(
                    f"Ticker [{self.ticker}] - Cumulative stop loss ({total_loss:.2f}) reached or exceeded "
                    f"budget ({loss_budget:.2f}). Stopping monthly stop loss."
                )
                break
            else:
                logger.info(
                    f"Ticker [{self.ticker}] - Cumulative loss ({total_loss:.2f}) < budget ({loss_budget:.2f}). "
                    f"Checking next eligible aged position..."
                )

        # 8. Mark monthly execution completed for this month
        self.last_monthly_stop_loss_date = today_str
        self.db_manager.save_grid_session_state(self.ticker, today_str)
        logger.info(
            f"Ticker [{self.ticker}] - Monthly stop loss completed for {today_str}. "
            f"Total realized loss: {total_loss:.2f}. Resuming normal grid evaluation."
        )

