import logging
import time
import math
from datetime import datetime
import pytz
import pandas as pd
import yfinance as yf

from strategies.base import BaseStrategy

logger = logging.getLogger("TossTradeBot.Strategy.CRSI")

class CrsiStrategy(BaseStrategy):
    """
    CrsiStrategy: Implementation of Larry Connors' 2-period RSI Short-Term Swing Strategy.

    Key Rules:
    - Runs ONCE DAILY at 10 minutes before market close (15:50 EST for US / 15:10 KST for KR).
    - Trades ONLY integer share quantities (buy_qty), no fractional trading.
    - Data: Downloads historical daily closes via yfinance and appends current live price.
    - Trend Filter: Current Price > 200-day SMA.
    - Buy Trigger: RSI(2) < rsi_buy (default 10.0) when not holding.
    - Exit Trigger: Current Price > 5-day SMA OR RSI(2) > rsi_sell (default 70.0) OR holding_days >= max_holding_days (default 5).
    """

    def initialize_state(self):
        """
        Loads persistent CRSI session state (last_eval_date, entry_date) and displays diagnostics.
        """
        self.symbol = self.ticker
        state = self.db_manager.get_crsi_session_state(self.symbol)
        self.last_eval_date = state.get("last_eval_date")
        self.entry_date = state.get("entry_date")

        buy_qty = int(self.config.get("buy_qty", 1))
        rsi_buy = float(self.config.get("rsi_buy", 10.0))
        rsi_sell = float(self.config.get("rsi_sell", 70.0))
        sma_trend = int(self.config.get("sma_trend_period", 200))
        sma_exit = int(self.config.get("sma_exit_period", 5))
        max_days = int(self.config.get("max_holding_days", 5))

        logger.info(
            f"CRSI Ticker [{self.ticker}] | Order Qty: {buy_qty} shares | "
            f"RSI Buy < {rsi_buy} | RSI Sell > {rsi_sell} | Trend SMA: {sma_trend}d | "
            f"Exit SMA: {sma_exit}d | Max Holding: {max_days}d | "
            f"Holdings: {len(self.incomplete_orders)} | Pending Buys: {len(self.pending_buy_orders)} | "
            f"Last Eval Date: {self.last_eval_date}"
        )
        for oid, order in self.incomplete_orders.items():
            logger.info(
                f"  CRSI Holding: ID={oid}, BuyPrice={order.get('buyPrice', order.get('price'))}, "
                f"Qty={order.get('quantity')}, HoldingDays={order.get('holdingDays', 0)}"
            )
        for oid, order in self.pending_buy_orders.items():
            logger.info(f"  CRSI Pending Buy: ID={oid}, Price={order.get('price')}, Qty={order.get('quantity')}")

    def _save_session_state(self):
        self.db_manager.save_crsi_session_state(
            symbol=self.symbol,
            last_eval_date=self.last_eval_date,
            entry_date=self.entry_date
        )

    def _is_execution_time(self) -> tuple[bool, str]:
        """
        Checks if current time is within the execution window (10 mins before market close)
        and whether it has already executed today.
        Returns (in_window, today_date_str).
        """
        market = self.config.get("market", "US").upper()
        minutes_before = int(self.config.get("eval_minute_before_close", 10))

        if market == "KR":
            tz_kr = pytz.timezone("Asia/Seoul")
            now = datetime.now(tz_kr)
            today_str = now.strftime("%Y-%m-%d")
            # KR market closes at 15:20 KST for API orders. 10 mins before = 15:10
            close_hour, close_min = 15, 20
            start_min = max(0, close_min - minutes_before)
            start_time = now.replace(hour=15, minute=start_min, second=0, microsecond=0)
            end_time = now.replace(hour=close_hour, minute=close_min, second=0, microsecond=0)
        else:
            tz_us = pytz.timezone("America/New_York")
            now = datetime.now(tz_us)
            today_str = now.strftime("%Y-%m-%d")
            # US regular market closes at 16:00 EST. 10 mins before = 15:50
            close_hour, close_min = 16, 0
            start_min = 60 - minutes_before
            start_time = now.replace(hour=15, minute=start_min, second=0, microsecond=0)
            end_time = now.replace(hour=close_hour, minute=close_min, second=0, microsecond=0)

        # Check weekday (Monday=0 to Friday=4)
        if now.weekday() >= 5:
            return False, today_str

        # Check time window
        in_window = start_time <= now <= end_time
        return in_window, today_str

    def _fetch_indicators(self, current_price: float) -> tuple[float, float, float] | None:
        """
        Fetches 1 year of daily closes from yfinance, appends current_price as today's close,
        and calculates SMA(200), SMA(5), and RSI(2).
        """
        try:
            ticker_symbol = self.ticker
            # For KRX stocks (numeric tickers), append .KS for yfinance
            if ticker_symbol.isdigit():
                ticker_symbol = f"{ticker_symbol}.KS"

            df = yf.download(ticker_symbol, period="1y", interval="1d", progress=False, auto_adjust=True)
            if df.empty or 'Close' not in df:
                logger.warning(f"CRSI [{self.ticker}] - Empty historical data from yfinance.")
                return None

            close_series = df['Close']
            if isinstance(close_series, pd.DataFrame):
                close_series = close_series.iloc[:, 0]

            close_series = close_series.dropna()
            if len(close_series) < int(self.config.get("sma_trend_period", 200)):
                logger.warning(f"CRSI [{self.ticker}] - Insufficient price history ({len(close_series)} bars). Need at least 200 bars.")
                return None

            # Append current live price as today's close
            combined_closes = pd.concat([close_series, pd.Series([current_price])], ignore_index=True)

            # 1. Trend SMA (default 200)
            sma_trend_period = int(self.config.get("sma_trend_period", 200))
            sma_trend = float(combined_closes.rolling(window=sma_trend_period).mean().iloc[-1])

            # 2. Exit SMA (default 5)
            sma_exit_period = int(self.config.get("sma_exit_period", 5))
            sma_exit = float(combined_closes.rolling(window=sma_exit_period).mean().iloc[-1])

            # 3. RSI(2) (Connors simple 2-period average method)
            delta = combined_closes.diff()
            gain = delta.where(delta > 0, 0.0).rolling(window=2).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(window=2).mean()
            rs = gain / (loss + 1e-9)
            rsi_2 = float((100 - (100 / (1 + rs))).iloc[-1])

            return sma_trend, sma_exit, rsi_2
        except Exception as e:
            logger.error(f"CRSI [{self.ticker}] - Failed to compute indicators: {e}")
            return None

    def _verify_crsi_buy_executions(self):
        """
        Reconciles pending CRSI buy orders with Toss OpenAPI.
        """
        if not self.pending_buy_orders:
            return

        pending_ids = list(self.pending_buy_orders.keys())
        for oid in pending_ids:
            order_data = self.pending_buy_orders.get(oid)
            if not order_data:
                continue

            try:
                details = self.api_client.get_order_details(oid)
                status = details.get("status")
                logger.info(f"CRSI [{self.ticker}] Checking Pending Buy Order {oid} | Status: {status}")

                if status == "FILLED":
                    filled_price = self._extract_filled_price(details) or order_data["price"]
                    filled_qty = float(details.get("quantity") or order_data["quantity"])
                    logger.info(f"★★ CRSI BUY FILLED ★★ | {self.ticker} | Qty: {filled_qty} | Price: ${filled_price:.2f}")

                    # Add to incomplete orders (holdings)
                    holding_data = {
                        "orderId": oid,
                        "symbol": self.ticker,
                        "price": str(filled_price),
                        "quantity": str(filled_qty),
                        "buyPrice": str(filled_price),
                        "orderedAt": datetime.now().isoformat(),
                        "holdingDays": 0,
                        "exchangeOrderId": ""
                    }
                    self.db_manager.add_crsi_incomplete_order(oid, holding_data)
                    self.incomplete_orders[oid] = holding_data

                    # Remove from pending
                    self.db_manager.remove_crsi_pending_buy_order(oid)
                    del self.pending_buy_orders[oid]

                    self.entry_date = datetime.now().strftime("%Y-%m-%d")
                    self._save_session_state()

                elif status in ["CANCELED", "REJECTED"]:
                    logger.warning(f"CRSI [{self.ticker}] Buy order {oid} was {status}. Removing from pending.")
                    self.db_manager.remove_crsi_pending_buy_order(oid)
                    del self.pending_buy_orders[oid]

            except Exception as e:
                logger.error(f"CRSI [{self.ticker}] Error verifying pending buy order {oid}: {e}")

    def _verify_crsi_sell_executions(self):
        """
        Reconciles active sell orders with Toss OpenAPI.
        """
        if not self.incomplete_orders:
            return

        holding_ids = list(self.incomplete_orders.keys())
        for oid in holding_ids:
            order_data = self.incomplete_orders.get(oid)
            if not order_data:
                continue

            exchange_order_id = order_data.get("exchangeOrderId")
            if not exchange_order_id:
                continue

            try:
                details = self.api_client.get_order_details(exchange_order_id)
                status = details.get("status")
                logger.info(f"CRSI [{self.ticker}] Checking Sell Order {exchange_order_id} | Status: {status}")

                if status == "FILLED":
                    actual_sell_price = self._extract_filled_price(details) or float(order_data["price"])
                    buy_price = float(order_data.get("buyPrice", order_data.get("price", 0.0)))
                    qty = float(order_data.get("quantity", 1.0))
                    profit = (actual_sell_price - buy_price) * qty
                    holding_days = int(order_data.get("holdingDays", 0))

                    logger.info(
                        f"$$$$ CRSI SELL FILLED $$$$ | {self.ticker} | Qty: {qty} | "
                        f"BuyPrice: ${buy_price:.2f} | SellPrice: ${actual_sell_price:.2f} | "
                        f"Profit: ${profit:.2f} ({(actual_sell_price/buy_price - 1)*100:.2f}%) | HoldingDays: {holding_days}"
                    )

                    # Record trade history
                    self.db_manager.add_crsi_trade_history(
                        symbol=self.ticker,
                        quantity=qty,
                        buy_price=buy_price,
                        sell_price=actual_sell_price,
                        profit=profit,
                        sell_order_id=exchange_order_id,
                        holding_days=holding_days,
                        buy_time=order_data.get("orderedAt")
                    )

                    # Remove from incomplete holdings
                    self.db_manager.remove_crsi_incomplete_order(oid)
                    del self.incomplete_orders[oid]

                    self.entry_date = None
                    self._save_session_state()

                elif status in ["CANCELED", "REJECTED"]:
                    logger.warning(f"CRSI [{self.ticker}] Sell order {exchange_order_id} was {status}. Resetting exchange ID.")
                    order_data["exchangeOrderId"] = ""
                    self.db_manager.update_crsi_incomplete_order_exchange_id(oid, "")

            except Exception as e:
                logger.error(f"CRSI [{self.ticker}] Error verifying sell order {exchange_order_id}: {e}")

    def evaluate(self, current_price: float):
        """
        Main evaluation cycle called on every scheduler iteration.
        """
        # 1. Reconcile any existing pending buy or sell orders
        self._verify_crsi_buy_executions()
        self._verify_crsi_sell_executions()

        # 2. Check if we are inside the 10-minutes-before-close execution window
        in_window, today_str = self._is_execution_time()
        if not in_window:
            return

        # 3. Check if we already evaluated trading actions today
        if self.last_eval_date == today_str:
            logger.debug(f"CRSI [{self.ticker}] Already evaluated for today ({today_str}). Skipping.")
            return

        # 4. Fetch indicators: SMA(200), SMA(5), RSI(2)
        indicators = self._fetch_indicators(current_price)
        if not indicators:
            logger.warning(f"CRSI [{self.ticker}] Could not calculate indicators. Skipping evaluation.")
            return

        sma_200, sma_5, rsi_2 = indicators
        logger.info(
            f"CRSI [{self.ticker}] Evaluation | Live Price: ${current_price:.2f} | "
            f"SMA(200): ${sma_200:.2f} | SMA(5): ${sma_5:.2f} | RSI(2): {rsi_2:.2f}"
        )

        rsi_buy_thresh = float(self.config.get("rsi_buy", 10.0))
        rsi_sell_thresh = float(self.config.get("rsi_sell", 70.0))
        max_holding_days = int(self.config.get("max_holding_days", 5))

        # --- [CASE A: Holding Position -> Evaluate Exit Conditions] ---
        if self.incomplete_orders:
            for oid, order in list(self.incomplete_orders.items()):
                # If there's already an active sell order in flight, don't double sell
                if order.get("exchangeOrderId"):
                    logger.info(f"CRSI [{self.ticker}] Holding {oid} already has pending sell {order.get('exchangeOrderId')}.")
                    continue

                # Increment holding days count
                curr_days = int(order.get("holdingDays", 0)) + 1
                order["holdingDays"] = curr_days
                self.db_manager.update_crsi_incomplete_order_days(oid, curr_days)

                qty = int(float(order.get("quantity", 1)))
                buy_price = float(order.get("buyPrice", order.get("price", 0.0)))
                yield_pct = (current_price / buy_price - 1.0) * 100 if buy_price > 0 else 0.0

                exit_triggered = False
                exit_reason = ""

                if current_price > sma_5:
                    exit_triggered = True
                    exit_reason = f"Price (${current_price:.2f}) > SMA(5) (${sma_5:.2f})"
                elif rsi_2 > rsi_sell_thresh:
                    exit_triggered = True
                    exit_reason = f"RSI(2) ({rsi_2:.2f}) > {rsi_sell_thresh}"
                elif curr_days >= max_holding_days:
                    exit_triggered = True
                    exit_reason = f"Time-stop reached: HoldingDays ({curr_days}) >= {max_holding_days}"

                if exit_triggered:
                    logger.info(
                        f"★★ CRSI EXIT SIGNAL ★★ | {self.ticker} | Reason: {exit_reason} | "
                        f"Current Yield: {yield_pct:+.2f}% | Selling {qty} shares..."
                    )
                    try:
                        res = self.api_client.place_market_order(self.ticker, "SELL", qty)
                        sell_order_id = res.get("orderId", "")
                        order["exchangeOrderId"] = sell_order_id
                        self.db_manager.update_crsi_incomplete_order_exchange_id(oid, sell_order_id)
                        logger.info(f"Placed CRSI MARKET SELL order {sell_order_id} for {qty} shares.")
                    except Exception as e:
                        logger.error(f"CRSI [{self.ticker}] Failed to place market sell order: {e}")

            self.last_eval_date = today_str
            self._save_session_state()

        # --- [CASE B: No Position & No Pending Buys -> Evaluate Entry Conditions] ---
        elif not self.pending_buy_orders:
            # Check enabled flag
            if not self.config.get("enabled", True):
                logger.info(f"CRSI [{self.ticker}] Strategy is disabled. Skipping new buy entry.")
                self.last_eval_date = today_str
                self._save_session_state()
                return

            buy_qty = int(self.config.get("buy_qty", 1))
            is_above_trend = current_price > sma_200
            is_oversold = rsi_2 < rsi_buy_thresh

            if is_above_trend and is_oversold:
                logger.info(
                    f"★★ CRSI ENTRY SIGNAL ★★ | {self.ticker} | "
                    f"Price (${current_price:.2f}) > SMA(200) (${sma_200:.2f}) AND RSI(2) ({rsi_2:.2f}) < {rsi_buy_thresh} | "
                    f"Buying {buy_qty} integer shares..."
                )
                try:
                    res = self.api_client.place_market_order(self.ticker, "BUY", buy_qty)
                    buy_order_id = res.get("orderId", f"crsi_buy_{int(time.time())}")
                    self.db_manager.add_crsi_pending_buy_order(buy_order_id, self.ticker, buy_qty, current_price)
                    self.pending_buy_orders[buy_order_id] = {
                        "orderId": buy_order_id,
                        "symbol": self.ticker,
                        "quantity": buy_qty,
                        "price": current_price,
                        "orderedAt": datetime.now().isoformat()
                    }
                    logger.info(f"Placed CRSI MARKET BUY order {buy_order_id} for {buy_qty} shares.")
                except Exception as e:
                    logger.error(f"CRSI [{self.ticker}] Failed to place market buy order: {e}")
            else:
                logger.info(
                    f"CRSI [{self.ticker}] No entry signal today. "
                    f"Above SMA(200): {is_above_trend} (Price ${current_price:.2f} vs SMA ${sma_200:.2f}), "
                    f"Oversold: {is_oversold} (RSI(2) {rsi_2:.2f} vs {rsi_buy_thresh})"
                )

            self.last_eval_date = today_str
            self._save_session_state()
