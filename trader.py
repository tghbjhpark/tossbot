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
        Instantiates/updates Strategy objects for all configured tickers.
        Preserves existing in-memory cooldown or order caches if possible.
        """
        old_strategies = self.strategies
        self.strategies = {}
        
        for ticker, config in TICKER_CONFIGS.items():
            strategy_name = config.get("strategy", "GRID")
            strategy_class = get_strategy_class(strategy_name)
            
            strategy_instance = strategy_class(ticker, self.api_client, self.db_manager, config)
            
            # Carry over active states to prevent losing caches/cooldowns during hot-reload
            if ticker in old_strategies:
                strategy_instance.cooldown_state = old_strategies[ticker].cooldown_state
                strategy_instance.incomplete_orders = old_strategies[ticker].incomplete_orders
                strategy_instance.pending_buy_orders = old_strategies[ticker].pending_buy_orders
                
            self.strategies[ticker] = strategy_instance
            
        logger.info(f"Initialized strategies: {list(self.strategies.keys())}")

    def initialize_state(self):
        """
        Loads active order caches from SQLite and distributes them to matching strategy instances.
        """
        logger.info("Initializing in-memory state from SQLite for strategies...")
        
        # Load GRID data
        raw_grid_incomplete = self.db_manager.get_incomplete_orders()
        raw_grid_pending = self.db_manager.get_pending_buy_orders()
        
        # Load DCA data
        raw_dca_incomplete = self.db_manager.get_dca_incomplete_orders()
        raw_dca_pending = self.db_manager.get_dca_pending_buy_orders()
        
        for ticker, strategy in self.strategies.items():
            strategy_name = strategy.config.get("strategy", "GRID").upper()
            if strategy_name == "DCA":
                strategy.incomplete_orders = {
                    oid: order for oid, order in raw_dca_incomplete.items() if order.get("symbol") == ticker
                }
                strategy.pending_buy_orders = {
                    oid: order for oid, order in raw_dca_pending.items() if order.get("symbol") == ticker
                }
            else: # GRID
                strategy.incomplete_orders = {
                    oid: order for oid, order in raw_grid_incomplete.items() if order.get("symbol") == ticker
                }
                strategy.pending_buy_orders = {
                    oid: order for oid, order in raw_grid_pending.items() if order.get("symbol") == ticker
                }
            strategy.initialize_state()

    def is_market_active_for_ticker(self, ticker: str) -> bool:
        """
        Checks if the trading session is active for the given ticker,
        distinguishing between US and Korean stock markets.
        """
        strategy = self.strategies.get(ticker)
        if not strategy:
            return False
            
        config = strategy.config
        market = config.get("market", "US").upper()
        
        if market == "KR":
            # Korea market hours check: Monday-Friday, 09:00 AM to 03:20 PM KST
            tz_kst = pytz.timezone("Asia/Seoul")
            now_kst = datetime.now(tz_kst)
            
            if now_kst.weekday() >= 5:
                return False
                
            start_time = now_kst.replace(hour=9, minute=0, second=0, microsecond=0)
            end_time = now_kst.replace(hour=15, minute=20, second=0, microsecond=0)
            
            return start_time <= now_kst <= end_time
            
        else:
            # US market hours check
            tz_est = pytz.timezone("America/New_York")
            now_est = datetime.now(tz_est)
            
            if now_est.weekday() >= 5:
                return False
                
            buy_mode = config.get("buy_mode", "AMOUNT").upper()
            if buy_mode == "AMOUNT":
                # 소수점 금액 주문: 09:40 ~ 14:50 (09:30 ~ 15:00 대비 앞뒤 10분 버퍼 적용)
                start_time = now_est.replace(hour=9, minute=40, second=0, microsecond=0)
                end_time = now_est.replace(hour=14, minute=50, second=0, microsecond=0)
                return start_time <= now_est <= end_time
            else:
                # 일반 수량 주문 (QTY): 평일 상시 작동 (시간 제한 없음)
                return True

    def run_one_iteration(self):
        """
        Executes one iteration of trading logic for all active tickers.
        """
        try:
            if reload_config_if_changed():
                self.initialize_strategies()
                self.initialize_state()

            # Filter tickers that have active market sessions and are enabled
            active_tickers = [
                t for t in TICKERS 
                if self.is_market_active_for_ticker(t) and self.strategies[t].config.get("enabled", True)
            ]
            
            # Log current times for user convenience
            tz_est = pytz.timezone("America/New_York")
            now_est = datetime.now(tz_est)
            tz_kst = pytz.timezone("Asia/Seoul")
            now_kst = datetime.now(tz_kst)
            logger.info(
                f"Market Check Tick | New York Time: {now_est.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"Korea Time: {now_kst.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"Active Tickers: {active_tickers}"
            )
            
            if not active_tickers:
                logger.info("No active market sessions for any enabled configured tickers right now.")
                return
 
            logger.info("Starting batch trading iteration for active tickers...")
            
            # Fetch prices for active tickers in a single HTTP batch query
            prices = self.api_client.get_current_prices(active_tickers)
            
            # Run strategy sequentially for each active ticker
            for ticker in active_tickers:
                price = prices.get(ticker)
                if price is None:
                    logger.warning(f"Skipping ticker [{ticker}] - Live market price is unavailable.")
                    continue
                    
                strategy_name = self.strategies[ticker].config.get("strategy", "GRID")
                logger.info(f"Processing Strategy [{strategy_name}] | Ticker: {ticker} | Live Price: {price:.2f}")
                
                self.strategies[ticker].evaluate(price)
                
            logger.info("Completed batch trading iteration.")
            
        except Exception as e:
            logger.exception(f"Unexpected error in batch trading loop iteration: {e}")

# Backward compatibility alias
GridTrader = TradeBot
