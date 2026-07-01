import os
import sqlite3
import logging
from datetime import datetime
from config import SQLITE_DB_PATH

logger = logging.getLogger("TossTradeBot.SQLiteManager")

class SQLiteManager:
    """
    Manages state synchronization and historical logging with a local SQLite database.
    Replaces Firebase for local offline execution.
    """
    def __init__(self):
        self.db_path = SQLITE_DB_PATH
        self._initialized = False

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> bool:
        if self._initialized:
            return True
        
        try:
            # Ensure parent directory exists
            db_dir = os.path.dirname(self.db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)
                
            logger.info(f"Initializing SQLite database at: {self.db_path}")
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 1. Table for pending buy orders
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_buy_orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    ordered_at TEXT NOT NULL,
                    is_amount_based INTEGER DEFAULT 0,
                    order_amount REAL DEFAULT 0.0
                )
            """)
            
            # 2. Table for incomplete sell orders
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS incomplete_orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    buy_price REAL NOT NULL,
                    ordered_at TEXT NOT NULL,
                    is_synthetic INTEGER DEFAULT 0,
                    exchange_order_id TEXT
                )
            """)
            
            # DB Migration: Add exchange_order_id column if it doesn't exist
            try:
                cursor.execute("ALTER TABLE incomplete_orders ADD COLUMN exchange_order_id TEXT")
            except sqlite3.OperationalError:
                pass # Column already exists
            
            # 3. Table for historical match logs and profits
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sell_order_id TEXT UNIQUE NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    buy_price REAL NOT NULL,
                    buy_time TEXT NOT NULL,
                    sell_price REAL,
                    sell_time TEXT,
                    profit REAL,
                    status TEXT NOT NULL
                )
            """)
            
            conn.commit()
            conn.close()
            self._initialized = True
            logger.info("SQLite Database successfully initialized and schema applied.")
            return True
        except Exception as e:
            logger.exception(f"Failed to initialize SQLite database: {e}")
            return False

    def is_initialized(self) -> bool:
        return self._initialized

    def get_incomplete_orders(self) -> dict:
        """
        Fetches all currently incomplete sell orders stored in SQLite.
        """
        if not self._initialized:
            logger.warning("SQLite not initialized. Returning empty dict.")
            return {}
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM incomplete_orders")
            rows = cursor.fetchall()
            conn.close()
            
            orders = {}
            for row in rows:
                oid = row["order_id"]
                orders[oid] = {
                    "orderId": oid,
                    "symbol": row["symbol"],
                    "price": str(row["price"]),
                    "quantity": str(row["quantity"]),
                    "buyPrice": str(row["buy_price"]),
                    "orderedAt": row["ordered_at"],
                    "isSynthetic": bool(row["is_synthetic"]),
                    "exchangeOrderId": row["exchange_order_id"] if row["exchange_order_id"] else ""
                }
            return orders
        except Exception as e:
            logger.error(f"Error fetching incomplete orders from SQLite: {e}")
            return {}

    def add_incomplete_order(self, order_id: str, order_data: dict) -> bool:
        """
        Saves a new incomplete sell order to SQLite and registers it in trade history.
        """
        if not self._initialized:
            logger.warning("SQLite not initialized. Skipping add_incomplete_order.")
            return False
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Save incomplete order state
            cursor.execute(
                """
                INSERT OR REPLACE INTO incomplete_orders 
                (order_id, symbol, price, quantity, buy_price, ordered_at, is_synthetic, exchange_order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    order_data.get("symbol"),
                    float(order_data.get("price")),
                    float(order_data.get("quantity")),
                    float(order_data.get("buyPrice")),
                    order_data.get("orderedAt"),
                    1 if order_data.get("isSynthetic") else 0,
                    order_data.get("exchangeOrderId", "")
                )
            )
            
            # Add to historical trace logs as BUY_FILLED state
            cursor.execute(
                """
                INSERT OR IGNORE INTO trades_history 
                (sell_order_id, symbol, quantity, buy_price, buy_time, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    order_data.get("symbol"),
                    float(order_data.get("quantity")),
                    float(order_data.get("buyPrice")),
                    order_data.get("orderedAt"),
                    "BUY_FILLED"
                )
            )
            
            conn.commit()
            conn.close()
            logger.info(f"Successfully saved incomplete order {order_id} to SQLite and trade history.")
            return True
        except Exception as e:
            logger.error(f"Error adding incomplete order {order_id} to SQLite: {e}")
            return False

    def remove_incomplete_order(self, order_id: str) -> bool:
        """
        Deletes a sell order from SQLite and marks the matching trade history record as COMPLETED.
        """
        if not self._initialized:
            logger.warning("SQLite not initialized. Skipping remove_incomplete_order.")
            return False
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Get order details to calculate profit
            cursor.execute("SELECT * FROM incomplete_orders WHERE order_id = ?", (order_id,))
            order = cursor.fetchone()
            
            if order:
                buy_price = order["buy_price"]
                sell_price = order["price"]
                quantity = order["quantity"]
                profit = (sell_price - buy_price) * quantity
                sell_time = datetime.now().isoformat()
                
                # Update trades_history to mark transaction as complete and record profit
                cursor.execute(
                    """
                    UPDATE trades_history
                    SET sell_price = ?, sell_time = ?, profit = ?, status = ?
                    WHERE sell_order_id = ?
                    """,
                    (sell_price, sell_time, profit, "COMPLETED", order_id)
                )
                logger.info(f"Matched trade history completed for sell order {order_id}. Profit: {profit:.4f}")
            else:
                logger.warning(f"Could not find matching incomplete order {order_id} to record in trade history.")

            # Delete the active incomplete order
            cursor.execute("DELETE FROM incomplete_orders WHERE order_id = ?", (order_id,))
            conn.commit()
            conn.close()
            logger.info(f"Successfully removed incomplete order {order_id} from SQLite.")
            return True
        except Exception as e:
            logger.error(f"Error removing incomplete order {order_id} from SQLite: {e}")
            return False

    def get_pending_buy_orders(self) -> dict:
        """
        Gets buy orders that were placed but the corresponding sell orders have not yet been generated.
        """
        if not self._initialized:
            return {}
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM pending_buy_orders")
            rows = cursor.fetchall()
            conn.close()
            
            orders = {}
            for row in rows:
                oid = row["order_id"]
                orders[oid] = {
                    "orderId": oid,
                    "symbol": row["symbol"],
                    "quantity": str(row["quantity"]),
                    "price": str(row["price"]),
                    "orderedAt": row["ordered_at"],
                    "isAmountBased": bool(row["is_amount_based"]),
                    "orderAmount": str(row["order_amount"])
                }
            return orders
        except Exception as e:
            logger.error(f"Error fetching pending buy orders: {e}")
            return {}

    def add_pending_buy_order(self, order_id: str, order_data: dict) -> bool:
        if not self._initialized:
            return False
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO pending_buy_orders 
                (order_id, symbol, quantity, price, ordered_at, is_amount_based, order_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    order_data.get("symbol"),
                    float(order_data.get("quantity")),
                    float(order_data.get("price")),
                    order_data.get("orderedAt"),
                    1 if order_data.get("isAmountBased") else 0,
                    float(order_data.get("orderAmount", 0.0))
                )
            )
            conn.commit()
            conn.close()
            logger.info(f"Successfully recorded pending buy order {order_id} in SQLite.")
            return True
        except Exception as e:
            logger.error(f"Error adding pending buy order {order_id}: {e}")
            return False

    def remove_pending_buy_order(self, order_id: str) -> bool:
        if not self._initialized:
            return False
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pending_buy_orders WHERE order_id = ?", (order_id,))
            conn.commit()
            conn.close()
            logger.info(f"Successfully removed pending buy order {order_id} from SQLite.")
            return True
        except Exception as e:
            logger.error(f"Error removing pending buy order {order_id}: {e}")
            return False

    def update_incomplete_order_exchange_id(self, order_id: str, exchange_id: str) -> bool:
        if not self._initialized:
            return False
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE incomplete_orders SET exchange_order_id = ? WHERE order_id = ?",
                (exchange_id, order_id)
            )
            conn.commit()
            conn.close()
            logger.info(f"Successfully updated exchange_order_id to {exchange_id} for {order_id} in SQLite.")
            return True
        except Exception as e:
            logger.error(f"Error updating exchange_order_id for {order_id}: {e}")
            return False

    def update_incomplete_order_quantity(self, order_id: str, quantity: float) -> bool:
        if not self._initialized:
            return False
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE incomplete_orders SET quantity = ? WHERE order_id = ?",
                (quantity, order_id)
            )
            conn.commit()
            conn.close()
            logger.info(f"Successfully updated quantity to {quantity} for incomplete order {order_id} in SQLite.")
            return True
        except Exception as e:
            logger.error(f"Error updating quantity for {order_id}: {e}")
            return False
