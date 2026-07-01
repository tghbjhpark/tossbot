import os
import json
import logging
from dotenv import load_dotenv

# Load local environment variables if a .env file exists
load_dotenv()

# Logger settings
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("TossTradeBot.Config")

# Toss Securities API configuration
TOSS_CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "")
TOSS_CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "")
TOSS_ACCOUNT_SEQ = os.getenv("TOSS_ACCOUNT_SEQ", "")
TOSS_BASE_URL = os.getenv("TOSS_BASE_URL", "https://openapi.tossinvest.com")

if not TOSS_CLIENT_ID or not TOSS_CLIENT_SECRET:
    logger.warning("TOSS_CLIENT_ID or TOSS_CLIENT_SECRET is not set in environment variables.")
if not TOSS_ACCOUNT_SEQ:
    logger.warning("TOSS_ACCOUNT_SEQ is not set. Real trading will fail without X-Tossinvest-Account header.")

# SQLite configuration
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "data/toss_trade_bot.db")

# Configuration File Path
TICKER_JSON_PATH = os.getenv("TICKER_JSON_PATH", "config/ticker.json")

# Fallback defaults from env (backward compatibility)
TICKERS_ENV = os.getenv("TICKERS", os.getenv("TICKER", "SOXL")).upper()
TICKERS_LIST = [t.strip() for t in TICKERS_ENV.split(",") if t.strip()]
if not TICKERS_LIST:
    TICKERS_LIST = ["SOXL"]

DEFAULT_YIELD_TARGET = float(os.getenv("YIELD_TARGET", "0.02"))
DEFAULT_GRID_INTERVAL = float(os.getenv("GRID_INTERVAL", "0.01"))
DEFAULT_BUY_QTY = int(os.getenv("BUY_QTY", "1"))
DEFAULT_BUY_MODE = os.getenv("BUY_MODE", "AMOUNT").upper()
DEFAULT_BUY_AMOUNT = float(os.getenv("BUY_AMOUNT", "10.0"))

TICKER_CONFIGS = {}
TICKERS = []

# Load config from ticker.json
if os.path.exists(TICKER_JSON_PATH):
    try:
        with open(TICKER_JSON_PATH, "r", encoding="utf-8") as f:
            configs = json.load(f)
            if isinstance(configs, list):
                for item in configs:
                    ticker = item.get("ticker", "").upper().strip()
                    if not ticker:
                        continue
                    market = item.get("market", "KR" if ticker.isdigit() else "US").upper()
                    TICKER_CONFIGS[ticker] = {
                        "ticker": ticker,
                        "market": market,
                        "buy_mode": item.get("buy_mode", "QTY" if market == "KR" else "AMOUNT").upper(),
                        "buy_qty": int(item.get("buy_qty", 1)),
                        "buy_amount": float(item.get("buy_amount", 10.0)),
                        "yield_target": float(item.get("yield_target", 0.02)),
                        "grid_interval": float(item.get("grid_interval", 0.01)),
                        "enabled": bool(item.get("enabled", True))
                    }
            elif isinstance(configs, dict):
                for ticker, item in configs.items():
                    ticker = ticker.upper().strip()
                    market = item.get("market", "KR" if ticker.isdigit() else "US").upper()
                    TICKER_CONFIGS[ticker] = {
                        "ticker": ticker,
                        "market": market,
                        "buy_mode": item.get("buy_mode", "QTY" if market == "KR" else "AMOUNT").upper(),
                        "buy_qty": int(item.get("buy_qty", 1)),
                        "buy_amount": float(item.get("buy_amount", 10.0)),
                        "yield_target": float(item.get("yield_target", 0.02)),
                        "grid_interval": float(item.get("grid_interval", 0.01)),
                        "enabled": bool(item.get("enabled", True))
                    }
        TICKERS = list(TICKER_CONFIGS.keys())
        logger.info(f"Loaded {len(TICKERS)} ticker configurations from {TICKER_JSON_PATH}.")
    except Exception as e:
        logger.error(f"Error loading {TICKER_JSON_PATH}: {e}. Falling back to environment variables.")

# If ticker.json does not exist or failed to load, create it using env defaults
if not TICKER_CONFIGS:
    logger.info(f"Generating default {TICKER_JSON_PATH} from environment variables...")
    default_configs = []
    for ticker in TICKERS_LIST:
        market = "KR" if ticker.isdigit() else "US"
        config_item = {
            "ticker": ticker,
            "market": market,
            "buy_mode": DEFAULT_BUY_MODE,
            "buy_qty": DEFAULT_BUY_QTY,
            "buy_amount": DEFAULT_BUY_AMOUNT,
            "yield_target": DEFAULT_YIELD_TARGET,
            "grid_interval": DEFAULT_GRID_INTERVAL,
            "enabled": True
        }
        default_configs.append(config_item)
        TICKER_CONFIGS[ticker] = config_item
    
    TICKERS = list(TICKER_CONFIGS.keys())
    try:
        with open(TICKER_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(default_configs, f, indent=2, ensure_ascii=False)
        logger.info(f"Successfully created default {TICKER_JSON_PATH}.")
    except Exception as e:
        logger.error(f"Failed to write default {TICKER_JSON_PATH}: {e}")

# Polling Interval (in seconds)
POLLING_INTERVAL = int(os.getenv("POLLING_INTERVAL", "60"))

# Record initial file modification time for reload comparison
_last_mtime = 0
if os.path.exists(TICKER_JSON_PATH):
    try:
        _last_mtime = os.path.getmtime(TICKER_JSON_PATH)
    except Exception:
        pass

def reload_config_if_changed() -> bool:
    """
    ticker.json 파일의 수정 시간을 감지하여, 변경 시 전역 TICKERS 및 TICKER_CONFIGS를 동적으로 로드합니다.
    변경 사항이 발생하여 리로드를 수행한 경우 True를 반환합니다.
    """
    global TICKERS, TICKER_CONFIGS, _last_mtime
    if not os.path.exists(TICKER_JSON_PATH):
        return False
        
    try:
        current_mtime = os.path.getmtime(TICKER_JSON_PATH)
        if current_mtime == _last_mtime:
            return False
            
        with open(TICKER_JSON_PATH, "r", encoding="utf-8") as f:
            configs = json.load(f)
            
        new_configs = {}
        items = configs if isinstance(configs, list) else configs.values()
        
        for item in items:
            ticker = item.get("ticker", "").upper().strip()
            if not ticker:
                continue
            market = item.get("market", "KR" if ticker.isdigit() else "US").upper()
            enabled = item.get("enabled", True)
                
            new_configs[ticker] = {
                "ticker": ticker,
                "market": market,
                "buy_mode": item.get("buy_mode", "QTY" if market == "KR" else "AMOUNT").upper(),
                "buy_qty": int(item.get("buy_qty", 1)),
                "buy_amount": float(item.get("buy_amount", 10.0)),
                "yield_target": float(item.get("yield_target", 0.02)),
                "grid_interval": float(item.get("grid_interval", 0.01)),
                "enabled": bool(enabled)
            }
            
        # 전역 객체 동적 업데이트 (참조 유지를 위해 clear 후 update)
        TICKER_CONFIGS.clear()
        TICKER_CONFIGS.update(new_configs)
        
        TICKERS.clear()
        TICKERS.extend(list(TICKER_CONFIGS.keys()))
        
        _last_mtime = current_mtime
        logger.info(f"Dynamically reloaded configurations. Active Tickers: {TICKERS}")
        return True
    except Exception as e:
        logger.error(f"Error during dynamic config reload: {e}")
        return False

logger.info(
    f"Configuration Loaded: TICKERS={TICKERS}, TICKER_CONFIGS={TICKER_CONFIGS}, "
    f"POLLING_INTERVAL={POLLING_INTERVAL}s"
)
