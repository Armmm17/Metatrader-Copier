"""Core copier logic: diff detection, lot calculation, symbol mapping, config I/O.

All functions that depend on copy_settings or symbol_mapping now take them
directly (per-slave), rather than reading from the global config.
"""

import json
import os
from typing import Optional

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_slave_config(config: dict, slave_id: str) -> Optional[dict]:
    for s in config.get("slaves", []):
        if s["id"] == slave_id:
            return s
    return None


def map_symbol(symbol: str, symbol_mapping: dict) -> str:
    """Map a master symbol to the slave broker's symbol using the slave's mapping."""
    return symbol_mapping.get(symbol, symbol)


def reverse_map_symbol(slave_symbol: str, symbol_mapping: dict) -> str:
    reverse = {v: k for k, v in symbol_mapping.items()}
    return reverse.get(slave_symbol, slave_symbol)


def calculate_slave_lots(
    master_lots: float,
    copy_settings: dict,
    master_balance: float = 0,
    slave_balance: float = 0,
) -> float:
    mode = copy_settings.get("lot_mode", "mirror")
    max_lot = copy_settings.get("max_lot_size", 100.0)

    if mode == "multiplier":
        lots = master_lots * copy_settings.get("lot_multiplier", 1.0)
    elif mode == "proportional":
        lots = master_lots * (slave_balance / master_balance) if master_balance > 0 else master_lots
    else:  # mirror
        lots = master_lots

    return max(round(min(lots, max_lot), 2), 0.01)


def is_symbol_allowed(symbol: str, copy_settings: dict) -> bool:
    allowed = copy_settings.get("allowed_symbols", [])
    if not allowed:
        return True
    return symbol in allowed


def check_max_trades(current_count: int, copy_settings: dict) -> bool:
    return current_count < copy_settings.get("max_open_trades", 999)


def check_drawdown(equity: float, balance: float, copy_settings: dict) -> bool:
    dd_pct = copy_settings.get("drawdown_stop_percent", 100)
    if balance <= 0:
        return True
    return ((balance - equity) / balance) * 100 < dd_pct


def detect_changes(
    master_positions: list[dict],
    active_mappings: list[dict],
) -> dict:
    master_tickets = {p["ticket"]: p for p in master_positions}
    mapped_tickets = {m["master_ticket"]: m for m in active_mappings}

    new_positions = []
    closed_mappings = []
    sl_tp_changed = []
    partial_closes = []

    for ticket, pos in master_tickets.items():
        if ticket not in mapped_tickets:
            new_positions.append(pos)

    for ticket, mapping in mapped_tickets.items():
        if ticket not in master_tickets:
            if mapping["status"] not in ("closed", "closing"):
                closed_mappings.append(mapping)

    for ticket, mapping in mapped_tickets.items():
        if ticket in master_tickets and mapping["status"] == "synced":
            master_pos = master_tickets[ticket]
            sl_tp_changed.append((mapping, master_pos))
            if master_pos["volume"] < mapping["master_lots"] - 0.001:
                partial_closes.append((mapping, master_pos))

    return {
        "new": new_positions,
        "closed": closed_mappings,
        "sl_tp_changed": sl_tp_changed,
        "partial_close": partial_closes,
    }
