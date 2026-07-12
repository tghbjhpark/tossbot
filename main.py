import time
import sys
import signal
import logging
from config import POLLING_INTERVAL, TICKERS
from sqlite_manager import SQLiteManager
from toss_api import TossAPIClient
from trader import TradeBot

logger = logging.getLogger("TossTradeBot.Main")

# Global flag for shutdown handling
running = True

def handle_shutdown(signum, frame):
    global running
    logger.info(f"Signal received ({signum}). Initiating graceful shutdown...")
    running = False

def main():
    global running
    
    # Register signal handlers for Docker lifecycle management
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    logger.info("==============================================")
    logger.info("  Toss Securities Grid Trading Bot Starting   ")
    logger.info("==============================================")
    
    # 1. Initialize SQLite Connection
    db_manager = SQLiteManager()
    db_ok = db_manager.initialize()
    if not db_ok:
        logger.error("Failed to initialize SQLite database connection. Exiting bot.")
        sys.exit(1)
        
    # 2. Initialize Toss OpenAPI Client
    api_client = TossAPIClient()
    try:
        # Fetch token initially to verify credentials on startup
        api_client.get_access_token()
    except Exception as e:
        logger.error(f"Failed to fetch initial Toss OpenAPI token: {e}. Exiting bot.")
        sys.exit(1)
        
    # 3. Instantiate Trader and load caching state
    trader = TradeBot(api_client, db_manager)
    trader.initialize_state()
    
    logger.info(f"Bot successfully started. Target Stocks: {TICKERS}. Loop interval: {POLLING_INTERVAL} seconds.")
    
    # 4. Main Event Loop
    while running:
        loop_start = time.time()
        
        logger.debug("Executing core scheduler tick...")
        trader.run_one_iteration()
        
        # Calculate execution time to avoid scheduling drift
        elapsed = time.time() - loop_start
        sleep_time = max(1.0, POLLING_INTERVAL - elapsed)
        
        # Sleep and yield CPU. Periodically check the 'running' flag to shut down quickly.
        sleep_end = time.time() + sleep_time
        while time.time() < sleep_end and running:
            time.sleep(0.5)

    logger.info("==============================================")
    logger.info("  Toss Securities Grid Trading Bot Stopped    ")
    logger.info("==============================================")

if __name__ == "__main__":
    main()
