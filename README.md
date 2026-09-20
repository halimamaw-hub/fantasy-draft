# Draft Day web app

A basic shared web front-end for the draft engine (`engine.py`, unchanged
logic from the original script -- just refactored so it can be called from
a server instead of only an interactive terminal).

## What you get
- `engine.py` -- the original draft engine. All logic (projection blending,
  z-scores, roster solving, the Monte-Carlo top-3 recommender, category
  rankings, H2H standings, playoff bracket sim) is untouched. Only change:
  the old `input()`-based command loop was split into `dispatch_command()`,
  which returns text instead of printing straight to a terminal, so both
  the original CLI (`python engine.py`) and the web app can share it.
- `app.py` -- a small Flask server: one shared `DraftTracker` in memory,
  one lock so simultaneous commands from different people don't collide,
  and two endpoints (`POST /api/command`, `GET /api/history`).
- `static/index.html` -- one plain HTML/JS page. No build step, no
  framework. It polls `/api/history` every 2 seconds so everyone's screen
  shows the same running log of picks/commands and the same "on the clock"
  banner. Good enough for 3 people glancing at their phones during a draft;
  not meant to scale past that.
- `data/` -- the 6 CSVs you uploaded (Fantrax player pool, the 4 projection
  sources, and the NBA schedule).

## Run it locally
```bash
pip install -r requirements.txt
python app.py
```
It starts on port 5000 and prints the URL to use. On the same machine:
`http://127.0.0.1:5000`. For others on the same Wi-Fi, use your machine's
LAN IP (the terminal prints its best guess), e.g. `http://192.168.1.23:5000`.

## Deploy on Render (for people who aren't on your network)
1. Push this whole folder to a new GitHub repo.
2. In Render: **New > Web Service**, connect that repo.
3. Environment: Python 3. Build command: `pip install -r requirements.txt`.
   Start command: Render will pick up the included `Procfile`
   (`gunicorn app:app --workers 1 --threads 4 --timeout 120`) automatically
   -- leave the start command field blank, or paste that in if it asks.
4. Deploy. Render gives you a permanent URL like
   `https://your-app.onrender.com` -- share that with your 2 friends.

**Keep `--workers 1`.** The draft state lives in one Python process's
memory; extra gunicorn workers would each get their own separate draft
and nobody would see the same picks. `--threads 4` is what lets 3 people
hit it at once safely within that one worker (the lock in `app.py`
serializes the actual writes).

**Free-tier instances spin down after ~15 minutes with no traffic**, and
waking back up resets the in-memory draft. In practice the page's own
2-second polling from any open browser tab counts as traffic and keeps it
awake, so this mostly only bites if everyone closes their tab/laptop for
a while mid-draft. If that's a real risk for your group, Render's cheapest
paid instance type removes the spin-down entirely.

## Using it
Type a player's name and hit Send (or Enter) to log the pick for whoever's
on the clock. Everything else from the original CLI still works as a typed
command: `top3`, `status`, `catrank`, `h2hstand`, `playoffbracket`,
`endofseason`, `undo`, `snake`, `3rr`, `help`, plus the per-source variants
(`espncatrank`, `rotoh2hstand`, `fantraxcatrank`, etc.). The quick-action
buttons cover the common ones. Everyone typing into the page shares the
exact same draft state -- there's no per-user draft.

## The Top 3 recommendation view
`top3` (and auto-recommend) now renders as a real table on the web page: one
row per pick with draft-market numbers, the chance the player is still there at
your next pick, the simulated podium/sweep/title odds, and a Met / Not met goal
pill. Click a row for the full breakdown (alliance seeds, simulation CI and
sample size, roster-health terms). A low-simulation-count warning shows when
the live time budget cut the Monte Carlo short. The CLI prints the same
information as a readable text report. This is display-only -- scoring and
ranking in `recommend_top3()` are unchanged. `print_top3_table()` also stores a
structured copy on `tracker.last_report`, which `app.py` sends to the page as
`data` alongside the plain-text `output`.

## Auto Top 3 on alliance turns
Whenever the clock lands on an alliance team (your slot + allies, i.e. teams 9, 6, 7), the server
automatically runs `top3` for that team and posts the table to the shared log as a
`top3 (auto, pick #N, Team X)` entry from user `auto`. It runs in the background so the pick that
triggered it returns instantly; the table appears on everyone's screen within a few seconds via the
normal polling. It also fires after `undo` or `snake`/`3rr` if that moves an alliance team onto the
clock, and drops itself if the draft moves on before it finishes. Turn it off by setting the
`AUTO_TOP3=0` environment variable on Render.

## Cat rank, H2H standings and playoff bracket views
`catrank`, `h2hstand` and `playoffbracket` get the same treatment: an aligned
text report on the CLI, and real HTML on the web page (a color-scaled category
rank grid, a standings table with the bye line and the alliance's projected
losses, and a round-by-round bracket with a podium strip). Scoring is unchanged.

Each one can also be run against a single projection file. Prefix the command
with the source: `espn`, `roto`, `rank`, `table`, or `fantrax`.

| Command | Example (ESPN) | File used |
|---|---|---|
| category rankings | `espncatrank` | ESPN_Fantasy_Basketball_Projections_Complete.csv |
| H2H standings | `espnh2hstand` (or `espnh2h`) | same |
| playoff bracket | `espnbracket` (or `espnplayoffbracket`) | same |

`roto` = rotoballerfantasyranking.csv, `rank` = fantasy_basketball_rankings.csv,
`table` = table.csv, `fantrax` = the base Fantrax export. The page has a
"Reports by projection source" panel with a button for every command/source
combination (the *Blended* button is the original un-prefixed command).

## Resetting the draft
The **Reset draft** button (top right of the page, or typing `reset` in the
command box) opens an "Are you sure you want to reset?" dialog that shows how
many picks will be lost. Cancel, Esc, or clicking outside closes it without
doing anything. Confirming rebuilds a fresh draft on the server -- every pick,
the shared log, and any `snake`/`3rr` switch are wiped and it goes back to pick
#1 -- and every connected browser clears its screen and shows a "Draft reset by
<name>" line. Behind it is `POST /api/reset`, which refuses to run unless the
request body contains `{"confirm": true}`.

## Player pool size
The draftable pool is now the top **400** players (was 350), which brings in
names like Cam Thomas, Mike Conley, Buddy Hield and Kevin Love. Change it
without editing code by setting a `POOL_SIZE` environment variable (on Render:
Environment tab), or edit `DEFAULT_POOL_SIZE` at the bottom of `engine.py`.
A bigger pool means slightly more work per recommendation; the top-3 time
budget still applies.

## Notes / things you may want to change
- `app.py` builds the tracker with `my_slot=9, teams=10, rounds=14,
  reversal_round=3, alliance_allies=(6, 7)` (plus the pool size above) and `auto_recommend_teams=()`
  (auto-recommend is off by default on the web version so the shared log
  doesn't get a recommendation block after every single pick -- turn it
  back on by editing that line if you want it).
- The log (`HISTORY` in `app.py`) is in memory only -- restarting the
  server clears it, though the draft state itself (picks logged) also
  lives only in memory, so a restart resets the whole draft. If you need
  the draft to survive a restart, the easiest addition is dumping
  `TRACKER._log` to a file after each pick and replaying it on startup.
