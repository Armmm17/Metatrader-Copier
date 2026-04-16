# MT5 Trade Copier

A real-time MetaTrader 5 trade copier that mirrors positions from one **master** account to one or more **slave** accounts. Each slave runs as an independent process with its own copy settings, symbol mapping, and risk controls. A FastAPI web dashboard provides live monitoring and control.

---

## Architecture

```
launcher.py
├── master_monitor.py     — Polls master MT5; writes positions to shared DB
├── slave_executor.py     — One instance per slave; copies trades from DB
│     slave_executor.py --slave-id slave_2
│     slave_executor.py --slave-id slave_3  ...
└── web_dashboard.py      — FastAPI dashboard + REST API
         ↕
      copier.db  (SQLite WAL — shared state between all processes)
```

The MT5 Python library can only connect to **one terminal per process**, so each account runs in its own process. All inter-process communication goes through the shared SQLite database.

| File | Role |
|---|---|
| `launcher.py` | Starts and supervises all sub-processes; hot-reloads slave list from config |
| `master_monitor.py` | Connects to master terminal, polls positions and account info |
| `slave_executor.py` | Per-slave trade execution: open, close, modify SL/TP, partial close |
| `web_dashboard.py` | REST API + HTML dashboard (FastAPI + Jinja2) |
| `db.py` | SQLite helpers (WAL mode, shared across all processes) |
| `copier_logic.py` | Pure logic: lot calculation, symbol mapping, change detection |
| `models.py` | Dataclasses: `Position`, `AccountInfo`, `TradeAction`, `PositionMapping` |
| `config.json` | All credentials and per-slave settings |

---

## Requirements

- Windows (MT5 Python API is Windows-only)
- Python 3.11+
- MetaTrader 5 terminals installed (one per account), each logged in with **"Allow Algo Trading"** enabled (Tools → Options → Expert Advisors)

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Quick Start

1. Install dependencies: `pip install -r requirements.txt`
2. Edit `config.json` with real credentials and terminal paths (see below)
3. Start both MT5 terminals and log in to each account
4. Run the launcher:

```bash
python launcher.py
```

The launcher starts:
- The master monitor
- The web dashboard
- One slave executor per enabled slave

Open the dashboard at `http://127.0.0.1:6535` (or whichever host/port you configured).

To stop, press `Ctrl+C`. All sub-processes are terminated gracefully.

---

## Configuration (`config.json`)

### Master

```json
"master": {
  "terminal_path": "C:\\Path\\To\\MT5_Master\\terminal64.exe",
  "login": 12345678,
  "password": "yourpassword",
  "server": "BrokerName-Server"
}
```

### Slaves

Each slave entry is fully independent:

```json
"slaves": [
  {
    "id": "slave_1",
    "enabled": true,
    "terminal_path": "C:\\Path\\To\\MT5_Slave\\terminal64.exe",
    "login": 87654321,
    "password": "slavepassword",
    "server": "BrokerName-Server",
    "copy_settings": {
      "lot_mode": "multiplier",
      "lot_multiplier": 1.0,
      "poll_interval_ms": 50,
      "max_open_trades": 10,
      "max_lot_size": 5.0,
      "drawdown_stop_percent": 20,
      "allowed_symbols": ["GOLD", "SILVER"]
    },
    "symbol_mapping": {
      "GOLD": "XAUUSD",
      "SILVER": "XAGUSD"
    }
  }
]
```

Set `"enabled": false` to disable a slave without removing it. The launcher will stop its process within 5 seconds.

#### copy_settings reference

| Key | Type | Description |
|---|---|---|
| `lot_mode` | `"mirror"` \| `"multiplier"` \| `"proportional"` | How slave lot size is calculated |
| `lot_multiplier` | float | Multiplied by master lots when `lot_mode = "multiplier"` |
| `poll_interval_ms` | int | How often the slave checks for changes (milliseconds) |
| `max_open_trades` | int | Max simultaneous copied positions; new trades are skipped beyond this |
| `max_lot_size` | float | Hard cap on any single copied lot size |
| `drawdown_stop_percent` | int | Pause copying when `(balance − equity) / balance` exceeds this % |
| `allowed_symbols` | list of strings | Whitelist of master symbols to copy; empty list = copy all |

**Lot modes explained:**

| Mode | Formula |
|---|---|
| `mirror` | slave lots = master lots |
| `multiplier` | slave lots = master lots × `lot_multiplier` |
| `proportional` | slave lots = master lots × (slave balance / master balance) |

Result is always rounded to 2 decimal places and clamped between `0.01` and `max_lot_size`.

#### symbol_mapping

Maps master symbol names to the slave broker's equivalent. Symbols not listed are passed through unchanged.

```json
"symbol_mapping": {
  "GOLD": "XAUUSD",
  "SILVER": "XAGUSD"
}
```

### Dashboard

```json
"dashboard": {
  "host": "127.0.0.1",
  "port": 6535
}
```

---

## What Gets Copied

| Master action | Slave action |
|---|---|
| New position opened | Open matching position (same direction, mapped symbol, calculated lots) |
| Position closed | Close matching position |
| SL or TP modified | Update slave SL/TP to match |
| Partial close | Proportionally reduce slave volume |

Copy delay (time from detection to execution) is measured and stored per trade.

---

## Safety Features

- **Drawdown protection** — copying pauses for a slave when its drawdown exceeds `drawdown_stop_percent`; resumes automatically when equity recovers
- **Max trades limit** — new trades are skipped (and logged) when the slave already has `max_open_trades` open
- **Max lot cap** — lots are capped at `max_lot_size` regardless of other settings
- **Symbol filter** — only symbols in `allowed_symbols` are copied
- **Order retries** — failed orders retry up to 3 times with refreshed prices before giving up
- **Auto-reconnect** — master and slave executors reconnect automatically if the terminal disconnects
- **Process watchdog** — the launcher restarts the master monitor and dashboard if they crash

---

## Web Dashboard

Open `http://127.0.0.1:6535` after starting the launcher.

The dashboard displays:
- Master and slave account info (balance, equity, floating P&L, connection status)
- All open positions and their copy status per slave
- Trade activity log (OPENED, CLOSED, MODIFIED, ERROR, INFO)
- Today's performance stats: trades copied, wins, losses, net P&L, win rate, average copy delay

### Controls

| Control | Action |
|---|---|
| Pause | Suspend all copying (open positions remain on the slave) |
| Resume | Re-enable copying |
| Close All | Emergency close all positions on all slaves |
| Close All (per slave) | Emergency close all positions on a specific slave |

---

## REST API Reference

The dashboard exposes a REST API at the same host/port.

### Status & data

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Copier status, uptime, master & slave account info |
| GET | `/api/positions` | All open positions with copy mapping per slave |
| GET | `/api/log` | Trade log (`?limit=100&slave_id=slave_1`) |
| GET | `/api/stats` | Today's performance stats (`?slave_id=slave_1`) |
| GET | `/api/config` | Config (passwords redacted) |
| GET | `/api/config/full` | Full config including passwords |

### Configuration

| Method | Path | Body | Description |
|---|---|---|---|
| PUT | `/api/config/master` | `MasterUpdate` | Update master credentials |
| POST | `/api/config/slaves` | `SlaveCreate` | Add a new slave |
| PUT | `/api/config/slaves/{slave_id}` | `SlaveUpdate` | Update slave settings |
| DELETE | `/api/config/slaves/{slave_id}` | — | Remove a slave |

Config changes take effect on the next poll cycle — no restart required. The launcher picks up added/removed/enabled/disabled slaves within 5 seconds.

### Controls

| Method | Path | Description |
|---|---|---|
| POST | `/api/pause` | Pause all copying |
| POST | `/api/resume` | Resume copying |
| POST | `/api/close_all` | Emergency close all (`?slave_id=slave_1` for a specific slave) |

---

## Running Processes Individually

Each process can be run standalone for debugging:

```bash
python master_monitor.py
python slave_executor.py --slave-id slave_1
python web_dashboard.py
```

---

## Database

State is stored in `copier.db` (SQLite, WAL mode for concurrent multi-process access). The file is created automatically on first run.

| Table | Contents |
|---|---|
| `master_positions` | Current snapshot of master open positions |
| `position_mapping` | Master ↔ slave ticket mappings, one row per (master_ticket, slave_id) |
| `account_info` | Balance, equity, etc. for master and each slave |
| `trade_log` | Append-only log of all trade actions with timestamps |
| `copier_state` | Key-value store for runtime state (pause flag, close_all requests, drawdown flags) |

If the schema version in `db.py` is bumped, all tables are dropped and recreated on the next startup.

---

## Folder Structure

```
Copier/
├── launcher.py          # Entry point — run this
├── master_monitor.py
├── slave_executor.py
├── web_dashboard.py
├── copier_logic.py
├── db.py
├── models.py
├── config.json          # Edit this before starting
├── copier.db            # Auto-created at runtime
├── requirements.txt
├── templates/
│   └── dashboard.html
└── static/
    └── style.css
```
