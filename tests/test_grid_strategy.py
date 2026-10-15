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

    def test_first_trading_day_of_month_logic(self):
        strat = GridStrategy("TSLL", self.api_client, self.db_manager, self.config_us_qty)
        tz_us = pytz.timezone("America/New_York")

        # 2026-10-01 (Thursday) -> First trading day (True)
        dt_first = datetime(2026, 10, 1, 10, 0, tzinfo=tz_us)
        self.assertTrue(strat._is_first_trading_day_of_month(dt_first))

        # 2026-10-02 (Friday) -> Second trading day (False)
        dt_second = datetime(2026, 10, 2, 10, 0, tzinfo=tz_us)
        self.assertFalse(strat._is_first_trading_day_of_month(dt_second))

        # 2026-11-01 (Sunday) -> Weekend (False)
        dt_nov_sun = datetime(2026, 11, 1, 10, 0, tzinfo=tz_us)
        self.assertFalse(strat._is_first_trading_day_of_month(dt_nov_sun))

        # 2026-11-02 (Monday) -> First trading day in Nov (True)
        dt_nov_mon = datetime(2026, 11, 2, 10, 0, tzinfo=tz_us)
        self.assertTrue(strat._is_first_trading_day_of_month(dt_nov_mon))

        # 2026-01-01 (Thursday) -> New Year's Day (False)
        dt_jan_1 = datetime(2026, 1, 1, 10, 0, tzinfo=tz_us)
        self.assertFalse(strat._is_first_trading_day_of_month(dt_jan_1))

        # 2026-01-02 (Friday) -> First trading day in Jan (True)
        dt_jan_2 = datetime(2026, 1, 2, 10, 0, tzinfo=tz_us)
        self.assertTrue(strat._is_first_trading_day_of_month(dt_jan_2))

    def test_get_grid_monthly_profit(self):
        conn = self.db_manager._get_connection()
        cursor = conn.cursor()

        # Previous month: 2026-09
        # 1. Profitable completed trade 1 (+15.0) -> Include
        cursor.execute(
            "INSERT INTO trades_history (sell_order_id, symbol, quantity, buy_price, buy_time, sell_price, sell_time, profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sell_1", "TSLL", 1.0, 10.0, "2026-09-01T10:00:00", 25.0, "2026-09-05T10:00:00", 15.0, "COMPLETED")
        )
        # 2. Profitable completed trade 2 (+25.0) -> Include
        cursor.execute(
            "INSERT INTO trades_history (sell_order_id, symbol, quantity, buy_price, buy_time, sell_price, sell_time, profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sell_2", "TSLL", 2.0, 10.0, "2026-09-02T10:00:00", 22.5, "2026-09-10T10:00:00", 25.0, "COMPLETED")
        )
        # 3. Stop loss trade (-10.0) -> Exclude (status = STOP_LOSS)
        cursor.execute(
            "INSERT INTO trades_history (sell_order_id, symbol, quantity, buy_price, buy_time, sell_price, sell_time, profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sell_3", "TSLL", 1.0, 30.0, "2026-09-03T10:00:00", 20.0, "2026-09-15T10:00:00", -10.0, "STOP_LOSS")
        )
        # 4. Completed but loss trade (-5.0) -> Exclude (profit <= 0)
        cursor.execute(
            "INSERT INTO trades_history (sell_order_id, symbol, quantity, buy_price, buy_time, sell_price, sell_time, profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sell_4", "TSLL", 1.0, 25.0, "2026-09-04T10:00:00", 20.0, "2026-09-20T10:00:00", -5.0, "COMPLETED")
        )
        # 5. Different month trade (2026-08) (+50.0) -> Exclude
        cursor.execute(
            "INSERT INTO trades_history (sell_order_id, symbol, quantity, buy_price, buy_time, sell_price, sell_time, profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sell_5", "TSLL", 1.0, 10.0, "2026-08-01T10:00:00", 60.0, "2026-08-15T10:00:00", 50.0, "COMPLETED")
        )
        # 6. Current month trade (2026-10) (+20.0) -> Exclude
        cursor.execute(
            "INSERT INTO trades_history (sell_order_id, symbol, quantity, buy_price, buy_time, sell_price, sell_time, profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sell_6", "TSLL", 1.0, 10.0, "2026-10-01T09:00:00", 30.0, "2026-10-01T10:00:00", 20.0, "COMPLETED")
        )
        conn.commit()
        conn.close()

        profit = self.db_manager.get_grid_monthly_profit("TSLL", "2026-09")
        self.assertAlmostEqual(profit, 40.0, places=2)

    def test_market_wide_monthly_stop_loss_execution_and_budget_cap(self):
        bot = TradeBot(self.api_client, self.db_manager)
        tz_us = pytz.timezone("America/New_York")

        # Two GRID strategies in US market: TSLL and NVDA
        config_nvda = {
            "ticker": "NVDA",
            "strategy": "GRID",
            "market": "US",
            "buy_mode": "QTY",
            "buy_qty": 1,
            "buy_amount": 20.0,
            "enabled": True
        }
        strat_tsll = GridStrategy("TSLL", self.api_client, self.db_manager, self.config_us_qty)
        strat_nvda = GridStrategy("NVDA", self.api_client, self.db_manager, config_nvda)
        bot.strategies = {"TSLL:GRID": strat_tsll, "NVDA:GRID": strat_nvda}

        # Mock previous month profit: TSLL (+60.0) + NVDA (+40.0) = Aggregate US Profit 100.0 (10% budget = 10.0)
        conn = self.db_manager._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO trades_history (sell_order_id, symbol, quantity, buy_price, buy_time, sell_price, sell_time, profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hist_tsll", "TSLL", 3.0, 10.0, "2026-09-01T10:00:00", 30.0, "2026-09-10T10:00:00", 60.0, "COMPLETED")
        )
        cursor.execute(
            "INSERT INTO trades_history (sell_order_id, symbol, quantity, buy_price, buy_time, sell_price, sell_time, profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hist_nvda", "NVDA", 2.0, 10.0, "2026-09-02T10:00:00", 30.0, "2026-09-15T10:00:00", 40.0, "COMPLETED")
        )
        conn.commit()
        conn.close()

        # Prices
        prices = {"TSLL": 15.0, "NVDA": 15.0}

        # Incomplete orders:
        # 1. TSLL order o1: 45 days old (2026-08-17), buy_price=21.0, qty=1.0 -> Loss = 6.0 (Oldest in entire market -> Sold first!)
        # 2. NVDA order o2: 35 days old (2026-08-27), buy_price=20.0, qty=1.0 -> Loss = 5.0 (2nd oldest in market -> Sold second! Cumulative loss = 11.0 >= 10.0 budget -> Stops!)
        # 3. TSLL order o3: 31 days old (2026-08-31), buy_price=25.0, qty=1.0 -> Should NOT be sold!
        # 4. NVDA order o4: 10 days old (2026-09-21), buy_price=30.0, qty=1.0 -> < 30 days -> Should NOT be sold!
        orders_tsll = {
            "o1": {"orderId": "o1", "symbol": "TSLL", "price": "23.0", "quantity": "1.0", "buyPrice": "21.0", "orderedAt": "2026-08-17T10:00:00", "exchangeOrderId": ""},
            "o3": {"orderId": "o3", "symbol": "TSLL", "price": "27.0", "quantity": "1.0", "buyPrice": "25.0", "orderedAt": "2026-08-31T10:00:00", "exchangeOrderId": ""},
        }
        orders_nvda = {
            "o2": {"orderId": "o2", "symbol": "NVDA", "price": "22.0", "quantity": "1.0", "buyPrice": "20.0", "orderedAt": "2026-08-27T10:00:00", "exchangeOrderId": ""},
            "o4": {"orderId": "o4", "symbol": "NVDA", "price": "33.0", "quantity": "1.0", "buyPrice": "30.0", "orderedAt": "2026-09-21T10:00:00", "exchangeOrderId": ""},
        }

        for oid, odata in orders_tsll.items():
            self.db_manager.add_incomplete_order(oid, odata)
            strat_tsll.incomplete_orders[oid] = odata
        for oid, odata in orders_nvda.items():
            self.db_manager.add_incomplete_order(oid, odata)
            strat_nvda.incomplete_orders[oid] = odata

        # Mock API sell order responses (1st call TSLL, 2nd call NVDA)
        self.api_client.place_limit_order = MagicMock(side_effect=[
            {"orderId": "ex_1"},
            {"orderId": "ex_2"}
        ])
        self.api_client.get_order_details = MagicMock(side_effect=[
            {"status": "FILLED", "execution": {"averageFilledPrice": "15.0"}},
            {"status": "FILLED", "execution": {"averageFilledPrice": "15.0"}}
        ])

        # Execute market-wide stop loss on 2026-10-01 10:00 EST (First trading day)
        eval_dt = datetime(2026, 10, 1, 10, 0, tzinfo=tz_us)
        with patch("trader.datetime") as mock_dt, \
             patch("time.sleep", return_value=None):
            mock_dt.now.return_value = eval_dt
            bot.process_monthly_grid_stop_loss(prices)

        # Verification:
        # o1 (TSLL) and o2 (NVDA) should be sold across the market
        self.assertNotIn("o1", strat_tsll.incomplete_orders)
        self.assertNotIn("o2", strat_nvda.incomplete_orders)

        # o3 (TSLL) and o4 (NVDA) must remain intact!
        self.assertIn("o3", strat_tsll.incomplete_orders)
        self.assertIn("o4", strat_nvda.incomplete_orders)

        # Market-wide session state should be recorded for US
        state = self.db_manager.get_grid_session_state("US")
        self.assertEqual(state.get("last_monthly_stop_loss_date"), "2026-10-01")

        # Check trades_history records have status 'STOP_LOSS'
        conn = self.db_manager._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT sell_order_id, status, profit FROM trades_history WHERE sell_order_id IN ('o1', 'o2')")
        rows = cursor.fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["status"], "STOP_LOSS")
            self.assertLess(r["profit"], 0)

        # Calling again on the same day should skip US market
        self.api_client.place_limit_order.reset_mock()
        with patch("trader.datetime") as mock_dt:
            mock_dt.now.return_value = eval_dt
            bot.process_monthly_grid_stop_loss(prices)
        self.api_client.place_limit_order.assert_not_called()

if __name__ == "__main__":
    unittest.main()
