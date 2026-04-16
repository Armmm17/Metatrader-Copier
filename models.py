"""Data classes for the MT5 Trade Copier system."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Position:
    """Represents an MT5 position."""
    ticket: int
    symbol: str
    type: int  # 0 = BUY, 1 = SELL
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    time_open: int
    magic: int = 0
    comment: str = ""


@dataclass
class AccountInfo:
    """Represents MT5 account information."""
    connected: bool
    login: int
    server: str
    broker: str
    balance: float
    equity: float
    profit: float
    margin: float
    margin_free: float
    open_positions: int
    daily_pnl: float = 0.0


@dataclass
class TradeAction:
    """Represents a trade action to be logged."""
    action: str  # OPENED, CLOSED, MODIFIED, ERROR, INFO
    symbol: str
    message: str
    master_ticket: Optional[int] = None
    slave_ticket: Optional[int] = None
    pnl: Optional[float] = None
    slave_id: str = ""


@dataclass
class PositionMapping:
    """Maps a master position to a slave position."""
    master_ticket: int
    slave_ticket: int
    symbol: str
    direction: str  # BUY or SELL
    master_lots: float
    slave_lots: float
    master_open_price: float
    slave_open_price: float
    slave_id: str = ""
    status: str = "synced"  # synced, pending, error, closing, closed
    copy_delay_ms: float = 0.0
