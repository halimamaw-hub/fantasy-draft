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
import math
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

# Teams whose automatic top-3 is temporarily suspended (toggled from the page or
# with the `pause` / `resume` commands). This is a live setting, not draft state:
# it survives a draft reset, but not a server restart. Manual `top3 <team>` still
# works for a paused team.
AUTO_PAUSED = set()

# "Long" mode: gives every top-3 (auto, typed/clicked `top3`, and `check`) a
# longer Monte Carlo time budget than the engine's 4 s default. ON by default;
# turn it off/on from the page or with `long off` / `long on`. Like AUTO_PAUSED
# this is a live setting: it survives a draft reset, but not a server restart.
# Start it off on Render with LONG_MODE=0; change the length (seconds) with
# LONG_TOP3_SECONDS (default: engine.LONG_TOP3_TIME_BUDGET_SEC, 10).
LONG_MODE = os.environ.get("LONG_MODE", "1").strip().lower() not in ("0", "false", "no", "off")
try:
    LONG_SECONDS = float(os.environ.get("LONG_TOP3_SECONDS", engine.LONG_TOP3_TIME_BUDGET_SEC))
    if LONG_SECONDS <= 0:
        raise ValueError
except ValueError:
    LONG_SECONDS = float(engine.LONG_TOP3_TIME_BUDGET_SEC)


def _top3_budget():
    """Seconds of Monte Carlo time to give the next top-3 / check (None = the
    engine's normal default)."""
    return LONG_SECONDS if LONG_MODE else None


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
    alliance = sorted({TRACKER.my_slot} | set(TRACKER.alliance_allies or ()))
    return {
        "overall": TRACKER.overall,
        "round": TRACKER.current_round if on_clock is not None else None,
        "on_the_clock": on_clock,
        "draft_complete": on_clock is None,
        "teams": TRACKER.teams,
        "rounds": TRACKER.rounds,
        "picks": TRACKER.overall - 1,
        "epoch": EPOCH,
        "alliance_teams": alliance,
        "on_clock_is_alliance": on_clock in alliance if on_clock is not None else False,
        "auto_top3_enabled": AUTO_TOP3,
        "auto_top3_paused": sorted(AUTO_PAUSED),
        "long_mode": LONG_MODE,
        "long_seconds": LONG_SECONDS,
        "default_seconds": float(engine.TOP3_TIME_BUDGET_SEC),
    }


# ---------------------------------------------------------------------
# Live report cache (catrank / h2hstand / playoffbracket, any projection
# source). Keyed by (kind, source) only -- each entry just remembers which
# (epoch, overall) it was computed for, and is recomputed the next time it's
# asked for if the draft has moved on since. This is what makes the "Live
# Standings" tab real-time without recomputing a Monte-Carlo bracket sim on
# every 3-second poll from every browser tab: unchanged state is a cache hit.
# ---------------------------------------------------------------------
REPORT_KINDS = {"catrank", "h2hstand", "playoffbracket"}
REPORT_SOURCES = {"", "espn", "roto", "rank", "table", "yahoo", "fantrax"}
REPORT_CACHE = {}


def _get_report(kind, source):
    cmd_text = (source + kind) if source else kind
    cache_key = (kind, source)
    with _lock:
        cached = REPORT_CACHE.get(cache_key)
        if cached and cached["epoch"] == EPOCH and cached["overall"] == TRACKER.overall:
            output, data = cached["output"], cached["data"]
        else:
            TRACKER.last_report = None
            try:
                output = engine.dispatch_command(TRACKER, cmd_text, WEEKLY)
            except Exception as e:
                output = f"[!] Error: {e}"
            data = getattr(TRACKER, "last_report", None)
            TRACKER.last_report = None
            REPORT_CACHE[cache_key] = {
                "epoch": EPOCH, "overall": TRACKER.overall, "output": output, "data": data,
            }
        state = _state_snapshot()
    return output, data, state


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
        if team in AUTO_PAUSED:  # paused while this was queued
            return
        TRACKER.last_report = None
        try:
            output = engine.dispatch_command(TRACKER, f"top3 {team}", WEEKLY,
                                             time_budget=_top3_budget())
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
    if team not in alliance or team in AUTO_PAUSED:
        return
    threading.Thread(target=_auto_top3_worker, args=(TRACKER, overall, team, EPOCH),
                     daemon=True).start()


def _alliance_teams():
    return {TRACKER.my_slot} | set(TRACKER.alliance_allies or ())


def _set_auto_paused(user, teams, paused):
    """Call while holding _lock. Pauses/resumes the auto top-3 for `teams` and
    logs it. Returns (message, error). Resuming a team that is on the clock right
    now immediately posts its recommendation, so it isn't left without one."""
    allowed = _alliance_teams()
    bad = [t for t in teams if t not in allowed]
    if bad:
        return None, f"Auto top-3 only runs for alliance teams {sorted(allowed)} (not {bad})."
    changed = [t for t in sorted(teams) if (t not in AUTO_PAUSED) == paused]
    for t in changed:
        (AUTO_PAUSED.add if paused else AUTO_PAUSED.discard)(t)
    verb = "paused" if paused else "resumed"
    if not changed:
        msg = f"Auto top-3 already {verb} for Team {', '.join(map(str, sorted(teams)))}."
    else:
        msg = f"Auto top-3 {verb} for Team {', '.join(map(str, changed))}."
    still = sorted(AUTO_PAUSED)
    msg += f" Currently paused: {', '.join('Team ' + str(t) for t in still) if still else 'none'}."
    if not AUTO_TOP3:
        msg += " (Note: AUTO_TOP3 is set to off on the server, so nothing auto-runs at all.)"
    _append_history(user, ("pause " if paused else "resume ") + " ".join(map(str, sorted(teams))),
                    msg, {"kind": "auto_top3", "paused": still})
    if not paused and AUTO_TOP3 and changed:
        overall, team = _clock_key()
        if team in changed:
            threading.Thread(target=_auto_top3_worker, args=(TRACKER, overall, team, EPOCH),
                             daemon=True).start()
    return msg, None


def _parse_pause_command(text):
    """'pause 6' / 'resume all' / 'pause 6 7' -> (paused, teams) or None if it
    isn't a pause/resume command. Raises ValueError for a malformed one."""
    parts = text.strip().lower().split()
    if not parts or parts[0] not in ("pause", "resume", "unpause"):
        return None
    paused = parts[0] == "pause"
    args = parts[1:]
    if not args:
        raise ValueError(f"usage: {parts[0]} <team number> [more teams] | {parts[0]} all")
    if args == ["all"]:
        return paused, set(_alliance_teams())
    try:
        return paused, {int(a.strip(",")) for a in args}
    except ValueError:
        raise ValueError(f"usage: {parts[0]} <team number> [more teams] | {parts[0]} all")


def _set_long_mode(user, enabled):
    """Call while holding _lock. Turns long mode on/off and logs it."""
    global LONG_MODE
    changed = LONG_MODE != enabled
    LONG_MODE = enabled
    if enabled:
        msg = (f"Long mode {'ON' if changed else 'already on'}: top-3 and player checks now get "
               f"{LONG_SECONDS:.0f}s of Monte Carlo time.")
    else:
        msg = (f"Long mode {'OFF' if changed else 'already off'}: top-3 and player checks use the "
               f"normal {engine.TOP3_TIME_BUDGET_SEC:.0f}s limit.")
    _append_history(user, "long on" if enabled else "long off", msg,
                    {"kind": "long_mode", "enabled": LONG_MODE, "seconds": LONG_SECONDS})
    return msg


def _parse_long_command(text):
    """'long' / 'long on' / 'long off' -> ('status'|'on'|'off') or None if it
    isn't a long command. Raises ValueError for a malformed one."""
    parts = text.strip().lower().split()
    if not parts or parts[0] != "long":
        return None
    if len(parts) == 1 or parts[1] == "status":
        return "status"
    if len(parts) == 2 and parts[1] in ("on", "off"):
        return parts[1]
    raise ValueError("usage: long on | long off | long (shows the current setting)")


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


@app.route("/api/report")
def api_report():
    """Live catrank / h2hstand / playoffbracket for the Standings tab. Doesn't
    touch the shared HISTORY log -- this is per-viewer, polled independently
    of the draft pick log. ?kind=catrank|h2hstand|playoffbracket
    &source=(blank for blended)|espn|roto|rank|table|yahoo|fantrax"""
    kind = request.args.get("kind", "catrank").strip().lower()
    source = request.args.get("source", "").strip().lower()
    if kind not in REPORT_KINDS or source not in REPORT_SOURCES:
        return jsonify({"error": "bad kind/source"}), 400
    output, data, state = _get_report(kind, source)
    return jsonify({"output": output, "data": data, "state": state})


@app.route("/api/players")
def api_players():
    """Undrafted players, best-ADP first, for the click-to-draft picker.
    Excludes anyone already on a roster."""
    with _lock:
        taken = set(TRACKER._drafted_to_team.keys())
        pool = TRACKER.pool
        cols = ["Player", "Position", "ADP"]
        if "Consensus_Rank" in pool.columns:
            cols.append("Consensus_Rank")
        records = pool[cols].to_dict("records")
        state = _state_snapshot()

    def _num(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return float(v)

    rows = []
    for rec in records:
        name = rec["Player"]
        if name in taken:
            continue
        rows.append({
            "player": name,
            "position": rec.get("Position") or "",
            "adp": _num(rec.get("ADP")),
            "consensus": _num(rec.get("Consensus_Rank")),
        })
    rows.sort(key=lambda r: r["adp"] if r["adp"] is not None else 1e9)
    return jsonify({"players": rows, "state": state})


@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    user = (body.get("user") or "").strip() or "anon"
    if not text:
        return jsonify({"error": "empty command"}), 400
    with _lock:
        try:
            pc = _parse_pause_command(text)
        except ValueError as e:
            pc, err = None, f"[!] {e}"
            _append_history(user, text, err)
            return jsonify({"output": err, "data": None, "state": _state_snapshot()})
        if pc is not None:
            msg, err = _set_auto_paused(user, pc[1], pc[0])
            if err:
                err = f"[!] {err}"
                _append_history(user, text, err)
                return jsonify({"output": err, "data": None, "state": _state_snapshot()})
            return jsonify({"output": msg, "data": None, "state": _state_snapshot()})
        try:
            lc = _parse_long_command(text)
        except ValueError as e:
            err = f"[!] {e}"
            _append_history(user, text, err)
            return jsonify({"output": err, "data": None, "state": _state_snapshot()})
        if lc is not None:
            if lc == "status":
                msg = (f"Long mode is {'ON' if LONG_MODE else 'OFF'} "
                       f"({LONG_SECONDS:.0f}s when on, {engine.TOP3_TIME_BUDGET_SEC:.0f}s when off).")
                _append_history(user, text, msg)
            else:
                msg = _set_long_mode(user, lc == "on")
            return jsonify({"output": msg, "data": None, "state": _state_snapshot()})
        before_key = _clock_key()
        TRACKER.last_report = None  # so a stale table never rides along with a different command
        try:
            output = engine.dispatch_command(TRACKER, text, WEEKLY, time_budget=_top3_budget())
        except Exception as e:
            output = f"[!] Error: {e}"
        data = getattr(TRACKER, "last_report", None)
        TRACKER.last_report = None
        _append_history(user, text, output, data)
        _maybe_auto_top3(before_key)
        state = _state_snapshot()
    return jsonify({"output": output, "data": data, "state": state})


@app.route("/api/auto_top3", methods=["POST"])
def api_auto_top3():
    """Pause or resume the automatic top-3 for one team (or all alliance teams).
    Body: {"team": 6 | "all", "paused": true|false, "user": "..."}"""
    body = request.get_json(force=True, silent=True) or {}
    user = (body.get("user") or "").strip() or "anon"
    team = body.get("team")
    paused = body.get("paused")
    if not isinstance(paused, bool):
        return jsonify({"error": "paused must be true or false"}), 400
    with _lock:
        if team == "all":
            teams = _alliance_teams()
        elif isinstance(team, int) and not isinstance(team, bool):
            teams = {team}
        else:
            return jsonify({"error": "team must be a team number or \"all\""}), 400
        msg, err = _set_auto_paused(user, teams, paused)
        state = _state_snapshot()
    if err:
        return jsonify({"error": err, "state": state}), 400
    return jsonify({"ok": True, "message": msg, "state": state})


@app.route("/api/long_mode", methods=["POST"])
def api_long_mode():
    """Turn long mode on or off. Body: {"enabled": true|false, "user": "..."}"""
    body = request.get_json(force=True, silent=True) or {}
    user = (body.get("user") or "").strip() or "anon"
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be true or false"}), 400
    with _lock:
        msg = _set_long_mode(user, enabled)
        state = _state_snapshot()
    return jsonify({"ok": True, "message": msg, "state": state})


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
            REPORT_CACHE.clear()
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
