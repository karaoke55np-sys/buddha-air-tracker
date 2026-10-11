"""
Buddha Air Fleet Tracker -- JSON API server
============================================
Runs FlightRadar24 polling at TWO speeds and keeps the latest result in
memory, served as JSON over HTTP -- so your web app's JS can just
fetch() it.

WHY TWO SPEEDS:
FlightRadar24's free/unofficial feed isn't built for sub-5-second
polling the way the paid FlightRadar24 website itself uses -- hitting it
that aggressively risks the connection getting rate-limited or blocked
entirely, breaking the whole tracker. Instead:
  - Live POSITION data (altitude, speed, heading, air/ground state) is
    refreshed FAST (every FAST_POLL_SECONDS), since that's what visibly
    needs to feel close to real-time on the map and in the cards.
  - Airport schedule-board data (sector/ETA/delay lookups) is refreshed
    SLOW (every SLOW_POLL_SECONDS), since that genuinely doesn't change
    second-to-second the way position does -- a status board update
    every 60-90s is plenty.
This keeps the visible "live" experience fast without multiplying the
total request rate against FlightRadar24 by the same factor.

SETUP (one time):
    pip install FlightRadarAPI flask

RUN:
    python api_server.py

Then, from your web app's JS (same machine, default port 5051):
    fetch("http://localhost:5051/api/fleet-status")
      .then(r => r.json())
      .then(data => { ...render data.air and data.ground... });

Endpoints:
    GET /api/fleet-status
        {
          "updated": "2026-09-03 12:12:19",
          "counts": {"air": 5, "ground": 10},
          "records": [ {...one per aircraft, see fleet_core.build_fleet_records...} ],
          "air": [ ...records with status == "air"... ],
          "ground": [ ...records with status == "ground"... ]
        }

CORS is wide open (Access-Control-Allow-Origin: *) by default so this
also works if your web app runs from a different port/domain during
development. Tighten CORS_ALLOW_ORIGIN below before exposing this
publicly -- this is meant to run on your own PC / local network.
"""

import threading
import time
import datetime
import json
import os

from flask import Flask, jsonify
from FlightRadarAPI import FlightRadar24API

import fleet_core as core

FAST_POLL_SECONDS = 10   # live position: altitude, speed, heading, air/ground
SLOW_POLL_SECONDS = 90   # airport schedule boards: sector/ETA/delay data
PORT = 5051
CORS_ALLOW_ORIGIN = "*"  # e.g. "http://localhost:3000" to restrict it

# Where the last-known fleet status gets saved, so a crash or restart of
# this process (whether from an unhandled error, a redeploy, or anything
# else) doesn't wipe the data the app is showing -- it reloads the last
# good snapshot immediately on startup instead of going blank, while the
# background loop works on getting fresh data again.
STATE_CACHE_FILE = os.path.join(core.DATA_DIR, "buddha_air_live_state.json")

app = Flask(__name__)

_state_lock = threading.Lock()
_state = {
    "updated": None,
    "counts": {"air": 0, "ground": 0},
    "records": [],
    "air": [],
    "ground": [],
    "error": None,
}


def load_last_known_state():
    """Reloads the last successfully-saved fleet snapshot at startup, so
    the API never has to answer with a totally blank payload after a
    restart. The returned state's own "updated" timestamp stays honest
    about when that data was actually fetched -- it's not pretended to
    be fresh."""
    if os.path.exists(STATE_CACHE_FILE):
        try:
            with open(STATE_CACHE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            saved["error"] = "Restored from last known data after a restart -- waiting for a fresh update."
            return saved
        except Exception as e:
            print(f"[state cache load failed] {e}")
    return None


def save_state_snapshot(state):
    try:
        with open(STATE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[state cache save failed] {e}")

_hub_schedule_lock = threading.Lock()
_hub_schedule = {}  # shared between the two loops; slow loop writes, fast loop reads

# ----------------------------------------------------------------------
# Rate-limit backoff
# ----------------------------------------------------------------------
# FlightRadar24 answers "HTTP 429" once too many requests arrive from one
# address, and it stays unhappy for a while. Retrying at the normal
# cadence during that window is both pointless and actively harmful: it
# keeps the block alive, and the fleet list then updates with nothing at
# all (every aircraft classified "unknown") instead of continuing to show
# the last data that did arrive. So on a rate-limit answer both loops
# pause for a cooldown and keep serving the previous snapshot.
#
# The state files the app reads are untouched during a cooldown, so the
# board-derived sectors/locations already found stay on screen.
RATE_LIMIT_COOLDOWN_SEC = 120
_ratelimit_lock = threading.Lock()
_ratelimit_until = 0.0


def _is_rate_limited(exc):
    text = str(exc).lower()
    return "429" in text or "too many requests" in text


def _start_ratelimit_cooldown(where, exc):
    """Pauses both loops after a 429, returning how long we waited."""
    global _ratelimit_until
    with _ratelimit_lock:
        _ratelimit_until = time.time() + RATE_LIMIT_COOLDOWN_SEC
    print(f"[rate limited] {where}: {exc} -- pausing polls for "
          f"{RATE_LIMIT_COOLDOWN_SEC}s and continuing to serve the last good data")
    return RATE_LIMIT_COOLDOWN_SEC


def _cooldown_remaining():
    with _ratelimit_lock:
        return max(0.0, _ratelimit_until - time.time())


def _slow_loop(fr):
    """Refreshes airport schedule-board data (sector/ETA/delay) at the
    slower cadence -- this is the heavier, multi-airport lookup that
    doesn't need to be fresh every few seconds."""
    global _hub_schedule
    while True:
        waited = 0
        if _cooldown_remaining() > 0:
            time.sleep(min(_cooldown_remaining(), SLOW_POLL_SECONDS))
            continue
        try:
            new_schedule = core.fetch_hub_schedule(fr)
            with _hub_schedule_lock:
                _hub_schedule = new_schedule
            print(f"[schedule board] refreshed -- {len(new_schedule)} aircraft matched")
        except Exception as e:
            if _is_rate_limited(e):
                waited = _start_ratelimit_cooldown("schedule board", e)
            else:
                print(f"[schedule board fetch failed] {e}")
        time.sleep(max(SLOW_POLL_SECONDS, waited))


def _fast_loop():
    """Refreshes live positions (the part that needs to feel real-time)
    at the fast cadence, using whatever airport-schedule snapshot the
    slow loop most recently produced.

    Position data is the part FlightRadar24 throttles hardest, and on a
    datacentre IP (Render) it can answer HTTP 429 to every query for long
    stretches. This loop therefore NEVER gives up: a failure leaves the
    last good fleet list in place, reports why, and retries with a
    growing gap instead of hammering a block that is already refusing us.
    """
    fr = FlightRadar24API()
    sector_cache = core.load_sector_cache()
    ground_since = core.load_ground_since()
    prev_air = set()  # aircraft that were airborne on the previous poll
    failures_in_a_row = 0

    threading.Thread(target=_slow_loop, args=(fr,), daemon=True).start()

    while True:
        waited = 0
        if _cooldown_remaining() > 0:
            time.sleep(min(_cooldown_remaining(), FAST_POLL_SECONDS))
            continue
        try:
            by_reg = core.fetch_positions(fr)
            timestamp = core.nepal_now_str()

            with _hub_schedule_lock:
                hub_schedule = _hub_schedule

            # Only the handful of airborne aircraft still missing a
            # sector after the board lookup need this per-flight call,
            # so it stays cheap enough for the fast loop.
            core.resolve_missing_sectors_via_details(fr, by_reg, hub_schedule)

            sector_cache = core.update_sector_cache(sector_cache, by_reg, timestamp)
            core.save_sector_cache(sector_cache)

            core.update_ground_since(ground_since, prev_air, by_reg, hub_schedule, time.time())
            core.save_ground_since(ground_since)

            records = core.build_fleet_records(by_reg, sector_cache, hub_schedule, timestamp, ground_since)
            air = [r for r in records if r["status"] == "air"]
            ground = [r for r in records if r["status"] == "ground"]
            # Most recently arrived aircraft first (by real arrival time).
            # Aircraft whose arrival time isn't known sort to the bottom,
            # and keep a stable order among themselves.
            ground.sort(key=lambda r: r["sort_ts"] if r.get("sort_ts") is not None else -1, reverse=True)

            with _state_lock:
                _state["updated"] = timestamp
                _state["counts"] = {"air": len(air), "ground": len(ground)}
                _state["records"] = records
                _state["air"] = air
                _state["ground"] = ground
                _state["error"] = None
                snapshot = dict(_state)

            save_state_snapshot(snapshot)

            failures_in_a_row = 0
            print(f"[{timestamp}] Updated. {len(air)} airborne, {len(ground)} on ground.")
        except Exception as e:
            failures_in_a_row += 1
            print(f"[poll error] {failures_in_a_row} in a row: {e}")
            if _is_rate_limited(e):
                waited = _start_ratelimit_cooldown("live positions", e)
            # Keep whatever fleet list is already being served (or was
            # restored from disk at startup) and say why it is not fresh.
            # Blanking the list here is what turns "FlightRadar24 is
            # throttling us" into "the whole fleet vanished".
            with _state_lock:
                _state["error"] = (
                    f"Live position feed unavailable ({e}) -- showing the last known "
                    f"fleet data, {failures_in_a_row} attempt(s) since it last updated."
                )
            # Extra spacing of our own on top of the 429 cooldown, so a
            # long block is not retried every 10s for hours on end.
            if failures_in_a_row >= 5:
                waited = max(waited, min(300, FAST_POLL_SECONDS * failures_in_a_row))

        time.sleep(max(FAST_POLL_SECONDS, waited))


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = CORS_ALLOW_ORIGIN
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


@app.route("/api/fleet-status")
def fleet_status():
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "fleet_size": len(core.FLEET)})


if __name__ == "__main__":
    restored = load_last_known_state()
    if restored:
        with _state_lock:
            _state.update(restored)
        print(f"[state cache] Restored last known snapshot from {restored.get('updated')} -- refreshing now.")

    t = threading.Thread(target=_fast_loop, daemon=True)
    t.start()
    print(f"Buddha Air fleet API running on http://localhost:{PORT}/api/fleet-status")
    print(f"Fast (position) poll: every {FAST_POLL_SECONDS}s -- Slow (schedule board) poll: every {SLOW_POLL_SECONDS}s")
    app.run(host="0.0.0.0", port=PORT)
