"""Launcher: starts master monitor, web dashboard, and one slave executor per slave.

Dynamically detects config changes to start/stop slave processes.

Usage:
    python launcher.py
"""

import sys
import os
import time
import threading
import subprocess
import logging
import json

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] LAUNCHER | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("launcher")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

sys.path.insert(0, BASE_DIR)
import db
db.init_db()
log.info("Database initialized.")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def pipe_reader(name: str, proc: subprocess.Popen):
    """Read stdout from a subprocess line by line and print it."""
    try:
        for line in iter(proc.stdout.readline, b""):
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[{name}] {text}")
    except (OSError, ValueError):
        pass


def start_process(name: str, cmd: list) -> subprocess.Popen:
    """Start a subprocess and attach a reader thread."""
    log.info(f"Starting {name}")
    proc = subprocess.Popen(
        cmd, cwd=BASE_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    t = threading.Thread(target=pipe_reader, args=(name, proc), daemon=True)
    t.start()
    return proc


def main():
    python = sys.executable
    master_script = os.path.join(BASE_DIR, "master_monitor.py")
    slave_script = os.path.join(BASE_DIR, "slave_executor.py")
    dashboard_script = os.path.join(BASE_DIR, "web_dashboard.py")

    # Start fixed processes: master monitor + web dashboard
    master_proc = start_process("Master Monitor", [python, master_script])
    time.sleep(0.5)
    dashboard_proc = start_process("Web Dashboard", [python, dashboard_script])
    time.sleep(0.5)

    # Start slave processes based on config
    slave_procs: dict[str, subprocess.Popen] = {}  # slave_id -> process

    def sync_slaves():
        """Start/stop slave processes to match current config."""
        try:
            config = load_config()
        except Exception as e:
            log.error(f"Failed to read config: {e}")
            return

        configured_ids = set()
        for slave_cfg in config.get("slaves", []):
            sid = slave_cfg["id"]
            if slave_cfg.get("enabled", True):
                configured_ids.add(sid)

        # Start new slaves
        for sid in configured_ids:
            if sid not in slave_procs or slave_procs[sid].poll() is not None:
                proc = start_process(
                    f"Slave [{sid}]",
                    [python, slave_script, "--slave-id", sid]
                )
                slave_procs[sid] = proc

        # Stop removed/disabled slaves
        for sid in list(slave_procs.keys()):
            if sid not in configured_ids:
                proc = slave_procs[sid]
                if proc.poll() is None:
                    log.info(f"Stopping slave [{sid}] (removed/disabled)")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                del slave_procs[sid]

    sync_slaves()
    log.info("All processes started. Press Ctrl+C to stop.")

    try:
        while True:
            # Restart master if it died
            if master_proc.poll() is not None:
                log.warning("Master Monitor died. Restarting in 3s...")
                time.sleep(3)
                master_proc = start_process("Master Monitor", [python, master_script])

            # Restart dashboard if it died
            if dashboard_proc.poll() is not None:
                log.warning("Web Dashboard died. Restarting in 3s...")
                time.sleep(3)
                dashboard_proc = start_process("Web Dashboard", [python, dashboard_script])

            # Sync slave processes with config (handles add/remove/enable/disable)
            sync_slaves()

            time.sleep(5)

    except KeyboardInterrupt:
        log.info("Shutting down all processes...")

        all_procs = [("Master Monitor", master_proc), ("Web Dashboard", dashboard_proc)]
        for sid, proc in slave_procs.items():
            all_procs.append((f"Slave [{sid}]", proc))

        for name, proc in all_procs:
            if proc.poll() is None:
                log.info(f"Terminating {name} (PID {proc.pid})")
                proc.terminate()

        for name, proc in all_procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning(f"Force killing {name}")
                proc.kill()

        log.info("All processes stopped.")


if __name__ == "__main__":
    main()
