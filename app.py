# -*- coding: utf-8 -*-
"""
Draft Day web app -- thin Flask wrapper around engine.py.

Run:
    pip install -r requirements.txt
    python app.py

Then open http://<this-machine's-LAN-IP>:5000 on up to 3 devices on the
same network (find the IP with `ipconfig`/`ifconfig`; the terminal also
prints it on startup). Everyone sees the same shared draft state and the
same scrolling command log, refreshed by polling every 2 seconds -- no
websocket server needed, keeps this simple.
"""
import os
import threading
import time
import warnings
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

warnings.filterwarnings("ignore")  # pandas perf-warnings from engine.py, not errors

import engine

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

app = Flask(__name__, static_folder="static", static_url_path="")

# ---------------------------------------------------------------------
# One shared draft, one lock. Three people hitting the same tracker at
# once is exactly the concurrency this needs to guard against.
# ---------------------------------------------------------------------
_lock = threading.Lock()
print("Loading projections and building the draft board (a few seconds)...")
TRACKER, WEEKLY = engine.build_tracker(
    data_dir=DATA_DIR,
    my_slot=9, teams=10, rounds=14, reversal_round=3,
    alliance_allies=(6, 7), auto_recommend_teams=(),  # web UI: nobody's auto-spammed
)
print("Draft board ready.")

# Rolling log of every command run so far, shared by all clients.
# Each entry: {id, ts, user, command, output}
HISTORY = []
_next_id = 1
HISTORY_CAP = 500  # trim so a long draft night doesn't grow this unbounded


def _append_history(user, command, output):
    global _next_id
    HISTORY.append({
        "id": _next_id,
        "ts": time.time(),
        "user": user or "?",
        "command": command,
        "output": output,
    })
    _next_id += 1
    if len(HISTORY) > HISTORY_CAP:
        del HISTORY[: len(HISTORY) - HISTORY_CAP]


def _state_snapshot():
    on_clock = TRACKER.pick_order[TRACKER.overall - 1] if TRACKER.overall <= len(TRACKER.pick_order) else None
    return {
        "overall": TRACKER.overall,
        "round": TRACKER.current_round if on_clock is not None else None,
        "on_the_clock": on_clock,
        "draft_complete": on_clock is None,
        "teams": TRACKER.teams,
        "rounds": TRACKER.rounds,
    }


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(_state_snapshot())


@app.route("/api/history")
def api_history():
    """Poll endpoint: ?since=<last id you already have> -> new entries only."""
    since = request.args.get("since", 0, type=int)
    with _lock:
        new_entries = [h for h in HISTORY if h["id"] > since]
        state = _state_snapshot()
    return jsonify({"entries": new_entries, "state": state})


@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    user = (body.get("user") or "").strip() or "anon"
    if not text:
        return jsonify({"error": "empty command"}), 400
    with _lock:
        try:
            output = engine.dispatch_command(TRACKER, text, WEEKLY)
        except Exception as e:
            output = f"[!] Error: {e}"
        _append_history(user, text, output)
        state = _state_snapshot()
    return jsonify({"output": output, "state": state})


if __name__ == "__main__":
    # Render (and most hosts) inject PORT; fall back to 5000 for local runs.
    port = int(os.environ.get("PORT", 5000))
    import socket
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan_ip = "<your-LAN-IP>"
    print(f"\nOpen http://127.0.0.1:{port} on this machine, or http://{lan_ip}:{port} from another device on the same network.\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
