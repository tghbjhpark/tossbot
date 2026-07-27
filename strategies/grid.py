import time
import logging
from config import update_stop_loss_count
from strategies.base import BaseStrategy

logger = logging.getLogger("TossTradeBot.Strategy.Grid")

class GridStrategy(BaseStrategy):
    """
    Standard Grid Trading strategy.
    Buys when price drops below the threshold calculated from the lowest active sell target,
    or chases upward rises to fill missing grid gaps.
    """
    def evaluate(self, current_price: float):
        # Step 1: Reconcile Sell Executions
        self._verify_sell_executions(current_price)
        
        # Step 2: Reconcile Pending Buy Executions
        self._verify_buy_executions()

        # Step 2.5: Check for Stop-Loss request
        stop_loss_count = int(self.config.get("stop_loss_count", 0))
        if stop_loss_count > 0:
            self._process_stop_loss(stop_loss_count, current_price)
            return
        
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

    def _process_stop_loss(self, stop_loss_count: int, current_price: float):
        """
        Executes stop loss for the top N positions with the highest target sell price.
        After selling, updates SQLite DB and sets stop_loss_count to 0 in config.
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

        buy_mode = self.config.get("buy_mode", "AMOUNT").upper()

        for order in target_sells:
            order_id = order.get("orderId")
            exchange_order_id = order.get("exchangeOrderId", "")
            target_price = float(order.get("price", 0.0))
            qty = float(order.get("quantity", 0.0))
            
            logger.info(
                f"  Executing Stop Loss for Order ID: {order_id} | Target Price: {target_price:.2f} | "
                f"Qty: {qty} | Current Market Price: {current_price:.2f}"
            )
            
            # Cancel active limit order on exchange if open
            if exchange_order_id:
                try:
                    logger.info(f"  Cancelling open exchange sell order {exchange_order_id} for stop loss...")
                    self.api_client.cancel_order(exchange_order_id)
                except Exception as cancel_err:
                    logger.error(f"  Failed to cancel exchange order {exchange_order_id} during stop loss: {cancel_err}")

            # Execute market/limit sell for stop loss
            actual_sell_price = current_price
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
                            if details.get("status") == "FILLED":
                                filled_p = self._extract_filled_price(details)
                                if filled_p:
                                    actual_sell_price = filled_p
                                break
                        except Exception:
                            pass
            except Exception as order_err:
                logger.error(f"  Error submitting stop loss sell order for {order_id}: {order_err}")

            # Update DB (mark trade history COMPLETED and remove incomplete order)
            self.db_manager.remove_incomplete_order(order_id, actual_sell_price)
            self._reset_consecutive_buys()
            if order_id in self.incomplete_orders:
                del self.incomplete_orders[order_id]
                
            logger.info(f"  Stop Loss completed for order {order_id} at price {actual_sell_price:.2f}. DB updated.")

        # Reset stop_loss_count in config file and in-memory config to 0
        update_stop_loss_count(self.instance_key, 0)
        self.config["stop_loss_count"] = 0
        logger.info(f"Ticker [{self.ticker}] - Stop Loss procedure completed. stop_loss_count reset to 0.")

