"""
Buddha Air Fleet Tracker -- JSON API server
============================================
Runs the same FlightRadar24 polling as tracker.py, but instead of writing
a text file, it keeps the latest result in memory and serves it as JSON
over HTTP -- so your existing web app's JS can just fetch() it.

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

import os
import threading
import time

from flask import Flask, jsonify
from FlightRadarAPI import FlightRadar24API

import fleet_core as core

# ==================== RENDER CONFIGURATION ====================
POLL_SECONDS = int(os.environ.get('POLL_SECONDS', 120))
PORT = int(os.environ.get('PYTHON_PORT', 5051))
CORS_ALLOW_ORIGIN = os.environ.get('CORS_ALLOW_ORIGIN', '*')

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


def _poll_loop():
    fr = FlightRadar24API()
    sector_cache = core.load_sector_cache()

    while True:
        try:
            records, sector_cache, timestamp = core.poll_once(fr, sector_cache)
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

        time.sleep(POLL_SECONDS)


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
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    print(f"Buddha Air fleet API running on port {PORT}")
    print(f"Endpoint: http://localhost:{PORT}/api/fleet-status")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)