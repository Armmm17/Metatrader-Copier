"""Process 2: Slave Executor.

Each instance handles ONE slave account, identified by --slave-id.
Reads its own copy_settings and symbol_mapping from its slave config block.

Usage:
    python slave_executor.py --slave-id slave_1
"""

import time
import sys
import argparse
import logging
import MetaTrader5 as mt5

import db
from models import AccountInfo, TradeAction, PositionMapping
from copier_logic import (
    load_config, get_slave_config, map_symbol, calculate_slave_lots,
    is_symbol_allowed, check_max_trades, check_drawdown, detect_changes,
)

MAX_RETRIES = 3
RETRY_DELAY = 1.0

SLAVE_ID = ""
log = logging.getLogger("slave_executor")


def get_slave_settings(config: dict) -> tuple[dict, dict]:
    """Return (copy_settings, symbol_mapping) for this slave."""
    slave_cfg = get_slave_config(config, SLAVE_ID)
    if not slave_cfg:
        return {}, {}
    return slave_cfg.get("copy_settings", {}), slave_cfg.get("symbol_mapping", {})


def connect_slave(config: dict) -> bool:
    slave_cfg = get_slave_config(config, SLAVE_ID)
    if not slave_cfg:
        log.error(f"Slave '{SLAVE_ID}' not found in config")
        return False

    path = slave_cfg["terminal_path"]
    login = slave_cfg["login"]
    password = slave_cfg["password"]
    server = slave_cfg["server"]

    log.info(f"Connecting to slave terminal: {path}")

    if not mt5.initialize(path=path, login=login, password=password, server=server):
        log.error(f"Failed to initialize slave MT5: {mt5.last_error()}")
        return False

    info = mt5.account_info()
    if info is None:
        log.error("Failed to get slave account info after initialization.")
        mt5.shutdown()
        return False

    log.info(f"Connected: {info.company} | Account #{info.login} | Server: {info.server}")
    return True


def get_slave_account() -> AccountInfo:
    info = mt5.account_info()
    if info is None:
        raise ConnectionError("Lost connection to slave terminal")
    return AccountInfo(
        connected=True, login=info.login, server=info.server,
        broker=info.company, balance=info.balance, equity=info.equity,
        profit=info.profit, margin=info.margin, margin_free=info.margin_free,
        open_positions=0,
    )


def get_slave_positions() -> dict:
    positions = mt5.positions_get()
    if positions is None:
        return {}
    return {p.ticket: p for p in positions}


def open_trade(master_pos: dict, copy_settings: dict, symbol_mapping: dict) -> int | None:
    symbol = map_symbol(master_pos["symbol"], symbol_mapping)
    order_type = mt5.ORDER_TYPE_BUY if master_pos["type"] == 0 else mt5.ORDER_TYPE_SELL

    master_info = db.get_account_info("master")
    slave_info = db.get_account_info(SLAVE_ID)
    master_balance = master_info["balance"] if master_info else 0
    slave_balance = slave_info["balance"] if slave_info else 0

    lots = calculate_slave_lots(master_pos["volume"], copy_settings, master_balance, slave_balance)

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        mt5.symbol_select(symbol, True)
        time.sleep(0.1)
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            log.error(f"Symbol {symbol} not found on slave terminal")
            return None

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"Cannot get tick for {symbol}")
        return None

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    digits = sym_info.digits
    sl = round(master_pos.get("sl", 0), digits) if master_pos.get("sl", 0) != 0 else 0.0
    tp = round(master_pos.get("tp", 0), digits) if master_pos.get("tp", 0) != 0 else 0.0

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol, "volume": lots, "type": order_type,
        "price": price, "sl": sl, "tp": tp, "deviation": 20,
        "magic": 123456, "comment": f"copy_{master_pos['ticket']}",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }

    direction = "BUY" if master_pos["type"] == 0 else "SELL"

    for attempt in range(MAX_RETRIES):
        result = mt5.order_send(request)
        if result is None:
            log.error(f"order_send returned None (attempt {attempt + 1})")
            time.sleep(RETRY_DELAY)
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"OPENED {direction} {lots} {symbol} -> Slave #{result.order} (Master #{master_pos['ticket']})")
            return result.order
        log.error(f"Order failed (attempt {attempt + 1}): retcode={result.retcode}, comment={result.comment}")
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            request["price"] = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
        time.sleep(RETRY_DELAY)

    return None


def close_trade(slave_ticket: int) -> float | None:
    positions = mt5.positions_get(ticket=slave_ticket)
    if not positions:
        log.warning(f"Slave position #{slave_ticket} not found — may already be closed")
        return 0.0

    pos = positions[0]
    close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        log.error(f"Cannot get tick for {pos.symbol} to close")
        return None

    price = tick.bid if pos.type == 0 else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
        "type": close_type, "position": slave_ticket, "price": price,
        "deviation": 20, "magic": 123456, "comment": "close_copy",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    pnl = pos.profit

    for attempt in range(MAX_RETRIES):
        result = mt5.order_send(request)
        if result is None:
            time.sleep(RETRY_DELAY)
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"CLOSED {pos.symbol} #{slave_ticket} -> P&L: ${pnl:.2f}")
            return pnl
        log.error(f"Close failed (attempt {attempt + 1}): retcode={result.retcode}, comment={result.comment}")
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick:
            request["price"] = tick.bid if pos.type == 0 else tick.ask
        time.sleep(RETRY_DELAY)

    return None


def modify_sl_tp(slave_ticket: int, new_sl: float, new_tp: float) -> bool:
    positions = mt5.positions_get(ticket=slave_ticket)
    if not positions:
        return False
    pos = positions[0]
    sym_info = mt5.symbol_info(pos.symbol)
    digits = sym_info.digits if sym_info else 5
    new_sl = round(new_sl, digits)
    new_tp = round(new_tp, digits)
    if abs(pos.sl - new_sl) < 10 ** (-digits) and abs(pos.tp - new_tp) < 10 ** (-digits):
        return True
    request = {
        "action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol,
        "position": slave_ticket, "sl": new_sl, "tp": new_tp,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"MODIFIED SL/TP #{slave_ticket}: SL={new_sl}, TP={new_tp}")
        return True
    if result:
        log.error(f"Modify SL/TP failed: retcode={result.retcode}, comment={result.comment}")
    return False


def partial_close(slave_ticket: int, new_volume: float) -> bool:
    positions = mt5.positions_get(ticket=slave_ticket)
    if not positions:
        return False
    pos = positions[0]
    volume_to_close = round(pos.volume - new_volume, 2)
    if volume_to_close <= 0:
        return True
    close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return False
    price = tick.bid if pos.type == 0 else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": volume_to_close,
        "type": close_type, "position": slave_ticket, "price": price,
        "deviation": 20, "magic": 123456, "comment": "partial_close_copy",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"PARTIAL CLOSE #{slave_ticket}: closed {volume_to_close}, remaining {new_volume}")
        return True
    if result:
        log.error(f"Partial close failed: retcode={result.retcode}, comment={result.comment}")
    return False


def close_all_slave_positions():
    positions = mt5.positions_get()
    if not positions:
        log.info("No slave positions to close")
        return
    for pos in positions:
        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            continue
        price = tick.bid if pos.type == 0 else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
            "type": close_type, "position": pos.ticket, "price": price,
            "deviation": 20, "magic": 123456, "comment": "emergency_close",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"Emergency closed #{pos.ticket} {pos.symbol}")
        else:
            log.error(f"Failed to emergency close #{pos.ticket}")

    db.add_trade_log(TradeAction(
        action="INFO", symbol="", slave_id=SLAVE_ID,
        message=f"[{SLAVE_ID}] Emergency close all executed."
    ))


def process_cycle(config: dict):
    """Run one cycle of the trade copier logic for this slave."""
    copy_settings, symbol_mapping = get_slave_settings(config)

    status = db.get_copier_state("status")
    if status == "paused":
        return

    # Per-slave close all
    close_key = f"close_all_{SLAVE_ID}"
    close_all = db.get_copier_state(close_key)
    if close_all == "requested":
        db.set_copier_state(close_key, "executing")
        close_all_slave_positions()
        db.set_copier_state(close_key, "done")
        return

    # Global close all
    global_close = db.get_copier_state("close_all")
    if global_close == "requested":
        close_all_slave_positions()
        return

    master_positions = db.get_master_positions()
    active_mappings = db.get_all_mappings(slave_id=SLAVE_ID)

    # Update slave account info
    try:
        slave_account = get_slave_account()
        slave_positions = get_slave_positions()
        slave_account.open_positions = len(slave_positions)
        slave_account.daily_pnl = slave_account.profit
        db.update_account_info(SLAVE_ID, slave_account)
    except ConnectionError:
        raise

    # Drawdown check
    if not check_drawdown(slave_account.equity, slave_account.balance, copy_settings):
        dd_key = f"paused_drawdown_{SLAVE_ID}"
        if db.get_copier_state(dd_key) != "true":
            db.set_copier_state(dd_key, "true")
            db.add_trade_log(TradeAction(
                action="ERROR", symbol="", slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] Drawdown limit reached! Equity: {slave_account.equity:.2f}, "
                        f"Balance: {slave_account.balance:.2f}. Copying paused."
            ))
            log.warning("Drawdown limit reached — copying paused for this slave")
        return

    dd_key = f"paused_drawdown_{SLAVE_ID}"
    if db.get_copier_state(dd_key) == "true":
        db.set_copier_state(dd_key, "false")
        db.add_trade_log(TradeAction(
            action="INFO", symbol="", slave_id=SLAVE_ID,
            message=f"[{SLAVE_ID}] Drawdown recovered. Copying resumed."
        ))

    changes = detect_changes(master_positions, active_mappings)

    # New positions
    for pos in changes["new"]:
        if not is_symbol_allowed(pos["symbol"], copy_settings):
            continue
        if not check_max_trades(len(active_mappings), copy_settings):
            log.warning("Max open trades reached — skipping new trade")
            db.add_trade_log(TradeAction(
                action="ERROR", symbol=pos["symbol"], slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] Max trades limit. Skipped {pos['symbol']}.",
                master_ticket=pos["ticket"],
            ))
            continue

        direction = "BUY" if pos["type"] == 0 else "SELL"
        log.info(f"New position: {direction} {pos['volume']} {pos['symbol']} #{pos['ticket']}")

        m_info = db.get_account_info("master")
        s_info = db.get_account_info(SLAVE_ID)
        master_balance = m_info["balance"] if m_info else 0
        slave_balance = s_info["balance"] if s_info else 0
        slave_lots = calculate_slave_lots(pos["volume"], copy_settings, master_balance, slave_balance)

        copy_start = time.time()
        slave_ticket = open_trade(pos, copy_settings, symbol_mapping)
        copy_delay = (time.time() - copy_start) * 1000

        if slave_ticket:
            mapping = PositionMapping(
                master_ticket=pos["ticket"], slave_ticket=slave_ticket,
                symbol=pos["symbol"], direction=direction,
                master_lots=pos["volume"], slave_lots=slave_lots,
                master_open_price=pos["price_open"], slave_open_price=0,
                slave_id=SLAVE_ID, status="synced", copy_delay_ms=copy_delay,
            )
            db.add_mapping(mapping)
            db.add_trade_log(TradeAction(
                action="OPENED", symbol=pos["symbol"], slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] OPENED: {pos['symbol']} {direction} {slave_lots} -> #{slave_ticket}",
                master_ticket=pos["ticket"], slave_ticket=slave_ticket,
            ))
        else:
            db.add_trade_log(TradeAction(
                action="ERROR", symbol=pos["symbol"], slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] Failed to copy {pos['symbol']} {direction} {pos['volume']}",
                master_ticket=pos["ticket"],
            ))

    # Closed positions
    for mapping in changes["closed"]:
        log.info(f"Position closed on master: #{mapping['master_ticket']} {mapping['symbol']}")
        db.update_mapping_status(mapping["master_ticket"], "closing", SLAVE_ID)

        pnl = close_trade(mapping["slave_ticket"])
        if pnl is not None:
            db.remove_mapping(mapping["master_ticket"], SLAVE_ID)
            db.add_trade_log(TradeAction(
                action="CLOSED", symbol=mapping["symbol"], slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] CLOSED: {mapping['symbol']} #{mapping['slave_ticket']} -> P&L: ${pnl:.2f}",
                master_ticket=mapping["master_ticket"], slave_ticket=mapping["slave_ticket"], pnl=pnl,
            ))
        else:
            db.update_mapping_status(mapping["master_ticket"], "error", SLAVE_ID)
            db.add_trade_log(TradeAction(
                action="ERROR", symbol=mapping["symbol"], slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] Failed to close {mapping['symbol']} #{mapping['slave_ticket']}",
                master_ticket=mapping["master_ticket"], slave_ticket=mapping["slave_ticket"],
            ))

    # SL/TP modifications
    for mapping, master_pos in changes["sl_tp_changed"]:
        if not modify_sl_tp(mapping["slave_ticket"], master_pos.get("sl", 0), master_pos.get("tp", 0)):
            db.add_trade_log(TradeAction(
                action="ERROR", symbol=mapping["symbol"], slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] Failed to modify SL/TP on #{mapping['slave_ticket']}",
                master_ticket=mapping["master_ticket"], slave_ticket=mapping["slave_ticket"],
            ))

    # Partial closes
    for mapping, master_pos in changes["partial_close"]:
        old_volume = mapping["master_lots"]
        new_volume = master_pos["volume"]
        ratio = new_volume / old_volume if old_volume > 0 else 1
        new_slave_lots = max(round(mapping["slave_lots"] * ratio, 2), 0.01)

        log.info(f"Partial close: #{mapping['master_ticket']} {old_volume} -> {new_volume}")

        if partial_close(mapping["slave_ticket"], new_slave_lots):
            db.update_mapping_lots(mapping["master_ticket"], new_slave_lots, SLAVE_ID)
            db.add_trade_log(TradeAction(
                action="MODIFIED", symbol=mapping["symbol"], slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] PARTIAL CLOSE: {mapping['symbol']} #{mapping['slave_ticket']} "
                        f"{mapping['slave_lots']} -> {new_slave_lots}",
                master_ticket=mapping["master_ticket"], slave_ticket=mapping["slave_ticket"],
            ))
        else:
            db.add_trade_log(TradeAction(
                action="ERROR", symbol=mapping["symbol"], slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] Failed partial close #{mapping['slave_ticket']}",
                master_ticket=mapping["master_ticket"], slave_ticket=mapping["slave_ticket"],
            ))


def run():
    global SLAVE_ID
    parser = argparse.ArgumentParser()
    parser.add_argument("--slave-id", required=True)
    args = parser.parse_args()
    SLAVE_ID = args.slave_id

    logging.basicConfig(
        level=logging.INFO,
        format=f"[%(asctime)s] {SLAVE_ID.upper()} | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    db.init_db()
    config = load_config()

    slave_cfg = get_slave_config(config, SLAVE_ID)
    if not slave_cfg:
        log.error(f"Slave '{SLAVE_ID}' not found in config. Exiting.")
        sys.exit(1)
    if not slave_cfg.get("enabled", True):
        log.info(f"Slave '{SLAVE_ID}' is disabled. Exiting.")
        sys.exit(0)

    copy_settings = slave_cfg.get("copy_settings", {})
    poll_ms = copy_settings.get("poll_interval_ms", 500)
    poll_sec = poll_ms / 1000.0

    time.sleep(2)

    connected = False
    while not connected:
        try:
            connected = connect_slave(config)
        except Exception as e:
            log.error(f"Connection error: {e}")
        if not connected:
            db.set_account_disconnected(SLAVE_ID)
            log.info("Retrying connection in 5 seconds...")
            time.sleep(5)

    db.add_trade_log(TradeAction(
        action="INFO", symbol="", slave_id=SLAVE_ID,
        message=f"[{SLAVE_ID}] Slave executor started and connected."
    ))

    log.info(f"Polling every {poll_ms}ms")

    while True:
        try:
            terminal_info = mt5.terminal_info()
            if terminal_info is None:
                raise ConnectionError("Slave terminal disconnected")

            config = load_config()

            slave_cfg = get_slave_config(config, SLAVE_ID)
            if not slave_cfg or not slave_cfg.get("enabled", True):
                log.info(f"Slave '{SLAVE_ID}' disabled or removed. Exiting.")
                mt5.shutdown()
                sys.exit(0)

            # Re-read poll interval in case it changed
            new_poll = slave_cfg.get("copy_settings", {}).get("poll_interval_ms", 500) / 1000.0
            if new_poll != poll_sec:
                poll_sec = new_poll
                log.info(f"Poll interval changed to {int(poll_sec*1000)}ms")

            process_cycle(config)

        except ConnectionError:
            log.warning("Lost connection to slave terminal. Reconnecting...")
            db.set_account_disconnected(SLAVE_ID)
            db.add_trade_log(TradeAction(
                action="ERROR", symbol="", slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] Lost connection. Reconnecting..."
            ))
            mt5.shutdown()

            connected = False
            while not connected:
                time.sleep(5)
                config = load_config()
                try:
                    connected = connect_slave(config)
                except Exception as e:
                    log.error(f"Reconnection error: {e}")

            db.add_trade_log(TradeAction(
                action="INFO", symbol="", slave_id=SLAVE_ID,
                message=f"[{SLAVE_ID}] Reconnected."
            ))

        except Exception as e:
            log.error(f"Error in slave executor loop: {e}")
            time.sleep(1)

        time.sleep(poll_sec)


if __name__ == "__main__":
    run()
