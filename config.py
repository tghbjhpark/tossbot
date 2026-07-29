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

def parse_ticker_item(item: dict) -> dict:
    ticker = item.get("ticker", "").upper().strip()
    if not ticker:
        return {}
    strategy = item.get("strategy", "GRID").upper().strip()
    custom_id = item.get("id") or item.get("name")
    if custom_id:
        instance_key = str(custom_id).strip()
    else:
        instance_key = f"{ticker}:{strategy}"

    market = item.get("market", "KR" if ticker.isdigit() else "US").upper()
    return {
        "instance_key": instance_key,
        "ticker": ticker,
        "strategy": strategy,
        "market": market,
        "buy_mode": item.get("buy_mode", "QTY" if market == "KR" else "AMOUNT").upper(),
        "buy_qty": int(item.get("buy_qty", 1)),
        "buy_amount": float(item.get("buy_amount", 10.0)),
        "yield_target": float(item.get("yield_target", 0.02)),
        "grid_interval": float(item.get("grid_interval", 0.01)),
        "enabled": bool(item.get("enabled", True)),
        "max_consecutive_buys": int(item.get("max_consecutive_buys")) if item.get("max_consecutive_buys") is not None else None,
        "cooldown_minutes": int(item.get("cooldown_minutes")) if item.get("cooldown_minutes") is not None else None,
        "fill_grid_on_rise": bool(item.get("fill_grid_on_rise", True)),
        "max_session_buys": int(item.get("max_session_buys", 40)),
        "min_session_buys": int(item.get("min_session_buys", 6)),
        "min_sell_qty": float(item.get("min_sell_qty", 1.0)),
        "stop_loss_count": int(item.get("stop_loss_count", 0)) if item.get("stop_loss_count") is not None else 0,
        "mode": item.get("mode", "ACCUMULATE").upper().strip(),
        "v_target": float(item["v_target"]) if item.get("v_target") is not None else None,
        "pocket_cash": float(item["pocket_cash"]) if item.get("pocket_cash") is not None else None,
        "band_rate": float(item.get("band_rate", 0.15)),
        "cycle_deposit": float(item.get("cycle_deposit", 0.0)),
        "cycle_withdrawal": float(item.get("cycle_withdrawal", 0.0)),
        "cycle_growth_rate": float(item.get("cycle_growth_rate", 0.0025)),
        "g_factor": float(item.get("g_factor", 10.0)) if item.get("g_factor") is not None else 10.0,
        "cycle_days": int(item.get("cycle_days", 10)),
        "min_trade_amount": float(item.get("min_trade_amount", 10.0)),
        "rebalance_hour_us": int(item.get("rebalance_hour_us") or item.get("rebalance_hour") or 11)
    }

def _build_configs_dict(items: list) -> dict:
    new_configs = {}
    for item in items:
        parsed = parse_ticker_item(item)
        if not parsed:
            continue
        base_key = parsed["instance_key"]
        key = base_key
        idx = 1
        while key in new_configs:
            idx += 1
            key = f"{base_key}:{idx}"
        parsed["instance_key"] = key
        new_configs[key] = parsed
    return new_configs

# Load config from ticker.json
if os.path.exists(TICKER_JSON_PATH):
    try:
        with open(TICKER_JSON_PATH, "r", encoding="utf-8") as f:
            configs = json.load(f)
            items = configs if isinstance(configs, list) else list(configs.values())
            TICKER_CONFIGS = _build_configs_dict(items)
        TICKERS = sorted(list(set(cfg["ticker"] for cfg in TICKER_CONFIGS.values())))
        logger.info(f"Loaded {len(TICKER_CONFIGS)} strategy configurations for {len(TICKERS)} unique tickers from {TICKER_JSON_PATH}.")
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
            "strategy": "GRID",
            "market": market,
            "buy_mode": DEFAULT_BUY_MODE,
            "buy_qty": DEFAULT_BUY_QTY,
            "buy_amount": DEFAULT_BUY_AMOUNT,
            "yield_target": DEFAULT_YIELD_TARGET,
            "grid_interval": DEFAULT_GRID_INTERVAL,
            "enabled": True
        }
        default_configs.append(config_item)
    TICKER_CONFIGS = _build_configs_dict(default_configs)
    TICKERS = sorted(list(set(cfg["ticker"] for cfg in TICKER_CONFIGS.values())))
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
            
        items = configs if isinstance(configs, list) else list(configs.values())
        new_configs = _build_configs_dict(items)
            
        # 전역 객체 동적 업데이트 (참조 유지를 위해 clear 후 update)
        TICKER_CONFIGS.clear()
        TICKER_CONFIGS.update(new_configs)
        
        TICKERS.clear()
        TICKERS.extend(sorted(list(set(cfg["ticker"] for cfg in TICKER_CONFIGS.values()))))
        
        _last_mtime = current_mtime
        logger.info(f"Dynamically reloaded configurations. Active Instances: {list(TICKER_CONFIGS.keys())}, Unique Tickers: {TICKERS}")
        return True
    except Exception as e:
        logger.error(f"Error during dynamic config reload: {e}")
        return False

def update_stop_loss_count(key_or_ticker: str, count: int = 0):
    """
    Updates stop_loss_count for instance_key or ticker in TICKER_CONFIGS and persists to TICKER_JSON_PATH.
    """
    target_key = key_or_ticker.strip()
    matching_ticker = ""
    matching_strategy = ""
    
    if target_key in TICKER_CONFIGS:
        TICKER_CONFIGS[target_key]["stop_loss_count"] = count
        matching_ticker = TICKER_CONFIGS[target_key]["ticker"]
        matching_strategy = TICKER_CONFIGS[target_key]["strategy"]
    else:
        # Search by ticker symbol
        for ik, cfg in TICKER_CONFIGS.items():
            if cfg["ticker"] == target_key.upper() or ik == target_key:
                cfg["stop_loss_count"] = count
                matching_ticker = cfg["ticker"]
                matching_strategy = cfg["strategy"]
                break
        
    if os.path.exists(TICKER_JSON_PATH):
        try:
            with open(TICKER_JSON_PATH, "r", encoding="utf-8") as f:
                configs = json.load(f)
            
            is_list = isinstance(configs, list)
            items = configs if is_list else list(configs.values())
            
            for item in items:
                item_ticker = item.get("ticker", "").upper().strip()
                item_strategy = item.get("strategy", "GRID").upper().strip()
                item_id = item.get("id") or item.get("name")
                
                if item_id and str(item_id).strip() == target_key:
                    item["stop_loss_count"] = count
                    break
                elif matching_ticker and item_ticker == matching_ticker and item_strategy == matching_strategy:
                    item["stop_loss_count"] = count
                    break
                elif item_ticker == target_key.upper():
                    item["stop_loss_count"] = count
                    break
                    
            with open(TICKER_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(configs, f, indent=2, ensure_ascii=False)
            
            global _last_mtime
            _last_mtime = os.path.getmtime(TICKER_JSON_PATH)
            logger.info(f"Updated stop_loss_count for [{target_key}] to {count} in {TICKER_JSON_PATH}")
        except Exception as e:
            logger.error(f"Failed to update stop_loss_count in {TICKER_JSON_PATH}: {e}")

logger.info(
    f"Configuration Loaded: TICKERS={TICKERS}, TICKER_CONFIGS={TICKER_CONFIGS}, "
    f"POLLING_INTERVAL={POLLING_INTERVAL}s"
)

