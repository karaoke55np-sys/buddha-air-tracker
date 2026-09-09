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

from flask import Flask, jsonify
from FlightRadarAPI import FlightRadar24API

import fleet_core as core

FAST_POLL_SECONDS = 10   # live position: altitude, speed, heading, air/ground
SLOW_POLL_SECONDS = 90   # airport schedule boards: sector/ETA/delay data
PORT = 5051
CORS_ALLOW_ORIGIN = "*"  # e.g. "http://localhost:3000" to restrict it

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

_hub_schedule_lock = threading.Lock()
_hub_schedule = {}  # shared between the two loops; slow loop writes, fast loop reads


def _slow_loop(fr):
    """Refreshes airport schedule-board data (sector/ETA/delay) at the
    slower cadence -- this is the heavier, multi-airport lookup that
    doesn't need to be fresh every few seconds."""
    global _hub_schedule
    while True:
        try:
            new_schedule = core.fetch_hub_schedule(fr)
            with _hub_schedule_lock:
                _hub_schedule = new_schedule
            print(f"[schedule board] refreshed -- {len(new_schedule)} aircraft matched")
        except Exception as e:
            print(f"[schedule board fetch failed] {e}")
        time.sleep(SLOW_POLL_SECONDS)


def _fast_loop():
    """Refreshes live positions (the part that needs to feel real-time)
    at the fast cadence, using whatever airport-schedule snapshot the
    slow loop most recently produced."""
    fr = FlightRadar24API()
    sector_cache = core.load_sector_cache()

    threading.Thread(target=_slow_loop, args=(fr,), daemon=True).start()

    while True:
        try:
            by_reg = core.fetch_positions(fr)
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with _hub_schedule_lock:
                hub_schedule = _hub_schedule

            # Only the handful of airborne aircraft still missing a
            # sector after the board lookup need this per-flight call,
            # so it stays cheap enough for the fast loop.
            core.resolve_missing_sectors_via_details(fr, by_reg, hub_schedule)

            sector_cache = core.update_sector_cache(sector_cache, by_reg, timestamp)
            core.save_sector_cache(sector_cache)

            records = core.build_fleet_records(by_reg, sector_cache, hub_schedule, timestamp)
            air = [r for r in records if r["status"] == "air"]
            ground = [r for r in records if r["status"] == "ground"]

            with _state_lock:
                _state["updated"] = timestamp
                _state["counts"] = {"air": len(air), "ground": len(ground)}
                _state["records"] = records
                _state["air"] = air
                _state["ground"] = ground
                _state["error"] = None

            print(f"[{timestamp}] Updated. {len(air)} airborne, {len(ground)} on ground.")
        except Exception as e:
            print(f"[poll error] {e}")
            with _state_lock:
                _state["error"] = str(e)

        time.sleep(FAST_POLL_SECONDS)


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
    t = threading.Thread(target=_fast_loop, daemon=True)
    t.start()
    print(f"Buddha Air fleet API running on http://localhost:{PORT}/api/fleet-status")
    print(f"Fast (position) poll: every {FAST_POLL_SECONDS}s -- Slow (schedule board) poll: every {SLOW_POLL_SECONDS}s")
    app.run(host="0.0.0.0", port=PORT)
