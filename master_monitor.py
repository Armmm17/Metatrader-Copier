"""Process 1: Master Monitor.

Connects to the master MT5 terminal, polls positions and account info,
and writes the state to the shared SQLite database.
"""

import time
import sys
import logging
import MetaTrader5 as mt5

import db
from models import Position, AccountInfo, TradeAction
from copier_logic import load_config

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] MASTER | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("master_monitor")


def connect_master(config: dict) -> bool:
    """Initialize connection to the master MT5 terminal."""
    master = config["master"]
    path = master["terminal_path"]
    login = master["login"]
    password = master["password"]
    server = master["server"]

    log.info(f"Connecting to master terminal: {path}")

    if not mt5.initialize(path=path, login=login, password=password, server=server):
        error = mt5.last_error()
        log.error(f"Failed to initialize master MT5: {error}")
        return False

    info = mt5.account_info()
    if info is None:
        log.error("Failed to get master account info after initialization.")
        mt5.shutdown()
        return False

    log.info(f"Connected to master: {info.company} | Account #{info.login} | Server: {info.server}")
    return True


def get_positions() -> list[Position]:
    """Fetch all open positions from the master terminal."""
    positions = mt5.positions_get()
    if positions is None:
        return []

    result = []
    for p in positions:
        result.append(Position(
            ticket=p.ticket,
            symbol=p.symbol,
            type=p.type,
            volume=p.volume,
            price_open=p.price_open,
            price_current=p.price_current,
            sl=p.sl,
            tp=p.tp,
            profit=p.profit,
            time_open=p.time,
            magic=p.magic,
            comment=p.comment if hasattr(p, "comment") else "",
        ))
    return result


def get_account() -> AccountInfo:
    """Fetch current account information from the master terminal."""
    info = mt5.account_info()
    if info is None:
        raise ConnectionError("Lost connection to master terminal")

    return AccountInfo(
        connected=True,
        login=info.login,
        server=info.server,
        broker=info.company,
        balance=info.balance,
        equity=info.equity,
        profit=info.profit,
        margin=info.margin,
        margin_free=info.margin_free,
        open_positions=0,  # will be set from positions count
    )


def run():
    """Main loop for the master monitor process."""
    db.init_db()
    config = load_config()
    poll_ms = config.get("copy_settings", {}).get("poll_interval_ms", 100)
    poll_sec = poll_ms / 1000.0

    # Connect to master
    connected = False
    while not connected:
        try:
            connected = connect_master(config)
        except Exception as e:
            log.error(f"Connection error: {e}")
        if not connected:
            db.set_account_disconnected("master")
            log.info("Retrying master connection in 5 seconds...")
            time.sleep(5)

    db.add_trade_log(TradeAction(
        action="INFO", symbol="", message="Master monitor started and connected."
    ))

    log.info(f"Polling master positions every {poll_ms}ms")

    while True:
        try:
            # Check if terminal is still alive
            terminal_info = mt5.terminal_info()
            if terminal_info is None:
                raise ConnectionError("Terminal disconnected")

            # Get positions
            positions = get_positions()
            db.update_master_positions(positions)

            # Get account info
            account = get_account()
            account.open_positions = len(positions)

            # Calculate daily P&L (simple: current profit)
            account.daily_pnl = account.profit

            db.update_account_info("master", account)

        except ConnectionError:
            log.warning("Lost connection to master terminal. Reconnecting...")
            db.set_account_disconnected("master")
            db.add_trade_log(TradeAction(
                action="ERROR", symbol="",
                message="Lost connection to master terminal. Attempting reconnection..."
            ))
            mt5.shutdown()

            connected = False
            while not connected:
                time.sleep(5)
                config = load_config()
                try:
                    connected = connect_master(config)
                except Exception as e:
                    log.error(f"Reconnection error: {e}")

            db.add_trade_log(TradeAction(
                action="INFO", symbol="", message="Master terminal reconnected."
            ))

        except Exception as e:
            log.error(f"Error in master monitor loop: {e}")
            time.sleep(1)

        time.sleep(poll_sec)


if __name__ == "__main__":
    run()
