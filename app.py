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
DATA_DIR = BASE_DIR  # CSVs sit next to app.py in the repo, not in a data/ subfolder

app = Flask(__name__)

# ---------------------------------------------------------------------
# One shared draft, one lock. Three people hitting the same tracker at
# once is exactly the concurrency this needs to guard against.
# ---------------------------------------------------------------------
_lock = threading.Lock()
_reset_lock = threading.Lock()  # only one reset/rebuild at a time

# When it's an alliance team's turn (my_slot + allies), automatically run the
# top-3 recommendation and post it to the shared log. Set AUTO_TOP3=0 on Render
# (Environment tab) to turn it off without touching code.
AUTO_TOP3 = os.environ.get("AUTO_TOP3", "1").strip().lower() not in ("0", "false", "no", "off")

# How many players are draftable, best ADP/rank first. Override with the
# POOL_SIZE environment variable (e.g. on Render) without touching code.
POOL_SIZE = int(os.environ.get("POOL_SIZE", engine.DEFAULT_POOL_SIZE))


def _build_draft():
    """Builds a brand-new, empty draft (same settings every time)."""
    return engine.build_tracker(
        data_dir=DATA_DIR,
        my_slot=9, teams=10, rounds=14, reversal_round=3,
        alliance_allies=(6, 7), auto_recommend_teams=(),  # web UI: nobody's auto-spammed
        pool_size=POOL_SIZE,
    )


print(f"Loading projections and building the draft board ({POOL_SIZE} players, a few seconds)...")
TRACKER, WEEKLY = _build_draft()
print("Draft board ready.")

# Bumped every time the draft is reset. Browsers compare it to the one they
# last saw and wipe their log when it changes.
EPOCH = 0

# Rolling log of every command run so far, shared by all clients.
# Each entry: {id, ts, user, command, output, data}
# `data` is optional structured info (top-3 table, catrank, h2hstand, playoff bracket) that the
# page renders as HTML; `output` is always the plain-text version.
HISTORY = []
_next_id = 1
HISTORY_CAP = 500  # trim so a long draft night doesn't grow this unbounded


def _append_history(user, command, output, data=None):
    global _next_id
    HISTORY.append({
        "id": _next_id,
        "ts": time.time(),
        "user": user or "?",
        "command": command,
        "output": output,
        "data": data,
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
        "picks": TRACKER.overall - 1,
        "epoch": EPOCH,
    }


def _clock_key():
    """(overall pick number, team on the clock) -- changes whenever the turn moves."""
    if TRACKER.overall > len(TRACKER.pick_order):
        return (TRACKER.overall, None)
    return (TRACKER.overall, TRACKER.pick_order[TRACKER.overall - 1])


def _auto_top3_worker(tracker, overall, team, epoch):
    """Runs in a background thread so the pick that triggered it returns instantly.
    Re-checks under the lock that the draft hasn't moved on (or been reset) while
    waiting; if it has, the recommendation would be stale, so it quietly drops it."""
    with _lock:
        if TRACKER is not tracker or EPOCH != epoch or _clock_key() != (overall, team):
            return
        TRACKER.last_report = None
        try:
            output = engine.dispatch_command(TRACKER, f"top3 {team}", WEEKLY)
        except Exception as e:
            output = f"[!] Auto top-3 failed: {e}"
        data = getattr(TRACKER, "last_report", None)
        TRACKER.last_report = None
        _append_history("auto", f"top3 (auto, pick #{overall}, Team {team})", output, data)


def _maybe_auto_top3(before_key):
    """Call while holding _lock, after a command. If the turn moved to an alliance
    team, kick off the top-3 in the background."""
    if not AUTO_TOP3:
        return
    after_key = _clock_key()
    overall, team = after_key
    if after_key == before_key or team is None:
        return
    alliance = set(TRACKER.alliance_allies or ()) | {TRACKER.my_slot}
    if team not in alliance:
        return
    threading.Thread(target=_auto_top3_worker, args=(TRACKER, overall, team, EPOCH),
                     daemon=True).start()


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(_state_snapshot())


@app.route("/api/history")
def api_history():
    """Poll endpoint: ?since=<last id you already have>&epoch=<epoch you last saw>.
    Returns new entries only -- unless the draft was reset since `epoch`, in which
    case it returns the whole (fresh) log and reset=true so the page clears itself."""
    since = request.args.get("since", 0, type=int)
    client_epoch = request.args.get("epoch", -1, type=int)
    with _lock:
        reset = client_epoch != EPOCH
        if reset:
            since = 0
        new_entries = [h for h in HISTORY if h["id"] > since]
        state = _state_snapshot()
    return jsonify({"entries": new_entries, "state": state, "epoch": state["epoch"], "reset": reset})


@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    user = (body.get("user") or "").strip() or "anon"
    if not text:
        return jsonify({"error": "empty command"}), 400
    with _lock:
        before_key = _clock_key()
        TRACKER.last_report = None  # so a stale table never rides along with a different command
        try:
            output = engine.dispatch_command(TRACKER, text, WEEKLY)
        except Exception as e:
            output = f"[!] Error: {e}"
        data = getattr(TRACKER, "last_report", None)
        TRACKER.last_report = None
        _append_history(user, text, output, data)
        _maybe_auto_top3(before_key)
        state = _state_snapshot()
    return jsonify({"output": output, "data": data, "state": state})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Erases the whole draft: every pick, the shared log, and any draft-mode
    change (snake/3rr). Needs {"confirm": true} in the body so a stray request
    can't wipe a draft by accident -- the web page only sends it after the
    user clicks through the confirmation dialog."""
    global TRACKER, WEEKLY, EPOCH
    body = request.get_json(force=True, silent=True) or {}
    user = (body.get("user") or "").strip() or "anon"
    if body.get("confirm") is not True:
        return jsonify({"error": "reset needs confirm=true"}), 400
    if not _reset_lock.acquire(blocking=False):
        return jsonify({"error": "a reset is already in progress"}), 409
    try:
        # Build the fresh draft OUTSIDE the main lock so other people's polling
        # keeps working during the few seconds it takes, then swap it in.
        new_tracker, new_weekly = _build_draft()
        with _lock:
            n_picks = TRACKER.overall - 1
            TRACKER, WEEKLY = new_tracker, new_weekly
            HISTORY.clear()
            EPOCH += 1
            _append_history(user, "reset",
                            f"Draft reset by {user}. All {n_picks} pick(s) and the log were erased.",
                            {"kind": "reset", "picks_erased": n_picks})
            state = _state_snapshot()
    except Exception as e:
        return jsonify({"error": f"reset failed: {e}"}), 500
    finally:
        _reset_lock.release()
    return jsonify({"ok": True, "state": state})


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
