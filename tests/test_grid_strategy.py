import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime
import pytz
import os
import tempfile

from toss_api import TossAPIClient
from sqlite_manager import SQLiteManager
from strategies.grid import GridStrategy
from trader import TradeBot
from config import parse_ticker_item

class TestGridStrategy(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.db_path = self.temp_db.name
        self.temp_db.close()

        with patch("sqlite_manager.SQLITE_DB_PATH", self.db_path):
            self.db_manager = SQLiteManager()
            self.db_manager.db_path = self.db_path
            self.db_manager.initialize()

        self.api_client = TossAPIClient()
        self.config_us_qty = {
            "ticker": "TSLL",
            "strategy": "GRID",
            "market": "US",
            "buy_mode": "QTY",
            "buy_qty": 1,
            "buy_amount": 20.0,
            "yield_target": 0.02,
            "grid_interval": 0.01,
            "open_delay_minutes": 15,
            "close_buffer_minutes": 10,
            "enabled": True
        }
        self.config_kr_qty = {
            "ticker": "0195S0",
            "strategy": "GRID",
            "market": "KR",
            "buy_mode": "QTY",
            "buy_qty": 1,
            "buy_amount": 20.0,
            "yield_target": 0.01,
            "grid_interval": 0.003,
            "open_delay_minutes": 15,
            "close_buffer_minutes": 10,
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
            "ticker": "TSLL",
            "strategy": "GRID",
            "market": "US",
            "buy_mode": "QTY",
            "open_delay_minutes": 15,
            "close_buffer_minutes": 10
        }
        parsed = parse_ticker_item(raw_item)
        self.assertEqual(parsed["ticker"], "TSLL")
        self.assertEqual(parsed["strategy"], "GRID")
        self.assertEqual(parsed["buy_mode"], "QTY")
        self.assertEqual(parsed["open_delay_minutes"], 15)
        self.assertEqual(parsed["close_buffer_minutes"], 10)

    def test_us_qty_trading_window(self):
        strat = GridStrategy("TSLL", self.api_client, self.db_manager, self.config_us_qty)
        tz_us = pytz.timezone("America/New_York")

        # 1. Weekend (Saturday) -> False
        dt_weekend = datetime(2026, 9, 26, 11, 0, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_weekend
            self.assertFalse(strat._is_trading_window())
            self.assertFalse(strat.is_active())

        # 2. Market Open (09:30 EST) on Monday (2026-09-21) -> False (needs 15 min delay)
        dt_open = datetime(2026, 9, 21, 9, 30, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_open
            self.assertFalse(strat._is_trading_window())
            self.assertFalse(strat.is_active())

        # 3. 09:44 EST -> False (14 mins after open)
        dt_before_window = datetime(2026, 9, 21, 9, 44, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_before_window
            self.assertFalse(strat._is_trading_window())
            self.assertFalse(strat.is_active())

        # 4. Start Boundary: Exactly 09:45 EST -> True (15 mins after open)
        dt_start = datetime(2026, 9, 21, 9, 45, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_start
            self.assertTrue(strat._is_trading_window())
            self.assertTrue(strat.is_active())

        # 5. Midday: 12:00 EST -> True
        dt_mid = datetime(2026, 9, 21, 12, 0, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_mid
            self.assertTrue(strat._is_trading_window())
            self.assertTrue(strat.is_active())

        # 6. Close Boundary: Exactly 15:50 EST -> True (10 mins before 16:00 close)
        dt_end = datetime(2026, 9, 21, 15, 50, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_end
            self.assertTrue(strat._is_trading_window())
            self.assertTrue(strat.is_active())

        # 7. Past Close: 15:51 EST -> False
        dt_after_window = datetime(2026, 9, 21, 15, 51, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_after_window
            self.assertFalse(strat._is_trading_window())
            self.assertFalse(strat.is_active())

        # 8. Late Evening: 18:00 EST -> False
        dt_after_hours = datetime(2026, 9, 21, 18, 0, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_after_hours
            self.assertFalse(strat._is_trading_window())
            self.assertFalse(strat.is_active())

    def test_kr_qty_trading_window(self):
        strat = GridStrategy("0195S0", self.api_client, self.db_manager, self.config_kr_qty)
        tz_kr = pytz.timezone("Asia/Seoul")

        # 1. 09:14 KST on Monday -> False
        dt_before = datetime(2026, 9, 21, 9, 14, tzinfo=tz_kr)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_before
            self.assertFalse(strat._is_trading_window())

        # 2. 09:15 KST (15 mins after 09:00 open) -> True
        dt_start = datetime(2026, 9, 21, 9, 15, tzinfo=tz_kr)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_start
            self.assertTrue(strat._is_trading_window())

        # 3. 15:10 KST (10 mins before 15:20 close) -> True
        dt_end = datetime(2026, 9, 21, 15, 10, tzinfo=tz_kr)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_end
            self.assertTrue(strat._is_trading_window())

        # 4. 15:11 KST -> False
        dt_after = datetime(2026, 9, 21, 15, 11, tzinfo=tz_kr)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_after
            self.assertFalse(strat._is_trading_window())

    def test_in_flight_orders_reconciliation_during_regular_hours(self):
        strat = GridStrategy("TSLL", self.api_client, self.db_manager, self.config_us_qty)
        strat.pending_buy_orders["test_order_1"] = {"orderId": "test_order_1"}
        tz_us = pytz.timezone("America/New_York")

        # At 15:55 EST (regular hours 09:30-16:00, but past 15:50 window cutoff):
        dt_pending = datetime(2026, 9, 21, 15, 55, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt, \
             patch("strategies.base.datetime") as mock_base_dt:
            mock_dt.now.return_value = dt_pending
            mock_base_dt.now.return_value = dt_pending
            self.assertFalse(strat._is_trading_window())
            self.assertTrue(strat.is_active())

    def test_trader_fallback_for_grid_qty(self):
        bot = TradeBot(self.api_client, self.db_manager)
        tz_us = pytz.timezone("America/New_York")
        strat = GridStrategy("TSLL", self.api_client, self.db_manager, self.config_us_qty)

        # 09:44 EST -> False
        dt_before = datetime(2026, 9, 21, 9, 44, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_before
            self.assertFalse(bot.is_market_active_for_strategy(strat))

        # 09:45 EST -> True
        dt_start = datetime(2026, 9, 21, 9, 45, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_start
            self.assertTrue(bot.is_market_active_for_strategy(strat))

        # 15:50 EST -> True
        dt_end = datetime(2026, 9, 21, 15, 50, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_end
            self.assertTrue(bot.is_market_active_for_strategy(strat))

        # 15:51 EST -> False
        dt_after = datetime(2026, 9, 21, 15, 51, tzinfo=tz_us)
        with patch("strategies.grid.datetime") as mock_dt:
            mock_dt.now.return_value = dt_after
            self.assertFalse(bot.is_market_active_for_strategy(strat))

if __name__ == "__main__":
    unittest.main()
