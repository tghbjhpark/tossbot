import time
import logging
from datetime import datetime, timedelta
import pytz

from config import TICKERS, TICKER_CONFIGS, reload_config_if_changed
from sqlite_manager import SQLiteManager
from toss_api import TossAPIClient

logger = logging.getLogger("TossTradeBot.Trader")

class GridTrader:
    """
    Main Grid Trading orchestrator. Coordinates current prices,
    order states, memory cache, Firebase syncing, and scheduling conditions.
    Supports trading multiple tickers concurrently.
    """
    def __init__(self, api_client: TossAPIClient, db_manager: SQLiteManager):
        self.api_client = api_client
        self.db_manager = db_manager
        self.ticker_configs = TICKER_CONFIGS
        
        # Nested memory caches: ticker -> {orderId -> order details}
        self.incomplete_orders = {ticker: {} for ticker in TICKERS}
        self.pending_buy_orders = {ticker: {} for ticker in TICKERS}
        
        # Cooldown state for consecutive buys per ticker
        # Structure: { ticker: { "consecutive_buys": int, "cooldown_until": datetime or None } }
        self.cooldown_states = {}

    def initialize_state(self):
        """
        Loads the initial state from SQLite database. Run once on bot startup.
        Groups loaded flat database records by stock ticker.
        """
        logger.info("Initializing in-memory state from SQLite...")
        raw_incomplete = self.db_manager.get_incomplete_orders()
        raw_pending = self.db_manager.get_pending_buy_orders()
        
        # Reset structures
        self.incomplete_orders = {ticker: {} for ticker in TICKERS}
        self.pending_buy_orders = {ticker: {} for ticker in TICKERS}
        
        # Populate and group active sell orders
        for oid, order in raw_incomplete.items():
            symbol = order.get("symbol")
            if symbol in self.incomplete_orders:
                self.incomplete_orders[symbol][oid] = order
            else:
                logger.debug(
                    f"Ignored incomplete order {oid} for symbol {symbol} (not in active TICKERS config)."
                )
                
        # Populate and group pending buy orders
        for oid, order in raw_pending.items():
            symbol = order.get("symbol")
            if symbol in self.pending_buy_orders:
                self.pending_buy_orders[symbol][oid] = order
            else:
                logger.debug(
                    f"Ignored pending buy order {oid} for symbol {symbol} (not in active TICKERS config)."
                )
        
        # Display Diagnostics
        for ticker in TICKERS:
            sells = self.incomplete_orders[ticker]
            buys = self.pending_buy_orders[ticker]
            logger.info(f"Ticker [{ticker}] | Active Sells: {len(sells)} | Pending Buys: {len(buys)}")
            for oid, order in sells.items():
                order_type = "Synthetic Sell" if order.get("isSynthetic") else "Limit Sell"
                logger.info(
                    f"  Sell Order ({order_type}): ID={oid}, TargetPrice={order.get('price')}, "
                    f"BuyPrice={order.get('buyPrice')}, Qty={order.get('quantity')}"
                )
            for oid, order in buys.items():
                logger.info(f"  Pending Buy: ID={oid}, Price={order.get('price')}, Qty={order.get('quantity')}")

    def _is_in_cooldown(self, ticker: str) -> bool:
        """
        Checks if the ticker is currently locked under the consecutive buy cooldown limit.
        Resets state if the cooldown period has expired.
        """
        config = self.ticker_configs.get(ticker, {})
        max_buys = config.get("max_consecutive_buys")
        cooldown_mins = config.get("cooldown_minutes")
        
        if max_buys is None or cooldown_mins is None:
            return False
            
        state = self.cooldown_states.setdefault(ticker, {"consecutive_buys": 0, "cooldown_until": None})
        cooldown_until = state["cooldown_until"]
        
        if cooldown_until:
            if datetime.now() < cooldown_until:
                logger.info(
                    f"Ticker [{ticker}] - Buy blocked. Consecutive buys: {state['consecutive_buys']}/{max_buys}. "
                    f"Cooldown active until {cooldown_until.strftime('%H:%M:%S')}"
                )
                return True
            else:
                # Cooldown expired! Reset counter.
                logger.info(f"Ticker [{ticker}] - Cooldown expired. Resetting consecutive buy counter to 0.")
                state["consecutive_buys"] = 0
                state["cooldown_until"] = None
                
        return False

    def _reset_consecutive_buys(self, ticker: str):
        """
        Resets the consecutive buy counter and lifts any active cooldown upon a successful sell execution.
        """
        if ticker in self.cooldown_states:
            state = self.cooldown_states[ticker]
            if state["consecutive_buys"] > 0 or state["cooldown_until"] is not None:
                logger.info(f"Ticker [{ticker}] - Successful sell execution detected. Resetting consecutive buys and lifting cooldown.")
                state["consecutive_buys"] = 0
                state["cooldown_until"] = None

    def is_market_active_for_ticker(self, ticker: str) -> bool:
        """
        Checks if the trading session is active for the given ticker,
        distinguishing between US and Korean stock markets.
        """
        config = self.ticker_configs.get(ticker, {})
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
        Executes one iteration of the grid trading strategy for all configured tickers.
        Only runs for tickers whose markets are currently active.
        """
        try:
            # Check for config file changes and reload dynamically
            if reload_config_if_changed():
                for ticker in TICKERS:
                    if ticker not in self.incomplete_orders:
                        self.incomplete_orders[ticker] = {}
                    if ticker not in self.pending_buy_orders:
                        self.pending_buy_orders[ticker] = {}

            # Filter tickers that have active market sessions and are enabled
            active_tickers = [
                t for t in TICKERS 
                if self.is_market_active_for_ticker(t) and self.ticker_configs.get(t, {}).get("enabled", True)
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
            
            # Run grid strategy sequentially for each active ticker
            for ticker in active_tickers:
                price = prices.get(ticker)
                if price is None:
                    logger.warning(f"Skipping ticker [{ticker}] - Live market price is unavailable.")
                    continue
                    
                logger.info(f"Processing Grid | Ticker: {ticker} | Live Price: {price:.2f}")
                
                # Step 1: Reconcile Sell Executions
                self._verify_sell_executions(ticker, price)
                
                # Step 2: Reconcile Pending Buy Executions
                self._verify_buy_executions(ticker)
                
                # Step 3: Evaluate grid buying triggers
                self._evaluate_grid_buying(ticker, price)
                
            logger.info("Completed batch trading iteration.")
            
        except Exception as e:
            logger.exception(f"Unexpected error in batch trading loop iteration: {e}")

    def _verify_sell_executions(self, ticker: str, current_price: float):
        """
        Scans incomplete (standby) sell orders.
        1. If exchange_order_id exists, checks execution state.
           - If filled, removes it.
           - If unfilled, cancels it at the end of the tick.
        2. If no exchange_order_id exists and current_price >= target_price,
           places a real limit/market sell order and updates exchange_order_id.
        """
        ticker_sells = self.incomplete_orders.get(ticker, {})
        if not ticker_sells:
            return

        config = self.ticker_configs.get(ticker, {})
        market = config.get("market", "US")
        buy_mode = config.get("buy_mode", "AMOUNT").upper()

        # Iterate over copy to allow safe mutation
        sell_order_ids = list(ticker_sells.keys())
        
        for order_id in sell_order_ids:
            order = ticker_sells.get(order_id)
            if not order:
                continue
                
            exchange_order_id = order.get("exchangeOrderId", "")
            target_price = float(order["price"])
            qty = float(order["quantity"])
            
            if exchange_order_id:
                # A. 실제 거래소 매도 주문이 진행 중인 상태 -> 체결 여부 확인 및 시간 초과 취소 처리
                try:
                    details = self.api_client.get_order_details(exchange_order_id)
                    status = details.get("status")
                    logger.info(f"  Checking exchange sell order {exchange_order_id} | Status: {status}")
                    
                    if status == "FILLED":
                        logger.info(f"$$$$ SELL ORDER FILLED $$$$ | Ticker: {ticker} | ID: {order_id} | Price: {target_price:.2f}")
                        self.db_manager.remove_incomplete_order(order_id)
                        self._reset_consecutive_buys(ticker)
                        if order_id in self.incomplete_orders[ticker]:
                            del self.incomplete_orders[ticker][order_id]
                            
                    elif status in ["CANCELED", "REJECTED"]:
                        # 취소된 경우 부분 체결 수량이 있는지 검증
                        self._process_canceled_sell(ticker, order_id, order, details)
                        
                    elif status in ["PENDING_CANCEL", "PENDING_REPLACE"]:
                        logger.info(f"  Exchange sell order {exchange_order_id} is in intermediate state {status}. Waiting...")
                        
                    elif status in ["PENDING", "PARTIAL_FILLED"]:
                        # 1턴 대기 후 미체결 상태 -> 취소 요청
                        logger.info(f"  Exchange sell order {exchange_order_id} is unfilled ({status}). Cancelling...")
                        try:
                            self.api_client.cancel_order(exchange_order_id)
                            new_details = self.api_client.get_order_details(exchange_order_id)
                            new_status = new_details.get("status")
                            
                            if new_status in ["CANCELED", "REJECTED"]:
                                self._process_canceled_sell(ticker, order_id, order, new_details)
                            else:
                                logger.warning(f"  Failed to confirm cancel for sell {exchange_order_id} immediately. State: {new_status}")
                        except Exception as cancel_err:
                            logger.error(f"  Failed to cancel exchange sell order {exchange_order_id}: {cancel_err}")
                            
                            # cancel-restricted 에러 처리: 이미 취소/체결 완료된 경우 정돈
                            is_cancel_restricted = False
                            if hasattr(cancel_err, 'response') and cancel_err.response is not None:
                                try:
                                    err_json = cancel_err.response.json()
                                    if err_json.get("error", {}).get("code") == "cancel-restricted":
                                        is_cancel_restricted = True
                                except Exception:
                                    pass
                                    
                            if is_cancel_restricted:
                                logger.info(f"  Sell order {exchange_order_id} cancel restricted. Fetching latest details...")
                                try:
                                    check_details = self.api_client.get_order_details(exchange_order_id)
                                    check_status = check_details.get("status")
                                    
                                    if check_status == "FILLED":
                                        logger.info(f"$$$$ SELL ORDER FILLED $$$$ | Ticker: {ticker} | ID: {order_id} | Price: {target_price:.2f}")
                                        self.db_manager.remove_incomplete_order(order_id)
                                        self._reset_consecutive_buys(ticker)
                                        if order_id in self.incomplete_orders[ticker]:
                                            del self.incomplete_orders[ticker][order_id]
                                    else:
                                        logger.info(f"  Treating restricted cancel sell order {exchange_order_id} as CANCELED/inactive.")
                                        self._process_canceled_sell(ticker, order_id, order, check_details)
                                except Exception as check_err:
                                    logger.error(f"  Failed to process details for sell order {exchange_order_id} after restricted cancel: {check_err}")
                                    
                    else:
                        logger.warning(f"  Exchange sell order {exchange_order_id} has unexpected status: {status}. Doing nothing.")
                            
                except Exception as api_err:
                    logger.error(f"  Failed to verify status for exchange sell order {exchange_order_id}: {api_err}")
                    # HTTP 400/404 에러 발생 시, 거래소에 없는 주문이므로 exchangeOrderId를 비우고 가상 매도 대기(Standby)로 환원
                    is_not_found = False
                    if hasattr(api_err, 'response') and api_err.response is not None:
                        if api_err.response.status_code in [400, 404]:
                            is_not_found = True
                    if is_not_found:
                        logger.warning(f"  Exchange sell order {exchange_order_id} not found on exchange. Resetting back to standby.")
                        self.db_manager.update_incomplete_order_exchange_id(order_id, "")
                        order["exchangeOrderId"] = ""
            
            else:
                # B. 대기 상태이며 실제 주문이 나가지 않은 상태 -> 가격 도달 시 실제 매도 주문 전송
                if current_price >= target_price:
                    logger.info(
                        f"Target met for [{ticker}] sell! Current {current_price:.2f} >= Target {target_price:.2f}. "
                        f"Submitting real sell order..."
                    )
                    try:
                        if buy_mode == "AMOUNT":
                            # 소수점 금액 기반은 수량 시장가 매도 진행
                            sell_res = self.api_client.place_market_order(ticker, "SELL", qty)
                        else:
                            # 일반 수량 지정가 매도 진행
                            sell_res = self.api_client.place_limit_order(ticker, "SELL", int(qty), target_price)
                            
                        new_exchange_id = sell_res["orderId"]
                        logger.info(f"  Submitted real sell order to exchange. Exchange ID: {new_exchange_id}")
                        
                        # DB 및 메모리 캐시 정보 업데이트
                        self.db_manager.update_incomplete_order_exchange_id(order_id, new_exchange_id)
                        order["exchangeOrderId"] = new_exchange_id
                        
                        # 즉시 체결 확인을 위해 3회 즉시 폴링 (각 1.5초 간격)
                        for attempt in range(3):
                            time.sleep(1.5)
                            details = self.api_client.get_order_details(new_exchange_id)
                            status = details.get("status")
                            logger.info(f"  Polling fresh sell [{ticker}] | Attempt {attempt + 1}/3 | Status: {status}")
                            if status == "FILLED":
                                logger.info(f"$$$$ SELL ORDER FILLED IN POLLING $$$$ | Ticker: {ticker} | ID: {order_id} | Price: {target_price:.2f}")
                                self.db_manager.remove_incomplete_order(order_id)
                                self._reset_consecutive_buys(ticker)
                                if order_id in self.incomplete_orders[ticker]:
                                    del self.incomplete_orders[ticker][order_id]
                                break
                            elif status in ["CANCELED", "REJECTED"]:
                                self._process_canceled_sell(ticker, order_id, order, details)
                                break
                                
                    except Exception as place_err:
                        logger.error(f"  Failed to submit real sell order for {order_id}: {place_err}")

    def _process_canceled_sell(self, ticker: str, order_id: str, order: dict, details: dict):
        """
        Handles canceled exchange sell orders. Supports partial fill management.
        """
        execution = details.get("execution", {})
        filled_qty_str = execution.get("filledQuantity", "0")
        filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
        
        if filled_qty > 0:
            logger.info(f"Sell order {order_id} cancel confirmed with partial execution of {filled_qty} shares.")
            buy_price = float(order["buyPrice"])
            sell_price = float(order["price"])
            total_qty = float(order["quantity"])
            remaining_qty = total_qty - filled_qty
            
            # A. 체결 완료된 부분 정산
            self.db_manager.update_incomplete_order_quantity(order_id, filled_qty)
            self.db_manager.remove_incomplete_order(order_id)
            self._reset_consecutive_buys(ticker)
            if order_id in self.incomplete_orders[ticker]:
                del self.incomplete_orders[ticker][order_id]
                
            # B. 남은 부분 신규 생성
            if remaining_qty > 0.000001:
                new_sell_id = f"synthetic_sell_rem_{int(time.time())}_{order_id}"
                config = self.ticker_configs.get(ticker, {})
                buy_mode = config.get("buy_mode", "AMOUNT").upper()
                if buy_mode == "AMOUNT":
                    qty_formatted = f"{remaining_qty:.6f}".rstrip('0').rstrip('.')
                else:
                    qty_formatted = str(int(remaining_qty))
                    
                new_sell_data = {
                    "orderId": new_sell_id,
                    "symbol": ticker,
                    "price": order["price"],
                    "quantity": qty_formatted,
                    "buyPrice": order["buyPrice"],
                    "orderedAt": order["orderedAt"],
                    "isSynthetic": True,
                    "exchangeOrderId": ""
                }
                
                self.db_manager.add_incomplete_order(new_sell_id, new_sell_data)
                self.incomplete_orders[ticker][new_sell_id] = new_sell_data
                logger.info(f"  Re-registered remaining {qty_formatted} shares as new synthetic sell {new_sell_id}")
        else:
            # 0주 체결 완전 취소: exchange_order_id 리셋
            logger.info(f"Sell order {order_id} cancel confirmed with 0 execution. Resetting to standby.")
            self.db_manager.update_incomplete_order_exchange_id(order_id, "")
            order["exchangeOrderId"] = ""

    def _verify_buy_executions(self, ticker: str):
        """
        Scans pending buy orders for a ticker. If an order is filled, places the corresponding target sell.
        """
        ticker_pending = self.pending_buy_orders.get(ticker, {})
        if not ticker_pending:
            return

        logger.info(f"Ticker [{ticker}] - Checking {len(ticker_pending)} pending buy orders...")
        pending_ids = list(ticker_pending.keys())
        
        for order_id in pending_ids:
            try:
                details = self.api_client.get_order_details(order_id)
                status = details.get("status")
                logger.info(f"  Checking Buy Order {order_id} | Status: {status}")
                
                if status == "FILLED":
                    self._handle_filled_buy(ticker, order_id, details)
                    
                elif status in ["CANCELED", "REJECTED"]:
                    self._process_canceled_buy(ticker, order_id, details)
                    
                elif status in ["PENDING_CANCEL", "PENDING_REPLACE"]:
                    logger.info(f"  Buy order {order_id} is in intermediate state {status}. Waiting...")
                    
                elif status in ["PENDING", "PARTIAL_FILLED"]:
                    # 다음 턴까지 체결되지 않은 주문은 즉시 취소 요청
                    logger.info(f"  Buy order {order_id} is still pending with status {status}. Cancelling to reload in next tick...")
                    try:
                        self.api_client.cancel_order(order_id)
                        # 취소 요청 후 부분 체결 수량 확인을 위해 재조회
                        new_details = self.api_client.get_order_details(order_id)
                        new_status = new_details.get("status")
                        logger.info(f"  Cancelled status check for {order_id}: {new_status}")
                        
                        if new_status in ["CANCELED", "REJECTED"]:
                            self._process_canceled_buy(ticker, order_id, new_details)
                        else:
                            logger.warning(
                                f"  Failed to confirm cancel for order {order_id} immediately (state: {new_status}). "
                                f"Will retry in next tick."
                            )
                    except Exception as cancel_err:
                        logger.error(f"  Failed to cancel buy order {order_id}: {cancel_err}")
                        
                        # cancel-restricted 에러 처리: 이미 취소/체결 완료된 경우 정돈
                        is_cancel_restricted = False
                        if hasattr(cancel_err, 'response') and cancel_err.response is not None:
                            try:
                                err_json = cancel_err.response.json()
                                if err_json.get("error", {}).get("code") == "cancel-restricted":
                                    is_cancel_restricted = True
                            except Exception:
                                pass
                                
                        if is_cancel_restricted:
                            logger.info(f"  Buy order {order_id} cancel restricted. Fetching latest details...")
                            try:
                                check_details = self.api_client.get_order_details(order_id)
                                check_status = check_details.get("status")
                                execution = check_details.get("execution", {})
                                filled_qty = float(execution.get("filledQuantity", "0") or "0")
                                
                                if check_status == "FILLED" or filled_qty > 0:
                                    logger.info(f"  Treating restricted cancel buy order {order_id} as FILLED/partially filled (qty: {filled_qty})")
                                    self._handle_filled_buy(ticker, order_id, check_details)
                                else:
                                    logger.info(f"  Treating restricted cancel buy order {order_id} as CANCELED/inactive.")
                                    self._process_canceled_buy(ticker, order_id, check_details)
                            except Exception as check_err:
                                logger.error(f"  Failed to process details for buy order {order_id} after restricted cancel: {check_err}")
                                
                else:
                    logger.warning(f"  Buy order {order_id} has unexpected status: {status}. Doing nothing.")
                            
            except Exception as e:
                logger.error(f"Error verifying pending buy order {order_id}: {e}")
                # HTTP 400/404 에러 발생 시, 계좌에 없는 유효하지 않은 주문으로 간주하고 DB/캐시에서 삭제 정돈
                is_not_found = False
                if hasattr(e, 'response') and e.response is not None:
                    if e.response.status_code in [400, 404]:
                        is_not_found = True
                if is_not_found:
                    logger.warning(f"Buy order {order_id} not found on exchange (HTTP {e.response.status_code}). Removing stale record from DB/memory.")
                    self.db_manager.remove_pending_buy_order(order_id)
                    if order_id in self.pending_buy_orders[ticker]:
                        del self.pending_buy_orders[ticker][order_id]

    def _process_canceled_buy(self, ticker: str, order_id: str, details: dict):
        """
        Handles post-cancel cleanup. If there are partially filled shares, places the matching sell order.
        Otherwise, removes it from memory and SQLite.
        """
        execution = details.get("execution", {})
        filled_qty_str = execution.get("filledQuantity", "0")
        filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
        
        if filled_qty > 0:
            logger.info(
                f"Buy order {order_id} was cancelled, but has partial fill of {filled_qty} shares. "
                f"Placing target sell order..."
            )
            partial_details = details.copy()
            partial_details["quantity"] = str(filled_qty)
            if "execution" in partial_details:
                partial_details["execution"]["filledQuantity"] = str(filled_qty)
            self._handle_filled_buy(ticker, order_id, partial_details)
        else:
            logger.info(f"Buy order {order_id} cancelled with 0 execution. Removing.")
            self.db_manager.remove_pending_buy_order(order_id)
            if order_id in self.pending_buy_orders[ticker]:
                del self.pending_buy_orders[ticker][order_id]

    def _handle_filled_buy(self, ticker: str, buy_order_id: str, details: dict):
        """
        Registers a standby target sell order in memory/DB without submitting it to the exchange immediately.
        """
        execution = details.get("execution", {})
        avg_price_str = execution.get("averageFilledPrice")
        buy_price = float(avg_price_str) if avg_price_str else float(details.get("price"))
        
        qty_str = execution.get("filledQuantity") or details.get("quantity")
        if not qty_str or float(qty_str) == 0.0:
            logger.warning(f"Buy order {buy_order_id} filled but execution quantity is zero/null.")
            return
            
        qty = float(qty_str)
        
        # Load ticker-specific configurations
        config = self.ticker_configs.get(ticker, {})
        market = config.get("market", "US")
        yield_target = config.get("yield_target", 0.02)
        buy_mode = config.get("buy_mode", "AMOUNT").upper()
        
        sell_price = buy_price * (1 + yield_target)
        
        logger.info(f"$$$$ BUY ORDER FILLED $$$$ | Ticker: {ticker} | ID: {buy_order_id} | Buy Price: {buy_price:.4f} | Qty: {qty}")
        
        # All sells start as standby synthetic records in SQLite/memory
        sell_order_id = f"synthetic_sell_{buy_order_id}"
        
        if buy_mode == "AMOUNT":
            qty_formatted = f"{qty:.6f}".rstrip('0').rstrip('.')
        else:
            qty_formatted = str(int(qty))
            
        sell_order_data = {
            "orderId": sell_order_id,
            "symbol": ticker,
            "price": self.api_client.format_price(sell_price, market),
            "quantity": qty_formatted,
            "buyPrice": self.api_client.format_price(buy_price, market),
            "orderedAt": datetime.now().isoformat(),
            "isSynthetic": True,
            "exchangeOrderId": ""
        }
        
        self.db_manager.add_incomplete_order(sell_order_id, sell_order_data)
        self.incomplete_orders[ticker][sell_order_id] = sell_order_data
        
        # Clear pending buy
        self.db_manager.remove_pending_buy_order(buy_order_id)
        if buy_order_id in self.pending_buy_orders[ticker]:
            del self.pending_buy_orders[ticker][buy_order_id]
            
        logger.info(f"Registered synthetic target sell order in DB: {sell_order_id} at {sell_price:.2f} for {qty_formatted} shares.")
        
        # Update cooldown state for consecutive buys
        max_buys = config.get("max_consecutive_buys")
        cooldown_mins = config.get("cooldown_minutes")
        if max_buys is not None and cooldown_mins is not None:
            state = self.cooldown_states.setdefault(ticker, {"consecutive_buys": 0, "cooldown_until": None})
            state["consecutive_buys"] += 1
            logger.info(f"Ticker [{ticker}] - Consecutive buy count increased: {state['consecutive_buys']}/{max_buys}")
            
            if state["consecutive_buys"] >= int(max_buys):
                cooldown_until = datetime.now() + timedelta(minutes=int(cooldown_mins))
                state["cooldown_until"] = cooldown_until
                logger.warning(
                    f"Ticker [{ticker}] - Reached maximum consecutive buys ({max_buys}). "
                    f"Cooldown activated. Buying suspended until {cooldown_until.strftime('%Y-%m-%d %H:%M:%S')}."
                )

    def _evaluate_grid_buying(self, ticker: str, current_price: float):
        """
        Compares current price with the lowest active sell order in memory for a ticker.
        Triggers a new grid buy if target is met.
        """
        if self._is_in_cooldown(ticker):
            return
            
        ticker_sells = self.incomplete_orders.get(ticker, {})
        ticker_pending = self.pending_buy_orders.get(ticker, {})
        
        # If there is already a pending buy order, skip evaluation to prevent duplication
        if ticker_pending:
            return

        # Load ticker-specific configurations
        config = self.ticker_configs.get(ticker, {})
        yield_target = config.get("yield_target", 0.02)
        grid_interval = config.get("grid_interval", 0.01)
        fill_grid_on_rise = config.get("fill_grid_on_rise", True)
        
        # Check if grid is empty
        if not ticker_sells:
            logger.info(f"No active sells and no pending buys for [{ticker}]. Placing initial seed buy order...")
            self._place_grid_buy(ticker, current_price)
            return

        # 1. 상승 중 비어있는 그리드 격자 메우기 전략 (fill_grid_on_rise)
        if fill_grid_on_rise:
            target_sell_price = current_price * (1 + yield_target)
            range_min = target_sell_price * (1 - grid_interval)
            range_max = target_sell_price * (1 + grid_interval)
            
            # Check if any incomplete sell order price lies within target_sell_price +- grid_interval
            has_matching_sell = False
            for sell_order in ticker_sells.values():
                sell_p = float(sell_order["price"])
                if range_min <= sell_p <= range_max:
                    has_matching_sell = True
                    break
                    
            if not has_matching_sell:
                logger.warning(
                    f"Ticker [{ticker}] - Rise grid gap detected. No active sell targets around "
                    f"target sell price {target_sell_price:.2f} (Checked range: {range_min:.2f} ~ {range_max:.2f}). "
                    f"Placing chase buy to fill the grid."
                )
                self._place_grid_buy(ticker, current_price)
                return

        # 2. 기존 최저 매도 목표가 대비 하락 매수 (Fall grid buying)
        # Find lowest active target sell price
        sorted_sells = sorted(
            ticker_sells.values(),
            key=lambda x: float(x.get("price", 0.0))
        )
        lowest_sell_order = sorted_sells[0]
        lowest_sell_price = float(lowest_sell_order["price"])
        
        # Load ticker-specific configurations
        config = self.ticker_configs.get(ticker, {})
        yield_target = config.get("yield_target", 0.02)
        grid_interval = config.get("grid_interval", 0.01)
        
        # Calculate target trigger price
        required_drop = yield_target + grid_interval
        trigger_price = lowest_sell_price * (1 - required_drop)
        
        logger.info(
            f"Grid Check [{ticker}] | Lowest Sell: {lowest_sell_price:.2f} | "
            f"Drop Threshold: {required_drop * 100}% | Trigger Buy Price <= {trigger_price:.2f}"
        )
        
        if current_price <= trigger_price:
            logger.info(
                f"Price target met for [{ticker}]! Current {current_price:.2f} is below target {trigger_price:.2f}. "
                f"Triggering buy."
            )
            # Verify if we already have a pending buy near or at this price to avoid duplicates
            for pending_buy in ticker_pending.values():
                p_price = float(pending_buy.get("price", 0.0))
                if abs(p_price - current_price) / current_price < 0.002:
                    logger.info(f"A pending buy order is already open at a similar price for [{ticker}]. Skipping duplicate.")
                    return
            
            self._place_grid_buy(ticker, current_price)
        else:
            logger.info(f"Price target not met for [{ticker}]. No new buy orders triggered.")

    def _place_grid_buy(self, ticker: str, price: float):
        """
        Submits a buy order, registers it under pending buy, and polls for 10 seconds for immediate execution.
        """
        try:
            # Load ticker-specific configurations
            config = self.ticker_configs.get(ticker, {})
            market = config.get("market", "US")
            buy_mode = config.get("buy_mode", "AMOUNT")
            buy_qty = config.get("buy_qty", 1)
            buy_amount = config.get("buy_amount", 10.0)
            
            if buy_mode == "AMOUNT":
                # Submit USD amount-based market order (fractional purchase)
                buy_res = self.api_client.place_amount_market_order(ticker, "BUY", buy_amount)
                buy_order_id = buy_res["orderId"]
                buy_order_data = {
                    "orderId": buy_order_id,
                    "symbol": ticker,
                    "quantity": "0",  # Will be populated once filled
                    "price": self.api_client.format_price(price, market),  # Initial reference price
                    "orderedAt": datetime.now().isoformat(),
                    "isAmountBased": True,
                    "orderAmount": str(buy_amount)
                }
            else:
                # Submit quantity-based limit buy order at current price
                buy_res = self.api_client.place_limit_order(ticker, "BUY", buy_qty, price)
                buy_order_id = buy_res["orderId"]
                buy_order_data = {
                    "orderId": buy_order_id,
                    "symbol": ticker,
                    "quantity": str(buy_qty),
                    "price": self.api_client.format_price(price, market),
                    "orderedAt": datetime.now().isoformat()
                }
            
            # Sync memory and SQLite
            self.db_manager.add_pending_buy_order(buy_order_id, buy_order_data)
            self.pending_buy_orders[ticker][buy_order_id] = buy_order_data
            
            # Poll for immediate execution (up to 5 attempts, every 2s)
            logger.info(f"Buy order placed for [{ticker}]: {buy_order_id}. Polling for immediate fill...")
            for attempt in range(5):
                time.sleep(2)
                try:
                    details = self.api_client.get_order_details(buy_order_id)
                    status = details.get("status")
                    logger.info(f"Polling Buy [{ticker}] | Attempt {attempt + 1}/5 | Status: {status}")
                    
                    if status == "FILLED":
                        self._handle_filled_buy(ticker, buy_order_id, details)
                        break
                    elif status in ["CANCELED", "REJECTED"]:
                        execution = details.get("execution", {})
                        filled_qty_str = execution.get("filledQuantity", "0")
                        filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
                        
                        if filled_qty > 0:
                            logger.info(f"Polling: Buy order cancelled with partial fill of {filled_qty} shares.")
                            partial_details = details.copy()
                            partial_details["quantity"] = str(filled_qty)
                            if "execution" in partial_details:
                                partial_details["execution"]["filledQuantity"] = str(filled_qty)
                            self._handle_filled_buy(ticker, buy_order_id, partial_details)
                        else:
                            logger.info(f"Polling: Buy order closed with 0 execution. Removing from pending.")
                            self.db_manager.remove_pending_buy_order(buy_order_id)
                            if buy_order_id in self.pending_buy_orders[ticker]:
                                del self.pending_buy_orders[ticker][buy_order_id]
                        break
                except Exception as poll_err:
                    logger.error(f"Error checking order status in polling: {poll_err}")
                    
        except Exception as place_err:
            logger.error(f"Failed to place new grid buy order for [{ticker}] at {price:.2f}: {place_err}")
