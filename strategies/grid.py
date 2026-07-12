import logging
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
