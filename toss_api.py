import time
import uuid
import logging
import requests
from config import TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, TOSS_ACCOUNT_SEQ, TOSS_BASE_URL, TICKER_CONFIGS

logger = logging.getLogger("TossTradeBot.TossAPI")

class TossAPIClient:
    """
    HTTP client for Toss Securities OpenAPI. Handles OAuth2 token,
    exponential backoff retries, and API parameters formatting.
    """
    def __init__(self):
        self.client_id = TOSS_CLIENT_ID
        self.client_secret = TOSS_CLIENT_SECRET
        self.account_seq = TOSS_ACCOUNT_SEQ
        self.base_url = TOSS_BASE_URL
        
        self._token = None
        self._token_expires_at = 0.0

    def _get_headers(self, include_account=True) -> dict:
        token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        if include_account:
            if not self.account_seq:
                raise ValueError("TOSS_ACCOUNT_SEQ is missing in configurations.")
            headers["X-Tossinvest-Account"] = self.account_seq
        return headers

    def get_access_token(self) -> str:
        """
        Returns cached token, or fetches a new one if it is missing or close to expiring (within 60s).
        """
        now = time.time()
        if not self._token or now >= self._token_expires_at - 60:
            self._refresh_token()
        return self._token

    def _refresh_token(self):
        url = f"{self.base_url}/oauth2/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        
        logger.info("Requesting a new Toss OpenAPI access token...")
        try:
            response = requests.post(
                url, 
                data=payload, 
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            self._token = data["access_token"]
            expires_in = data.get("expires_in", 86400)
            self._token_expires_at = time.time() + float(expires_in)
            logger.info("Successfully fetched new OAuth2 token.")
        except Exception as e:
            logger.error(f"Failed to refresh OAuth2 token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Token endpoint error body: {e.response.text}")
            raise e

    def _request(self, method: str, path: str, include_account: bool = True, retries: int = 3, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        
        for attempt in range(retries):
            try:
                headers = self._get_headers(include_account=include_account)
                if 'headers' in kwargs:
                    headers.update(kwargs['headers'])
                    del kwargs['headers']
                    
                response = requests.request(method, url, headers=headers, timeout=15, **kwargs)
                
                # Check if token is unauthorized (401)
                if response.status_code == 401:
                    logger.warning("Access token unauthorized (401). Forcing token refresh and retrying...")
                    self._refresh_token()
                    headers = self._get_headers(include_account=include_account)
                    response = requests.request(method, url, headers=headers, timeout=15, **kwargs)
                
                # Rate limit (429) or Server error (5xx)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 2 * (attempt + 1)))
                    logger.warning(f"Rate limited (429). Retrying in {retry_after} seconds...")
                    time.sleep(retry_after)
                    continue
                elif 500 <= response.status_code < 600:
                    wait_time = 2 ** attempt
                    logger.warning(f"Server error ({response.status_code}). Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                    
                response.raise_for_status()
                return response.json()
                
            except requests.exceptions.ConnectionError as e:
                wait_time = 2 ** attempt
                logger.warning(f"Connection error: {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
            except Exception as e:
                logger.error(f"API request failed on {method} {path}: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"Response details: {e.response.text}")
                raise e
                
        raise IOError(f"Failed to execute API request {method} {path} after {retries} retries.")

    def get_krx_tick_size(self, price: float) -> float:
        """
        Unified KRX tick size rule:
        - Price < 2,000 KRW: 1 KRW
        - 2,000 <= Price < 5,000 KRW: 5 KRW
        - 5,000 <= Price < 20,000 KRW: 10 KRW
        - 20,000 <= Price < 50,000 KRW: 50 KRW
        - 50,000 <= Price < 200,000 KRW: 100 KRW
        - 200,000 <= Price < 500,000 KRW: 500 KRW
        - Price >= 500,000 KRW: 1,000 KRW
        """
        if price < 2000:
            return 1.0
        elif price < 5000:
            return 5.0
        elif price < 20000:
            return 10.0
        elif price < 50000:
            return 50.0
        elif price < 200000:
            return 100.0
        elif price < 500000:
            return 500.0
        else:
            return 1000.0

    def format_kr_price(self, price: float) -> str:
        tick_size = self.get_krx_tick_size(price)
        rounded = round(price / tick_size) * tick_size
        return f"{int(rounded)}"

    def format_price(self, price: float, market: str) -> str:
        """
        Formats price based on market:
        - KR: Round to KRX tick size, no decimals.
        - US: Truncate to 2 or 4 decimals based on value.
        """
        if market.upper() == "KR":
            return self.format_kr_price(price)
        else:
            if price < 1.0:
                truncated = int(price * 10000) / 10000
                return f"{truncated:.4f}"
            else:
                truncated = int(price * 100) / 100
                return f"{truncated:.2f}"

    def get_current_price(self, ticker: str) -> float:
        """
        Fetches the current market price of the specified US ticker.
        Endpoint: GET /api/v1/prices
        """
        params = {"symbols": ticker}
        res = self._request("GET", "/api/v1/prices", include_account=False, params=params)
        
        result_list = res.get("result", [])
        for item in result_list:
            if item.get("symbol") == ticker:
                price_str = item.get("lastPrice")
                if price_str:
                    return float(price_str)
                    
        raise ValueError(f"Could not retrieve current price for symbol {ticker} from Toss API.")

    def get_current_prices(self, tickers: list[str]) -> dict[str, float]:
        """
        Fetches the current market prices for a list of tickers in a single batch call.
        Endpoint: GET /api/v1/prices?symbols=AAPL,MSFT,SOXL
        """
        if not tickers:
            return {}
        symbols_str = ",".join(tickers)
        params = {"symbols": symbols_str}
        res = self._request("GET", "/api/v1/prices", include_account=False, params=params)
        
        prices = {}
        result_list = res.get("result", [])
        for item in result_list:
            symbol = item.get("symbol")
            price_str = item.get("lastPrice")
            if symbol and price_str:
                prices[symbol] = float(price_str)
        return prices

    def place_limit_order(self, ticker: str, side: str, quantity: int, price: float) -> dict:
        """
        Places a quantity-based limit order.
        Endpoint: POST /api/v1/orders
        """
        client_order_id = f"gtb_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        config = TICKER_CONFIGS.get(ticker, {})
        market = config.get("market", "KR" if ticker.isdigit() else "US")
        price_str = self.format_price(price, market)
        
        payload = {
            "clientOrderId": client_order_id,
            "symbol": ticker,
            "side": side.upper(),
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "quantity": str(quantity),
            "price": price_str
        }
        
        logger.info(f"Placing LIMIT {side.upper()} order for {ticker}: quantity={quantity}, price={price_str}")
        res = self._request("POST", "/api/v1/orders", include_account=True, json=payload)
        return res.get("result", {})

    def place_market_order(self, ticker: str, side: str, quantity: float) -> dict:
        """
        Places a quantity-based market order. Supports fractional shares (float).
        Endpoint: POST /api/v1/orders
        """
        client_order_id = f"gtb_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        # For US market orders, quantity can be fractional (up to 6 decimal places)
        qty_str = f"{quantity:.6f}".rstrip('0').rstrip('.')
        
        payload = {
            "clientOrderId": client_order_id,
            "symbol": ticker,
            "side": side.upper(),
            "orderType": "MARKET",
            "quantity": qty_str
        }
        
        logger.info(f"Placing MARKET {side.upper()} order for {ticker}: quantity={qty_str}")
        res = self._request("POST", "/api/v1/orders", include_account=True, json=payload)
        return res.get("result", {})

    def place_amount_market_order(self, ticker: str, side: str, amount: float) -> dict:
        """
        Places an amount-based market order (US MARKET only).
        Endpoint: POST /api/v1/orders
        """
        client_order_id = f"gtb_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        
        payload = {
            "clientOrderId": client_order_id,
            "symbol": ticker,
            "side": side.upper(),
            "orderType": "MARKET",
            "orderAmount": f"{amount:.2f}"
        }
        
        logger.info(f"Placing AMOUNT-based MARKET {side.upper()} order for {ticker}: ${amount:.2f}")
        res = self._request("POST", "/api/v1/orders", include_account=True, json=payload)
        return res.get("result", {})

    def get_order_details(self, order_id: str) -> dict:
        """
        Fetches detail information of a specific order.
        Endpoint: GET /api/v1/orders/{orderId}
        """
        res = self._request("GET", f"/api/v1/orders/{order_id}", include_account=True)
        return res.get("result", {})

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancels a specific pending order.
        Endpoint: POST /api/v1/orders/{orderId}/cancel
        """
        logger.info(f"Cancelling order {order_id} via API...")
        res = self._request("POST", f"/api/v1/orders/{order_id}/cancel", include_account=True, json={})
        return res.get("result", {})
