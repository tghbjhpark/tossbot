import time
import logging
from datetime import datetime
import pytz

from config import TICKERS, TICKER_CONFIGS, reload_config_if_changed
from sqlite_manager import SQLiteManager
from toss_api import TossAPIClient
from strategies import get_strategy_class

logger = logging.getLogger("TossTradeBot.Trader")

class TradeBot:
    """
    Main Trading Bot Orchestrator.
    Manages timing check, config reloading, current price batch queries,
    and delegates stock-specific logic to strategy instances.
    """
    def __init__(self, api_client: TossAPIClient, db_manager: SQLiteManager):
        self.api_client = api_client
        self.db_manager = db_manager
        self.strategies = {}
        self.initialize_strategies()

    def initialize_strategies(self):
        """
        Instantiates/updates Strategy objects for all configured instances.
        Preserves existing in-memory cooldown or order caches if possible.
        """
        old_strategies = self.strategies
        self.strategies = {}
        
        for instance_key, config in TICKER_CONFIGS.items():
            ticker = config.get("ticker")
            strategy_name = config.get("strategy", "GRID")
            strategy_class = get_strategy_class(strategy_name)
            
            strategy_instance = strategy_class(ticker, self.api_client, self.db_manager, config)
            strategy_instance.instance_key = instance_key
            
            # Carry over active states to prevent losing caches/cooldowns during hot-reload
            if instance_key in old_strategies:
                strategy_instance.cooldown_state = old_strategies[instance_key].cooldown_state
                strategy_instance.incomplete_orders = old_strategies[instance_key].incomplete_orders
                strategy_instance.pending_buy_orders = old_strategies[instance_key].pending_buy_orders
                
            self.strategies[instance_key] = strategy_instance
            
        logger.info(f"Initialized strategy instances: {list(self.strategies.keys())}")

    def initialize_state(self):
        """
        Loads active order caches from SQLite and distributes them to matching strategy instances.
        """
        logger.info("Initializing in-memory state from SQLite for strategy instances...")
        
        # Load GRID data
        raw_grid_incomplete = self.db_manager.get_incomplete_orders()
        raw_grid_pending = self.db_manager.get_pending_buy_orders()
        
        # Load DCA data
        raw_dca_incomplete = self.db_manager.get_dca_incomplete_orders()
        raw_dca_pending = self.db_manager.get_dca_pending_buy_orders()
        
        # Load VR data
        raw_vr_pending = self.db_manager.get_vr_pending_buy_orders()
        
        # Load CRSI data
        raw_crsi_incomplete = self.db_manager.get_crsi_incomplete_orders()
        raw_crsi_pending = self.db_manager.get_crsi_pending_buy_orders()
        
        # Load TVR data
        raw_tvr_pending = self.db_manager.get_tvr_pending_buy_orders()
        
        for instance_key, strategy in self.strategies.items():
            strategy_name = strategy.config.get("strategy", "GRID").upper()
            ticker = strategy.ticker
            
            if strategy_name == "DCA":
                strategy.incomplete_orders = {
                    oid: order for oid, order in raw_dca_incomplete.items() if order.get("symbol") == ticker
                }
                strategy.pending_buy_orders = {
                    oid: order for oid, order in raw_dca_pending.items() if order.get("symbol") == ticker
                }
            elif strategy_name == "VR":
                strategy.incomplete_orders = {}
                strategy.pending_buy_orders = {
                    oid: order for oid, order in raw_vr_pending.items() if order.get("symbol") == ticker
                }
            elif strategy_name == "CRSI":
                strategy.incomplete_orders = {
                    oid: order for oid, order in raw_crsi_incomplete.items() if order.get("symbol") == ticker
                }
                strategy.pending_buy_orders = {
                    oid: order for oid, order in raw_crsi_pending.items() if order.get("symbol") == ticker
                }
            elif strategy_name in ["TVR", "SVR", "TARGET_VALUE", "SIMPLE_VR"]:
                strategy.incomplete_orders = {}
                strategy.pending_buy_orders = {
                    oid: order for oid, order in raw_tvr_pending.items() if order.get("symbol") == ticker
                }
            else: # GRID
                strategy.incomplete_orders = {
                    oid: order for oid, order in raw_grid_incomplete.items() if order.get("symbol") == ticker
                }
                strategy.pending_buy_orders = {
                    oid: order for oid, order in raw_grid_pending.items() if order.get("symbol") == ticker
                }
            strategy.initialize_state()

    def is_market_active_for_strategy(self, strategy) -> bool:
        """
        Checks if the trading session is active for the given strategy instance,
        distinguishing between US and Korean stock markets.
        """
        if hasattr(strategy, "is_active") and callable(strategy.is_active):
            return strategy.is_active()

        config = strategy.config
        market = config.get("market", "US").upper()
        strategy_type = config.get("strategy", "").upper()
        buy_mode = config.get("buy_mode", "AMOUNT").upper()
        
        if market == "KR":
            # Korea market hours check: Monday-Friday, 09:00 AM to 03:20 PM KST
            tz_kst = pytz.timezone("Asia/Seoul")
            now_kst = datetime.now(tz_kst)
            
            if now_kst.weekday() >= 5:
                return False
                
            if strategy_type in ["DCA", "GRID"] and buy_mode == "QTY":
                # DCA/GRID 일반 수량 주문 (QTY): 정규 거래 시작 15분 후(09:15)부터 장 마감 10분 전(15:10)까지
                start_time = now_kst.replace(hour=9, minute=15, second=0, microsecond=0)
                end_time = now_kst.replace(hour=15, minute=10, second=0, microsecond=0)
            else:
                start_time = now_kst.replace(hour=9, minute=0, second=0, microsecond=0)
                end_time = now_kst.replace(hour=15, minute=20, second=0, microsecond=0)
            
            return start_time <= now_kst <= end_time
            
        else:
            # US market hours check
            tz_est = pytz.timezone("America/New_York")
            now_est = datetime.now(tz_est)
            
            if now_est.weekday() >= 5:
                return False
                
            if strategy_type in ["DCA", "GRID"] and buy_mode == "QTY":
                # DCA/GRID 일반 수량 주문 (QTY): 정규 거래 시작 15분 후(09:45)부터 장 마감 10분 전(15:50)까지
                start_time = now_est.replace(hour=9, minute=45, second=0, microsecond=0)
                end_time = now_est.replace(hour=15, minute=50, second=0, microsecond=0)
                return start_time <= now_est <= end_time
            elif buy_mode == "AMOUNT":
                # 소수점 금액 주문: 09:40 ~ 14:50 (09:30 ~ 15:00 대비 앞뒤 10분 버퍼 적용)
                start_time = now_est.replace(hour=9, minute=40, second=0, microsecond=0)
                end_time = now_est.replace(hour=14, minute=50, second=0, microsecond=0)
                return start_time <= now_est <= end_time
            else:
                # 일반 수량 주문 (QTY): 평일 상시 작동 (시간 제한 없음)
                return True

    def is_market_active_for_ticker(self, ticker: str) -> bool:
        """
        Backward compatibility helper for ticker checking.
        """
        matching_strats = [s for s in self.strategies.values() if s.ticker == ticker]
        if not matching_strats:
            return False
        return any(self.is_market_active_for_strategy(s) for s in matching_strats)

    def is_first_trading_day_of_month(self, now_dt: datetime) -> bool:
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

    def process_monthly_grid_stop_loss(self, prices: dict):
        """
        Market-wide unified monthly stop loss for GRID strategy positions:
        1. Executed once per month on the first trading day of each market (US / KR).
        2. Executed prior to evaluating regular grid buying triggers.
        3. Aggregates previous month's net profit across all grid tickers in the same market (excluding stop-loss trades).
        4. Caps cumulative stop loss to 10% of the aggregate net profit.
        5. Pools all aged (>= 30 days) positions incurring loss across all grid tickers in that market,
           sorted by ordered_at ascending (oldest first across tickers).
        6. Sells positions sequentially until cumulative realized loss reaches or exceeds the 10% budget.
        """
        markets = ["US", "KR"]
        for market in markets:
            tz = pytz.timezone("Asia/Seoul" if market == "KR" else "America/New_York")
            now_market = datetime.now(tz)

            # 1. First trading day of the month check
            if not self.is_first_trading_day_of_month(now_market):
                continue

            # 2. Check if monthly stop loss was already executed this month for this market
            today_str = now_market.strftime("%Y-%m-%d")
            current_month_str = now_market.strftime("%Y-%m")
            session_state = self.db_manager.get_grid_session_state(market)
            last_date = session_state.get("last_monthly_stop_loss_date", "")
            if last_date and last_date.startswith(current_month_str):
                continue

            # 3. Find all GRID strategy instances in this market
            market_grid_strats = [
                s for s in self.strategies.values()
                if s.config.get("strategy", "GRID").upper() == "GRID"
                and s.config.get("market", "US").upper() == market
                and s.config.get("monthly_stop_loss_enabled", True)
            ]
            if not market_grid_strats:
                continue

            # Check if current time is within regular market hours for this market
            if not any(s.is_regular_market_hours() for s in market_grid_strats):
                continue

            # 4. Reconcile existing orders for all market grid strategies first
            for s in market_grid_strats:
                cur_p = prices.get(s.ticker)
                if cur_p is not None:
                    s._verify_sell_executions(cur_p)
                s._verify_buy_executions()

            # 5. Calculate previous month (YYYY-MM)
            if now_market.month == 1:
                prev_year = now_market.year - 1
                prev_month = 12
            else:
                prev_year = now_market.year
                prev_month = now_market.month - 1
            prev_month_str = f"{prev_year:04d}-{prev_month:02d}"

            # 6. Aggregate previous month's net profit across all grid tickers in this market
            grid_symbols = list(set(s.ticker for s in market_grid_strats))
            market_profit = self.db_manager.get_grid_monthly_profit(grid_symbols, prev_month_str)

            logger.info(
                f"Market [{market}] - Unified Monthly Stop Loss Check: First trading day of {current_month_str} ({today_str}). "
                f"Tickers: {grid_symbols} | Previous Month ({prev_month_str}) Aggregate Grid Profit: {market_profit:.2f}"
            )

            if market_profit <= 0:
                logger.info(
                    f"Market [{market}] - Total grid profit for {prev_month_str} is {market_profit:.2f} <= 0. "
                    f"No stop loss budget available. Marking monthly stop loss as completed."
                )
                self.db_manager.save_grid_session_state(market, today_str)
                continue

            # 7. Calculate loss budget (10% of aggregate grid profit in market)
            loss_rate = 0.10
            min_holding_days = 30
            loss_budget = market_profit * loss_rate

            logger.info(
                f"Market [{market}] - Monthly Stop Loss Active! Aggregate Profit: {market_profit:.2f} | "
                f"10% Loss Budget: {loss_budget:.2f} | Min Holding Days: {min_holding_days}"
            )

            # 8. Pool all eligible positions (holding >= 30 days and in loss) across all tickers in this market
            now_utc = datetime.now(pytz.utc).replace(tzinfo=None)
            candidates = []

            for strat in market_grid_strats:
                ticker_price = prices.get(strat.ticker)
                if ticker_price is None or ticker_price <= 0:
                    continue

                for order_id, order in list(strat.incomplete_orders.items()):
                    ordered_at_str = order.get("orderedAt") or order.get("ordered_at")
                    ordered_dt = strat._parse_order_time(ordered_at_str)
                    if not ordered_dt:
                        continue

                    holding_days = (now_utc - ordered_dt).total_seconds() / 86400.0
                    if holding_days < min_holding_days:
                        continue

                    buy_price = float(order.get("buyPrice") or order.get("buy_price", 0.0))
                    qty = float(order.get("quantity", 0.0))

                    if buy_price <= ticker_price:
                        continue

                    expected_loss = (buy_price - ticker_price) * qty
                    candidates.append({
                        "strategy": strat,
                        "order": order,
                        "order_id": order_id,
                        "symbol": strat.ticker,
                        "buy_price": buy_price,
                        "current_price": ticker_price,
                        "qty": qty,
                        "ordered_dt": ordered_dt,
                        "holding_days": holding_days,
                        "expected_loss": expected_loss
                    })

            if not candidates:
                logger.info(
                    f"Market [{market}] - No aged positions (>= {min_holding_days} days) incurring loss "
                    f"across any grid tickers. Monthly stop loss completed."
                )
                self.db_manager.save_grid_session_state(market, today_str)
                continue

            # Sort positions across all tickers by ordered_dt ascending (가장 오래된 매물부터)
            candidates.sort(key=lambda x: x["ordered_dt"])
            logger.info(
                f"Market [{market}] - Found {len(candidates)} eligible aged position(s) across all grid tickers."
            )

            # 9. Sequentially execute stop loss across tickers until cumulative loss >= loss_budget
            total_loss = 0.0
            for cand in candidates:
                strat = cand["strategy"]
                order = cand["order"]
                order_id = cand["order_id"]
                buy_price = cand["buy_price"]
                ticker_price = cand["current_price"]
                symbol = cand["symbol"]

                logger.info(
                    f"Market [{market}] - Processing Stop Loss for [{symbol}] Order: {order_id} | "
                    f"Holding: {cand['holding_days']:.1f}d | Buy: {buy_price:.2f} | Current: {ticker_price:.2f} | "
                    f"Expected Loss: {cand['expected_loss']:.2f}"
                )

                actual_sell_price, filled_qty = strat._execute_single_stop_loss(order, ticker_price)

                if filled_qty > 0 and actual_sell_price is not None:
                    realized_loss = (buy_price - actual_sell_price) * filled_qty
                    if realized_loss > 0:
                        total_loss += realized_loss
                    logger.info(
                        f"Market [{market}] - Stop Loss filled for [{symbol}] {order_id} at {actual_sell_price:.2f}. "
                        f"Realized Loss: {realized_loss:.2f} | Cumulative Loss: {total_loss:.2f} / Budget: {loss_budget:.2f}"
                    )
                else:
                    logger.warning(f"Market [{market}] - Stop Loss order [{symbol}] {order_id} was not filled.")

                # 매도 후 손실금액이 이전월 수익의 10%를 넘으면 중단
                if total_loss >= loss_budget:
                    logger.info(
                        f"Market [{market}] - Cumulative stop loss ({total_loss:.2f}) reached or exceeded "
                        f"10% profit budget ({loss_budget:.2f}). Stopping monthly stop loss for {market} market."
                    )
                    break
                else:
                    logger.info(
                        f"Market [{market}] - Cumulative loss ({total_loss:.2f}) < budget ({loss_budget:.2f}). "
                        f"Proceeding to check next oldest position across tickers..."
                    )

            # 10. Mark execution complete for this market
            self.db_manager.save_grid_session_state(market, today_str)
            logger.info(
                f"Market [{market}] - Unified monthly stop loss completed for {today_str}. "
                f"Total realized loss: {total_loss:.2f}."
            )

    def run_one_iteration(self):
        """
        Executes one iteration of trading logic for all active strategy instances.
        """
        try:
            if reload_config_if_changed():
                self.initialize_strategies()
                self.initialize_state()

            # Filter strategy instances that have active market sessions
            active_keys = [
                key for key, strat in self.strategies.items() 
                if self.is_market_active_for_strategy(strat)
            ]
            
            # Segregate enabled and disabled (sell-only) instances
            enabled_keys = [k for k in active_keys if self.strategies[k].config.get("enabled", True)]
            sell_only_keys = [k for k in active_keys if not self.strategies[k].config.get("enabled", True)]
            
            # Log current times for user convenience
            tz_est = pytz.timezone("America/New_York")
            now_est = datetime.now(tz_est)
            tz_kst = pytz.timezone("Asia/Seoul")
            now_kst = datetime.now(tz_kst)
            logger.info(
                f"Market Check Tick | New York Time: {now_est.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"Korea Time: {now_kst.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"Active Instances (Enabled): {enabled_keys} | Active Instances (Sell-only): {sell_only_keys}"
            )
            
            if not active_keys:
                logger.info("No active market sessions for any configured strategies right now.")
                return

            # Collect unique ticker symbols from active instances to query prices efficiently in batch
            unique_active_tickers = list(set(self.strategies[k].ticker for k in active_keys))
            logger.info(f"Starting batch trading iteration for unique tickers: {unique_active_tickers}")
            
            prices = self.api_client.get_current_prices(unique_active_tickers)

            # Unified monthly stop loss for GRID strategies (executed prior to regular buys)
            self.process_monthly_grid_stop_loss(prices)
            
            # Run each active strategy instance sequentially
            for key in active_keys:
                strategy = self.strategies[key]
                price = prices.get(strategy.ticker)
                if price is None:
                    logger.warning(f"Skipping instance [{key}] - Live market price is unavailable for ticker [{strategy.ticker}].")
                    continue
                    
                strategy_name = strategy.config.get("strategy", "GRID")
                logger.info(f"Processing Strategy [{strategy_name}] | Instance: {key} | Ticker: {strategy.ticker} | Live Price: {price:.2f}")
                
                try:
                    strategy.evaluate(price)
                except Exception as strat_err:
                    logger.error(
                        f"Error evaluating strategy instance [{key}] ({strategy_name} on {strategy.ticker}): {strat_err}",
                        exc_info=True
                    )
                
            logger.info("Completed batch trading iteration.")
            
        except Exception as e:
            logger.exception(f"Unexpected error in batch trading loop iteration: {e}")

# Backward compatibility alias
GridTrader = TradeBot
