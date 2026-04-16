"""SQLite database helper for shared state between processes.

Uses WAL mode for concurrent reads/writes across processes.
"""

import sqlite3
import time
import os
import datetime
from typing import Optional
from models import Position, AccountInfo, TradeAction, PositionMapping

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copier.db")

SCHEMA_VERSION = 2  # Bump when schema changes


def get_connection() -> sqlite3.Connection:
    """Get a database connection with WAL mode enabled."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema. Recreates tables if schema version changed."""
    conn = get_connection()
    cursor = conn.cursor()

    # Check schema version
    try:
        row = cursor.execute(
            "SELECT value FROM copier_state WHERE key='schema_version'"
        ).fetchone()
        current_version = int(row["value"]) if row else 0
    except Exception:
        current_version = 0

    if current_version < SCHEMA_VERSION:
        # Drop old tables and recreate
        cursor.executescript("""
            DROP TABLE IF EXISTS master_positions;
            DROP TABLE IF EXISTS position_mapping;
            DROP TABLE IF EXISTS account_info;
            DROP TABLE IF EXISTS trade_log;
            DROP TABLE IF EXISTS copier_state;
        """)

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS master_positions (
            ticket INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            type INTEGER NOT NULL,
            volume REAL NOT NULL,
            price_open REAL NOT NULL,
            price_current REAL NOT NULL,
            sl REAL DEFAULT 0,
            tp REAL DEFAULT 0,
            profit REAL DEFAULT 0,
            time_open INTEGER NOT NULL,
            magic INTEGER DEFAULT 0,
            comment TEXT DEFAULT '',
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS position_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_ticket INTEGER NOT NULL,
            slave_ticket INTEGER,
            slave_id TEXT NOT NULL DEFAULT '',
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            master_lots REAL NOT NULL,
            slave_lots REAL NOT NULL,
            master_open_price REAL DEFAULT 0,
            slave_open_price REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            copy_delay_ms REAL DEFAULT 0,
            UNIQUE(master_ticket, slave_id)
        );

        CREATE TABLE IF NOT EXISTS account_info (
            account_type TEXT PRIMARY KEY,
            connected INTEGER DEFAULT 0,
            login INTEGER DEFAULT 0,
            server TEXT DEFAULT '',
            broker TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            equity REAL DEFAULT 0,
            profit REAL DEFAULT 0,
            margin_used REAL DEFAULT 0,
            margin_free REAL DEFAULT 0,
            open_positions INTEGER DEFAULT 0,
            daily_pnl REAL DEFAULT 0,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            action TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            message TEXT NOT NULL,
            master_ticket INTEGER,
            slave_ticket INTEGER,
            slave_id TEXT DEFAULT '',
            pnl REAL
        );

        CREATE TABLE IF NOT EXISTS copier_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)

    now = time.time()
    cursor.execute(
        "INSERT OR REPLACE INTO copier_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("schema_version", str(SCHEMA_VERSION), now)
    )
    cursor.execute(
        "INSERT OR IGNORE INTO copier_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("status", "running", now)
    )
    cursor.execute(
        "INSERT OR IGNORE INTO copier_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("start_time", str(now), now)
    )

    conn.commit()
    conn.close()


# --- Master Positions ---

def update_master_positions(positions: list[Position]):
    """Replace the master positions snapshot with the current state."""
    conn = get_connection()
    cursor = conn.cursor()
    now = time.time()

    cursor.execute("DELETE FROM master_positions")
    for p in positions:
        cursor.execute(
            """INSERT INTO master_positions
               (ticket, symbol, type, volume, price_open, price_current, sl, tp, profit, time_open, magic, comment, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.ticket, p.symbol, p.type, p.volume, p.price_open, p.price_current,
             p.sl, p.tp, p.profit, p.time_open, p.magic, p.comment, now)
        )

    conn.commit()
    conn.close()


def get_master_positions() -> list[dict]:
    """Get all current master positions."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM master_positions").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Position Mapping ---

def add_mapping(mapping: PositionMapping):
    """Add a new position mapping."""
    conn = get_connection()
    now = time.time()
    conn.execute(
        """INSERT OR REPLACE INTO position_mapping
           (master_ticket, slave_ticket, slave_id, symbol, direction, master_lots, slave_lots,
            master_open_price, slave_open_price, status, created_at, updated_at, copy_delay_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mapping.master_ticket, mapping.slave_ticket, mapping.slave_id, mapping.symbol,
         mapping.direction, mapping.master_lots, mapping.slave_lots, mapping.master_open_price,
         mapping.slave_open_price, mapping.status, now, now, mapping.copy_delay_ms)
    )
    conn.commit()
    conn.close()


def update_mapping_status(master_ticket: int, status: str, slave_id: str, slave_ticket: Optional[int] = None):
    """Update the status (and optionally slave ticket) of a mapping."""
    conn = get_connection()
    now = time.time()
    if slave_ticket is not None:
        conn.execute(
            "UPDATE position_mapping SET status=?, slave_ticket=?, updated_at=? WHERE master_ticket=? AND slave_id=?",
            (status, slave_ticket, now, master_ticket, slave_id)
        )
    else:
        conn.execute(
            "UPDATE position_mapping SET status=?, updated_at=? WHERE master_ticket=? AND slave_id=?",
            (status, now, master_ticket, slave_id)
        )
    conn.commit()
    conn.close()


def update_mapping_lots(master_ticket: int, slave_lots: float, slave_id: str):
    """Update slave lots on a mapping (for partial close)."""
    conn = get_connection()
    now = time.time()
    conn.execute(
        "UPDATE position_mapping SET slave_lots=?, updated_at=? WHERE master_ticket=? AND slave_id=?",
        (slave_lots, now, master_ticket, slave_id)
    )
    conn.commit()
    conn.close()


def remove_mapping(master_ticket: int, slave_id: str):
    """Remove a position mapping (after close)."""
    conn = get_connection()
    conn.execute("DELETE FROM position_mapping WHERE master_ticket=? AND slave_id=?",
                 (master_ticket, slave_id))
    conn.commit()
    conn.close()


def get_all_mappings(slave_id: str = "") -> list[dict]:
    """Get all active position mappings, optionally filtered by slave_id."""
    conn = get_connection()
    if slave_id:
        rows = conn.execute(
            "SELECT * FROM position_mapping WHERE status != 'closed' AND slave_id=?",
            (slave_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM position_mapping WHERE status != 'closed'"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_mapping_by_master_ticket(master_ticket: int, slave_id: str) -> Optional[dict]:
    """Get a mapping by master ticket and slave_id."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM position_mapping WHERE master_ticket=? AND slave_id=?",
        (master_ticket, slave_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def remove_all_mappings_for_slave(slave_id: str):
    """Remove all mappings for a slave (when slave is removed)."""
    conn = get_connection()
    conn.execute("DELETE FROM position_mapping WHERE slave_id=?", (slave_id,))
    conn.commit()
    conn.close()


# --- Account Info ---

def update_account_info(account_type: str, info: AccountInfo):
    """Update account info for master or a slave (account_type = slave_id)."""
    conn = get_connection()
    now = time.time()
    conn.execute(
        """INSERT OR REPLACE INTO account_info
           (account_type, connected, login, server, broker, balance, equity, profit,
            margin_used, margin_free, open_positions, daily_pnl, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account_type, int(info.connected), info.login, info.server, info.broker,
         info.balance, info.equity, info.profit, info.margin, info.margin_free,
         info.open_positions, info.daily_pnl, now)
    )
    conn.commit()
    conn.close()


def get_account_info(account_type: str) -> Optional[dict]:
    """Get account info by type ('master' or slave_id like 'slave_1')."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM account_info WHERE account_type=?", (account_type,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_slave_account_info() -> list[dict]:
    """Get account info for all slaves."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM account_info WHERE account_type != 'master'"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_account_disconnected(account_type: str):
    """Mark an account as disconnected."""
    conn = get_connection()
    now = time.time()
    conn.execute(
        "UPDATE account_info SET connected=0, updated_at=? WHERE account_type=?",
        (now, account_type)
    )
    conn.commit()
    conn.close()


def remove_account_info(account_type: str):
    """Remove account info row (when slave is deleted)."""
    conn = get_connection()
    conn.execute("DELETE FROM account_info WHERE account_type=?", (account_type,))
    conn.commit()
    conn.close()


# --- Trade Log ---

def add_trade_log(entry: TradeAction):
    """Add an entry to the trade log."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO trade_log (timestamp, action, symbol, message, master_ticket, slave_ticket, slave_id, pnl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), entry.action, entry.symbol, entry.message,
         entry.master_ticket, entry.slave_ticket, entry.slave_id, entry.pnl)
    )
    conn.commit()
    conn.close()


def get_trade_log(limit: int = 100, slave_id: str = "") -> list[dict]:
    """Get recent trade log entries, optionally filtered by slave_id."""
    conn = get_connection()
    if slave_id:
        rows = conn.execute(
            "SELECT * FROM trade_log WHERE slave_id=? ORDER BY timestamp DESC LIMIT ?",
            (slave_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trade_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Copier State ---

def set_copier_state(key: str, value: str):
    """Set a copier state value."""
    conn = get_connection()
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO copier_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, now)
    )
    conn.commit()
    conn.close()


def get_copier_state(key: str) -> Optional[str]:
    """Get a copier state value."""
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM copier_state WHERE key=?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else None


# --- Performance Stats ---

def get_today_stats(slave_id: str = "") -> dict:
    """Get today's performance statistics, optionally filtered by slave_id."""
    conn = get_connection()

    today_start = datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    if slave_id:
        filter_sql = "AND slave_id=?"
        filter_params = (slave_id,)
    else:
        filter_sql = ""
        filter_params = ()

    # Today's closed trades
    rows = conn.execute(
        f"SELECT * FROM trade_log WHERE action='CLOSED' AND timestamp >= ? {filter_sql}",
        (today_start,) + filter_params
    ).fetchall()

    wins = sum(1 for r in rows if r["pnl"] is not None and r["pnl"] > 0)
    losses = sum(1 for r in rows if r["pnl"] is not None and r["pnl"] < 0)
    total_pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    pnl_values = [r["pnl"] for r in rows if r["pnl"] is not None]
    largest_win = max(pnl_values, default=0)
    largest_loss = min(pnl_values, default=0)

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    # All-time stats
    all_rows = conn.execute(
        f"SELECT pnl FROM trade_log WHERE action='CLOSED' AND pnl IS NOT NULL {filter_sql}",
        filter_params
    ).fetchall()
    all_wins = sum(1 for r in all_rows if r["pnl"] > 0)
    all_total = len(all_rows)
    all_time_win_rate = (all_wins / all_total * 100) if all_total > 0 else 0

    # Average copy delay
    if slave_id:
        delay_rows = conn.execute(
            "SELECT AVG(copy_delay_ms) as avg_delay FROM position_mapping WHERE copy_delay_ms > 0 AND slave_id=?",
            (slave_id,)
        ).fetchone()
    else:
        delay_rows = conn.execute(
            "SELECT AVG(copy_delay_ms) as avg_delay FROM position_mapping WHERE copy_delay_ms > 0"
        ).fetchone()
    avg_delay = delay_rows["avg_delay"] if delay_rows and delay_rows["avg_delay"] else 0

    # Today's copied trades count
    today_opened = conn.execute(
        f"SELECT COUNT(*) as cnt FROM trade_log WHERE action='OPENED' AND timestamp >= ? {filter_sql}",
        (today_start,) + filter_params
    ).fetchone()["cnt"]

    conn.close()

    return {
        "today_trades": today_opened,
        "today_wins": wins,
        "today_losses": losses,
        "today_pnl": round(total_pnl, 2),
        "today_win_rate": round(win_rate, 1),
        "all_time_win_rate": round(all_time_win_rate, 1),
        "avg_copy_delay_ms": round(avg_delay, 1),
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
    }
