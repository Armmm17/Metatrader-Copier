"""Process 3: Web Dashboard.

FastAPI application serving the monitoring dashboard and REST API.
All copy_settings and symbol_mapping are per-slave.
"""

import time
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

import db
from models import TradeAction
from copier_logic import load_config, save_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="MT5 Trade Copier Dashboard")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.on_event("startup")
def startup():
    db.init_db()


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# --- Status & Data APIs ---

@app.get("/api/status")
async def api_status():
    status = db.get_copier_state("status") or "unknown"
    start_time = db.get_copier_state("start_time")
    uptime = int(time.time() - float(start_time)) if start_time else 0

    master = db.get_account_info("master")

    config = load_config()
    slaves = {}
    for s in config.get("slaves", []):
        sid = s["id"]
        info = db.get_account_info(sid)
        slaves[sid] = {
            "config": {
                "id": sid,
                "enabled": s.get("enabled", True),
                "login": s.get("login"),
                "server": s.get("server"),
            },
            "account": info,
        }

    return {
        "status": status,
        "uptime_seconds": uptime,
        "master": master,
        "slaves": slaves,
    }


@app.get("/api/positions")
async def api_positions():
    master_positions = db.get_master_positions()
    mappings = db.get_all_mappings()

    mapping_by_key = {}
    for m in mappings:
        mapping_by_key[(m["master_ticket"], m["slave_id"])] = m

    combined = []
    config = load_config()
    slave_ids = [s["id"] for s in config.get("slaves", []) if s.get("enabled", True)]

    for pos in master_positions:
        for sid in slave_ids:
            m = mapping_by_key.get((pos["ticket"], sid))
            combined.append({
                "symbol": pos["symbol"],
                "direction": "BUY" if pos["type"] == 0 else "SELL",
                "master_ticket": pos["ticket"],
                "slave_id": sid,
                "slave_ticket": m["slave_ticket"] if m else None,
                "master_lots": pos["volume"],
                "slave_lots": m["slave_lots"] if m else None,
                "open_price": pos["price_open"],
                "current_price": pos["price_current"],
                "master_pnl": pos["profit"],
                "sl": pos["sl"],
                "tp": pos["tp"],
                "copy_status": m["status"] if m else "Pending",
            })

    master_tickets = {p["ticket"] for p in master_positions}
    for m in mappings:
        if m["master_ticket"] not in master_tickets:
            combined.append({
                "symbol": m["symbol"],
                "direction": m["direction"],
                "master_ticket": m["master_ticket"],
                "slave_id": m["slave_id"],
                "slave_ticket": m["slave_ticket"],
                "master_lots": m["master_lots"],
                "slave_lots": m["slave_lots"],
                "open_price": m["master_open_price"],
                "current_price": None,
                "master_pnl": None,
                "sl": None,
                "tp": None,
                "copy_status": m["status"],
            })

    return combined


@app.get("/api/log")
async def api_log(limit: int = 100, slave_id: str = ""):
    return db.get_trade_log(limit, slave_id)


@app.get("/api/stats")
async def api_stats(slave_id: str = ""):
    return db.get_today_stats(slave_id)


# --- Config Read ---

@app.get("/api/config")
async def api_config():
    """Get config with passwords redacted."""
    config = load_config()
    safe = {
        "master": {
            "terminal_path": config["master"]["terminal_path"],
            "login": config["master"]["login"],
            "server": config["master"]["server"],
        },
        "slaves": [],
        "dashboard": config.get("dashboard", {}),
    }
    for s in config.get("slaves", []):
        safe["slaves"].append({
            "id": s["id"],
            "enabled": s.get("enabled", True),
            "terminal_path": s.get("terminal_path", ""),
            "login": s.get("login", 0),
            "server": s.get("server", ""),
            "copy_settings": s.get("copy_settings", {}),
            "symbol_mapping": s.get("symbol_mapping", {}),
        })
    return safe


@app.get("/api/config/full")
async def api_config_full():
    """Full config including passwords (for settings forms)."""
    return load_config()


# --- Config Write: Master ---

class MasterUpdate(BaseModel):
    terminal_path: str
    login: int
    password: str
    server: str

@app.put("/api/config/master")
async def api_update_master(data: MasterUpdate):
    config = load_config()
    config["master"] = data.dict()
    save_config(config)
    db.add_trade_log(TradeAction(
        action="INFO", symbol="",
        message="Master account configuration updated via dashboard."
    ))
    return {"ok": True}


# --- Config Write: Slaves (includes per-slave copy_settings + symbol_mapping) ---

class CopySettings(BaseModel):
    lot_mode: str = "multiplier"
    lot_multiplier: float = 1.0
    poll_interval_ms: int = 500
    max_open_trades: int = 10
    max_lot_size: float = 5.0
    drawdown_stop_percent: int = 20
    allowed_symbols: list = []

class SlaveCreate(BaseModel):
    id: str
    enabled: bool = True
    terminal_path: str
    login: int
    password: str
    server: str
    copy_settings: CopySettings = CopySettings()
    symbol_mapping: dict = {}

@app.post("/api/config/slaves")
async def api_add_slave(data: SlaveCreate):
    config = load_config()
    slaves = config.get("slaves", [])
    for s in slaves:
        if s["id"] == data.id:
            return JSONResponse(status_code=400, content={"error": f"Slave '{data.id}' already exists"})
    slave_dict = data.dict()
    slave_dict["copy_settings"] = data.copy_settings.dict()
    slaves.append(slave_dict)
    config["slaves"] = slaves
    save_config(config)
    db.add_trade_log(TradeAction(
        action="INFO", symbol="",
        message=f"Slave '{data.id}' added via dashboard."
    ))
    return {"ok": True}


class SlaveUpdate(BaseModel):
    enabled: bool = True
    terminal_path: str
    login: int
    password: str
    server: str
    copy_settings: CopySettings = CopySettings()
    symbol_mapping: dict = {}

@app.put("/api/config/slaves/{slave_id}")
async def api_update_slave(slave_id: str, data: SlaveUpdate):
    config = load_config()
    slaves = config.get("slaves", [])
    found = False
    for i, s in enumerate(slaves):
        if s["id"] == slave_id:
            slave_dict = {"id": slave_id, **data.dict()}
            slave_dict["copy_settings"] = data.copy_settings.dict()
            slaves[i] = slave_dict
            found = True
            break
    if not found:
        return JSONResponse(status_code=404, content={"error": "Slave not found"})
    config["slaves"] = slaves
    save_config(config)
    db.add_trade_log(TradeAction(
        action="INFO", symbol="",
        message=f"Slave '{slave_id}' configuration updated via dashboard."
    ))
    return {"ok": True}


@app.delete("/api/config/slaves/{slave_id}")
async def api_remove_slave(slave_id: str):
    config = load_config()
    slaves = config.get("slaves", [])
    new_slaves = [s for s in slaves if s["id"] != slave_id]
    if len(new_slaves) == len(slaves):
        return JSONResponse(status_code=404, content={"error": "Slave not found"})
    config["slaves"] = new_slaves
    save_config(config)
    db.remove_all_mappings_for_slave(slave_id)
    db.remove_account_info(slave_id)
    db.add_trade_log(TradeAction(
        action="INFO", symbol="",
        message=f"Slave '{slave_id}' removed via dashboard."
    ))
    return {"ok": True}


# --- Controls ---

@app.post("/api/pause")
async def api_pause():
    db.set_copier_state("status", "paused")
    db.add_trade_log(TradeAction(action="INFO", symbol="", message="Copier paused by user."))
    return {"status": "paused"}


@app.post("/api/resume")
async def api_resume():
    db.set_copier_state("status", "running")
    db.add_trade_log(TradeAction(action="INFO", symbol="", message="Copier resumed by user."))
    return {"status": "running"}


@app.post("/api/close_all")
async def api_close_all(slave_id: str = ""):
    if slave_id:
        db.set_copier_state(f"close_all_{slave_id}", "requested")
        msg = f"Emergency close all requested for {slave_id}."
    else:
        db.set_copier_state("close_all", "requested")
        msg = "Emergency close all requested for ALL slaves."
    db.add_trade_log(TradeAction(action="INFO", symbol="", message=msg))
    return {"status": "close_all_requested"}


@app.post("/api/reconnect/{account_type}")
async def api_reconnect(account_type: str):
    db.set_copier_state(f"reconnect_{account_type}", "requested")
    return {"status": f"reconnect_{account_type}_requested"}


def run():
    import uvicorn
    config = load_config()
    host = config.get("dashboard", {}).get("host", "127.0.0.1")
    port = config.get("dashboard", {}).get("port", 8080)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
