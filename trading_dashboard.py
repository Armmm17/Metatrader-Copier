"""Process 4: Trading Dashboard.

Real-time XAUUSD trading chart with TradingView Lightweight Charts v4,
live MT5 position overlays, P&L tracking, and WebSocket streaming.

Connects to the master MT5 terminal for tick data and candle history.
Falls back to demo mode if MT5 is unavailable.
"""

import asyncio
import json
import os
import sys
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] TRADING | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("trading_dashboard")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from copier_logic import load_config

app = FastAPI(title="MT5 Trading Dashboard")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# --- MT5 Connection ---

mt5 = None
mt5_connected = False
demo_mode = False


def init_mt5():
    """Try to connect to the master MT5 terminal."""
    global mt5, mt5_connected, demo_mode
    try:
        import MetaTrader5 as _mt5
        mt5 = _mt5

        config = load_config()
        master = config["master"]
        trading_cfg = config.get("trading_dashboard", {})
        symbol = trading_cfg.get("symbol", "GOLD")

        if not mt5.initialize(
            path=master["terminal_path"],
            login=master["login"],
            password=master["password"],
            server=master["server"],
        ):
            error = mt5.last_error()
            log.warning(f"MT5 init failed: {error}. Starting in demo mode.")
            demo_mode = True
            mt5_connected = False
            return

        info = mt5.account_info()
        if info is None:
            log.warning("MT5 account info unavailable. Starting in demo mode.")
            demo_mode = True
            mt5_connected = False
            mt5.shutdown()
            return

        mt5_connected = True
        demo_mode = False
        log.info(f"Connected to MT5: {info.company} | #{info.login} | Symbol: {symbol}")

    except ImportError:
        log.warning("MetaTrader5 package not found. Starting in demo mode.")
        demo_mode = True
        mt5_connected = False


@app.on_event("startup")
def startup():
    init_mt5()


@app.on_event("shutdown")
def shutdown():
    global mt5, mt5_connected
    if mt5 and mt5_connected:
        mt5.shutdown()
        mt5_connected = False


# --- Helper: Get trading symbol ---

def get_symbol() -> str:
    config = load_config()
    return config.get("trading_dashboard", {}).get("symbol", "GOLD")


# --- Demo Data Generator ---

class DemoDataGen:
    """Generates realistic fake XAUUSD data for demo mode."""

    def __init__(self):
        self.base_price = 2650.0
        self.current_price = self.base_price
        self.tick_count = 0

    def generate_candles(self, timeframe: str, count: int) -> list[dict]:
        """Generate historical candle data."""
        tf_seconds = {
            "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
            "H1": 3600, "H4": 14400, "D1": 86400,
        }
        interval = tf_seconds.get(timeframe, 60)
        now = int(time.time())
        candles = []
        price = self.base_price - (count * 0.5)

        for i in range(count):
            t = now - (count - i) * interval
            o = price + random.uniform(-2, 2)
            h = o + random.uniform(0.5, 8)
            l = o - random.uniform(0.5, 8)
            c = random.uniform(l, h)
            v = random.randint(100, 5000)
            candles.append({
                "time": t,
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": v,
            })
            price = c

        self.current_price = price
        return candles

    def generate_tick(self) -> dict:
        """Generate a single tick."""
        self.tick_count += 1
        change = random.gauss(0, 0.3)
        self.current_price += change
        spread = random.uniform(0.1, 0.5)
        return {
            "time": int(time.time()),
            "bid": round(self.current_price, 2),
            "ask": round(self.current_price + spread, 2),
            "last": round(self.current_price + spread / 2, 2),
            "volume": random.randint(1, 50),
        }

    def generate_positions(self) -> list[dict]:
        """Generate fake open positions."""
        if random.random() > 0.7:
            return []
        direction = random.choice(["BUY", "SELL"])
        entry = round(self.current_price - random.uniform(-5, 5), 2)
        lots = round(random.choice([0.01, 0.05, 0.1, 0.5, 1.0]), 2)
        pnl = round((self.current_price - entry) * lots * 100 * (1 if direction == "BUY" else -1), 2)
        return [{
            "ticket": 10000 + self.tick_count % 5,
            "symbol": get_symbol(),
            "direction": direction,
            "volume": lots,
            "price_open": entry,
            "price_current": round(self.current_price, 2),
            "sl": round(entry - 10 if direction == "BUY" else entry + 10, 2),
            "tp": round(entry + 20 if direction == "BUY" else entry - 20, 2),
            "profit": pnl,
            "time_open": int(time.time()) - random.randint(60, 3600),
            "magic": 123456,
            "comment": "demo",
        }]

    def generate_trades(self, count: int = 20) -> list[dict]:
        """Generate fake trade history."""
        trades = []
        t = int(time.time())
        for i in range(count):
            direction = random.choice(["BUY", "SELL"])
            entry = round(self.base_price + random.uniform(-20, 20), 2)
            exit_p = round(entry + random.uniform(-10, 10), 2)
            lots = round(random.choice([0.01, 0.05, 0.1, 0.5]), 2)
            pnl = round((exit_p - entry) * lots * 100 * (1 if direction == "BUY" else -1), 2)
            trades.append({
                "ticket": 9000 + i,
                "symbol": get_symbol(),
                "direction": direction,
                "volume": lots,
                "price_open": entry,
                "price_close": exit_p,
                "profit": pnl,
                "time_open": t - (count - i) * 3600,
                "time_close": t - (count - i) * 3600 + random.randint(60, 3000),
            })
        return trades


demo = DemoDataGen()


# --- MT5 Data Functions ---

TIMEFRAME_MAP = {}


def _init_timeframe_map():
    global TIMEFRAME_MAP
    if mt5 and not TIMEFRAME_MAP:
        TIMEFRAME_MAP = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }


def get_candles_mt5(symbol: str, timeframe: str, count: int) -> list[dict]:
    """Fetch candle data from MT5."""
    _init_timeframe_map()
    tf = TIMEFRAME_MAP.get(timeframe, mt5.TIMEFRAME_M1)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return []
    candles = []
    for r in rates:
        candles.append({
            "time": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": int(r[5]),
        })
    return candles


def get_tick_mt5(symbol: str) -> Optional[dict]:
    """Get latest tick from MT5."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return {
        "time": int(tick.time),
        "bid": float(tick.bid),
        "ask": float(tick.ask),
        "last": float(tick.last),
        "volume": int(tick.volume),
    }


def get_positions_mt5(symbol: str) -> list[dict]:
    """Get open positions for symbol from MT5."""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return []
    result = []
    for p in positions:
        result.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "direction": "BUY" if p.type == 0 else "SELL",
            "volume": p.volume,
            "price_open": p.price_open,
            "price_current": p.price_current,
            "sl": p.sl,
            "tp": p.tp,
            "profit": p.profit,
            "time_open": p.time,
            "magic": p.magic,
            "comment": p.comment if hasattr(p, "comment") else "",
        })
    return result


def get_deals_mt5(symbol: str, count: int = 50) -> list[dict]:
    """Get recent deal history from MT5."""
    from_date = datetime.now(timezone.utc) - timedelta(days=30)
    to_date = datetime.now(timezone.utc)
    deals = mt5.history_deals_get(from_date, to_date, group=f"*{symbol}*")
    if deals is None:
        return []

    # Pair entry/exit deals into trades
    entries = {}
    trades = []
    for d in deals:
        if d.entry == 0:  # DEAL_ENTRY_IN
            entries[d.position_id] = d
        elif d.entry == 1:  # DEAL_ENTRY_OUT
            entry_deal = entries.get(d.position_id)
            if entry_deal:
                direction = "BUY" if entry_deal.type == 0 else "SELL"
                trades.append({
                    "ticket": d.position_id,
                    "symbol": d.symbol,
                    "direction": direction,
                    "volume": entry_deal.volume,
                    "price_open": entry_deal.price,
                    "price_close": d.price,
                    "profit": d.profit,
                    "time_open": entry_deal.time,
                    "time_close": d.time,
                })

    return trades[-count:]


# --- REST Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(BASE_DIR, "static", "trading.html")
    return FileResponse(path, media_type="text/html")


@app.get("/api/candles")
async def api_candles(
    symbol: str = "",
    timeframe: str = "M1",
    count: int = 500,
):
    if not symbol:
        symbol = get_symbol()
    if count > 5000:
        count = 5000

    if mt5_connected and not demo_mode:
        candles = get_candles_mt5(symbol, timeframe, count)
        if candles:
            return {"symbol": symbol, "timeframe": timeframe, "candles": candles, "demo": False}

    # Fallback to demo
    candles = demo.generate_candles(timeframe, count)
    return {"symbol": symbol, "timeframe": timeframe, "candles": candles, "demo": True}


@app.get("/api/positions")
async def api_positions(symbol: str = ""):
    if not symbol:
        symbol = get_symbol()

    if mt5_connected and not demo_mode:
        positions = get_positions_mt5(symbol)
        return {"positions": positions, "demo": False}

    return {"positions": demo.generate_positions(), "demo": True}


@app.get("/api/trades")
async def api_trades(symbol: str = "", count: int = 50):
    if not symbol:
        symbol = get_symbol()

    if mt5_connected and not demo_mode:
        trades = get_deals_mt5(symbol, count)
        return {"trades": trades, "demo": False}

    return {"trades": demo.generate_trades(count), "demo": True}


@app.get("/api/account")
async def api_account():
    if mt5_connected and not demo_mode:
        info = mt5.account_info()
        if info:
            return {
                "balance": info.balance,
                "equity": info.equity,
                "profit": info.profit,
                "margin": info.margin,
                "margin_free": info.margin_free,
                "demo": False,
            }

    return {
        "balance": 10000.0,
        "equity": 10000.0 + random.uniform(-200, 200),
        "profit": round(random.uniform(-200, 200), 2),
        "margin": round(random.uniform(0, 500), 2),
        "margin_free": round(random.uniform(8000, 10000), 2),
        "demo": True,
    }


@app.get("/api/chart/status")
async def api_chart_status():
    return {
        "mt5_connected": mt5_connected,
        "demo_mode": demo_mode,
        "symbol": get_symbol(),
    }


# --- WebSocket Streaming ---

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info(f"WebSocket connected. Active: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        log.info(f"WebSocket disconnected. Active: {len(self.active)}")

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    symbol = get_symbol()

    try:
        while True:
            # Get tick data
            if mt5_connected and not demo_mode:
                tick = get_tick_mt5(symbol)
                positions = get_positions_mt5(symbol)
                is_demo = False
            else:
                tick = demo.generate_tick()
                positions = demo.generate_positions()
                is_demo = True

            if tick:
                payload = {
                    "type": "update",
                    "tick": tick,
                    "positions": positions,
                    "demo": is_demo,
                    "timestamp": int(time.time()),
                }
                await ws.send_json(payload)

            await asyncio.sleep(1)
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        log.error(f"WebSocket error: {e}")
        manager.disconnect(ws)


# --- Run ---

def run():
    import uvicorn
    config = load_config()
    trading_cfg = config.get("trading_dashboard", {})
    host = trading_cfg.get("host", "127.0.0.1")
    port = trading_cfg.get("port", 8000)
    log.info(f"Starting Trading Dashboard on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
