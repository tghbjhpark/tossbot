import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import pytz
import os
import tempfile

from toss_api import TossAPIClient
from sqlite_manager import SQLiteManager
from strategies.tvr import TvrStrategy
from config import parse_ticker_item

class TestTvrStrategy(unittest.TestCase):
    def setUp(self):
        # Setup temporary SQLite database for testing
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.db_path = self.temp_db.name
        self.temp_db.close()

        with patch("sqlite_manager.SQLITE_DB_PATH", self.db_path):
            self.db_manager = SQLiteManager()
            self.db_manager.db_path = self.db_path
            self.db_manager.initialize()

        self.api_client = TossAPIClient()
        self.config = {
            "ticker": "SOXL",
            "strategy": "TVR",
            "market": "US",
            "target_value": 1000.0,
            "cycle": 10,
            "band": 0.10,
            "buy_mode": "AMOUNT",
            "min_trade_amount": 10.0,
            "rebalance_delay_minutes": 20,
            "enabled": True
        }

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_config_parsing(self):
        raw_item = {
            "ticker": "SOXL",
            "strategy": "TVR",
            "target_value": 1500.0,
            "cycle": 14,
            "band": 0.15,
            "rebalance_delay_minutes": 25
        }
        parsed = parse_ticker_item(raw_item)
        self.assertEqual(parsed["ticker"], "SOXL")
        self.assertEqual(parsed["strategy"], "TVR")
        self.assertEqual(parsed["target_value"], 1500.0)
        self.assertEqual(parsed["cycle"], 14)
        self.assertEqual(parsed["band"], 0.15)
        self.assertEqual(parsed["rebalance_delay_minutes"], 25)

    def test_toss_api_get_holding_quantity(self):
        # Mock OpenAPI response for /api/v1/holdings
        mock_response = {
            "items": [
                {
                    "symbol": "SOXL",
                    "quantity": "25.5",
                    "lastPrice": "40.0"
                }
            ]
        }
        with patch.object(self.api_client, "get_holdings", return_value=mock_response):
            qty = self.api_client.get_holding_quantity("SOXL")
            self.assertEqual(qty, 25.5)

            qty_unknown = self.api_client.get_holding_quantity("AAPL")
            self.assertEqual(qty_unknown, 0.0)

    def test_db_session_state_and_history(self):
        # Test session state persistence
        self.db_manager.save_tvr_session_state("SOXL", cycle_count=2, last_cycle_date="2026-09-01", last_rebalance_date="2026-09-01")
        state = self.db_manager.get_tvr_session_state("SOXL")
        self.assertEqual(state["cycle_count"], 2)
        self.assertEqual(state["last_cycle_date"], "2026-09-01")
        self.assertEqual(state["last_rebalance_date"], "2026-09-01")

        # Test trade history
        self.db_manager.add_tvr_trade_history("ord_123", "SOXL", "BUY", 5.0, 40.0, 200.0)
        # Verify no error thrown

    def test_execution_time_window(self):
        strat = TvrStrategy("SOXL", self.api_client, self.db_manager, self.config)
        tz_us = pytz.timezone("America/New_York")

        # Test 1: Exactly 09:49 EST on a Monday -> False (19 mins after open, needs 20 mins)
        dt_before = datetime(2026, 9, 21, 9, 49, tzinfo=tz_us) # 2026-09-21 is Monday
        with patch("strategies.tvr.datetime") as mock_dt:
            mock_dt.now.return_value = dt_before
            mock_dt.strptime = datetime.strptime
            in_window, _ = strat._is_execution_time()
            self.assertFalse(in_window)

        # Test 2: Exactly 09:50 EST on a Monday -> True (20 mins after open)
        dt_start = datetime(2026, 9, 21, 9, 50, tzinfo=tz_us)
        with patch("strategies.tvr.datetime") as mock_dt:
            mock_dt.now.return_value = dt_start
            mock_dt.strptime = datetime.strptime
            in_window, _ = strat._is_execution_time()
            self.assertTrue(in_window)

        # Test 3: 11:00 EST on a Monday -> True
        dt_mid = datetime(2026, 9, 21, 11, 0, tzinfo=tz_us)
        with patch("strategies.tvr.datetime") as mock_dt:
            mock_dt.now.return_value = dt_mid
            mock_dt.strptime = datetime.strptime
            in_window, _ = strat._is_execution_time()
            self.assertTrue(in_window)

        # Test 4: Weekend (Saturday) -> False
        dt_weekend = datetime(2026, 9, 26, 11, 0, tzinfo=tz_us)
        with patch("strategies.tvr.datetime") as mock_dt:
            mock_dt.now.return_value = dt_weekend
            mock_dt.strptime = datetime.strptime
            in_window, _ = strat._is_execution_time()
            self.assertFalse(in_window)

    def test_cycle_due_logic(self):
        strat = TvrStrategy("SOXL", self.api_client, self.db_manager, self.config)
        strat.initialize_state()

        # First run (last_cycle_date is None) -> Due
        self.assertTrue(strat._is_cycle_due("2026-09-21"))

        # Cycle = 10, last run 5 days ago -> Not due
        strat.last_cycle_date = "2026-09-16"
        self.assertFalse(strat._is_cycle_due("2026-09-21"))

        # Cycle = 10, last run 10 days ago -> Due
        strat.last_cycle_date = "2026-09-11"
        self.assertTrue(strat._is_cycle_due("2026-09-21"))

    def test_rebalance_undervaluation_buy(self):
        """
        Target: $1,000, Band: ±10% ($900 ~ $1,100).
        Holding: 10 shares @ $40 = $400.
        Valuation $400 < $900 -> Undervalued!
        Deficit = $1,000 - $400 = $600.
        Should trigger BUY order of $600.
        """
        strat = TvrStrategy("SOXL", self.api_client, self.db_manager, self.config)
        strat.initialize_state()

        # Mock holdings from Toss API
        with patch.object(self.api_client, "get_holding_quantity", return_value=10.0), \
             patch.object(self.api_client, "place_amount_market_order", return_value={"orderId": "buy_1"}) as mock_buy, \
             patch.object(strat, "_poll_order_fill"):
            
            strat._perform_rebalance(current_price=40.0, current_qty=10.0, today_str="2026-09-21")

            mock_buy.assert_called_once_with("SOXL", "BUY", 600.0)
            self.assertEqual(strat.last_rebalance_date, "2026-09-21")
            self.assertEqual(strat.last_cycle_date, "2026-09-21")
            self.assertEqual(strat.cycle_count, 2)

    def test_rebalance_overvaluation_sell(self):
        """
        Target: $1,000, Band: ±10% ($900 ~ $1,100).
        Holding: 30 shares @ $50 = $1,500.
        Valuation $1,500 > $1,100 -> Overvalued!
        Excess = $1,500 - $1,000 = $500.
        Sell shares = $500 / $50 = 10 shares.
        Should trigger MARKET SELL order of 10 shares.
        """
        strat = TvrStrategy("SOXL", self.api_client, self.db_manager, self.config)
        strat.initialize_state()

        with patch.object(self.api_client, "get_holding_quantity", return_value=30.0), \
             patch.object(self.api_client, "place_market_order", return_value={"orderId": "sell_1"}) as mock_sell, \
             patch.object(strat, "_poll_order_fill"):

            strat._perform_rebalance(current_price=50.0, current_qty=30.0, today_str="2026-09-21")

            mock_sell.assert_called_once_with("SOXL", "SELL", 10.0)
            self.assertEqual(strat.last_rebalance_date, "2026-09-21")
            self.assertEqual(strat.cycle_count, 2)

    def test_rebalance_within_band_no_trade(self):
        """
        Target: $1,000, Band: ±10% ($900 ~ $1,100).
        Holding: 20 shares @ $50 = $1,000.
        Valuation $1,000 is inside [$900 ~ $1,100].
        No buy or sell order should be submitted, but cycle should advance.
        """
        strat = TvrStrategy("SOXL", self.api_client, self.db_manager, self.config)
        strat.initialize_state()

        with patch.object(self.api_client, "place_amount_market_order") as mock_buy, \
             patch.object(self.api_client, "place_market_order") as mock_sell:

            strat._perform_rebalance(current_price=50.0, current_qty=20.0, today_str="2026-09-21")

            mock_buy.assert_not_called()
            mock_sell.assert_not_called()
            self.assertEqual(strat.last_rebalance_date, "2026-09-21")
            self.assertEqual(strat.cycle_count, 2)

if __name__ == "__main__":
    unittest.main()
