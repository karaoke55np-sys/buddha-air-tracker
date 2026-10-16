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

FAST_POLL_SECONDS = 15   # live position: altitude, speed, heading, air/ground
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
    # Softer than "error": the data IS fresh, but part of the feed was
    # missing this cycle, so some airborne aircraft are shown from their
    # last sighting (flagged per-aircraft with "signal_lost").
    "notice": None,
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
            saved.setdefault("notice", None)
            return saved
        except Exception as e:
            print(f"[state cache load failed] {e}")
    return None


def save_state_snapshot(state):
    try:
        core._atomic_write_json(STATE_CACHE_FILE, state)
    except Exception as e:
        print(f"[state cache save failed] {e}")

_hub_schedule_lock = threading.Lock()
# Shared between the two loops; slow loop writes, fast loop reads.
# Starts from the last snapshot saved to disk (if recent), so a restart
# doesn't drop every board-derived sector to "Unknown" while the first
# slow poll is still running.
_hub_schedule = core.load_hub_schedule()

# Per-aircraft flight history (see fleet_core "Per-aircraft flight
# history"). Filled slowly in the background, saved to disk, and used for
# parked aircraft whose sector the airport boards can't explain.
_history_lock = threading.Lock()
_history = core.load_history()
HISTORY_STARTUP_GAP_SEC = 6       # between lookups while catching up
HISTORY_STEADY_GAP_SEC = 30       # between lookups once caught up
HISTORY_REFRESH_UNKNOWN_SEC = 10 * 60   # re-check aircraft we still can't place
HISTORY_REFRESH_KNOWN_SEC = 30 * 60     # re-check aircraft we can place
HISTORY_AFTER_LANDING_SEC = 3 * 60      # re-check this long after a landing

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


def _start_ratelimit_cooldown(where, exc, seconds=None):
    """Pauses every loop after a 429, returning how long we wait."""
    global _ratelimit_until
    seconds = seconds or RATE_LIMIT_COOLDOWN_SEC
    with _ratelimit_lock:
        _ratelimit_until = max(_ratelimit_until, time.time() + seconds)
    print(f"[rate limited] {where}: {exc} -- pausing all FlightRadar24 requests for "
          f"{seconds}s and continuing to serve the last good data")
    return seconds


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
            if new_schedule or not _hub_schedule:
                with _hub_schedule_lock:
                    _hub_schedule = new_schedule
                core.save_hub_schedule(new_schedule)
                print(f"[schedule board] refreshed -- {len(new_schedule)} aircraft matched")
            else:
                # An empty answer while we already hold data is almost
                # always a failed/throttled fetch, not "no flights today".
                print("[schedule board] came back empty -- keeping the previous board data")
        except Exception as e:
            if _is_rate_limited(e):
                waited = _start_ratelimit_cooldown("schedule board", e)
            else:
                print(f"[schedule board fetch failed] {e}")
        time.sleep(max(SLOW_POLL_SECONDS, waited))


def _history_candidates():
    """Parked/unseen aircraft whose history is missing or due a refresh,
    most-needed first. Airborne aircraft don't need it (live data has
    their sector)."""
    now = time.time()
    with _state_lock:
        airborne = {r["registration"] for r in _state.get("air", [])}
        unknown = {r["registration"] for r in _state.get("ground", [])
                   if not r.get("sector") or not r.get("ground_since_ts")}
        # When each parked aircraft landed, as far as we know.
        landed_at = {r["registration"]: r.get("ground_since_ts")
                     for r in _state.get("ground", []) if r.get("ground_since_ts")}
    with _history_lock:
        hist = dict(_history)
    due = []
    for reg in core.FLEET:
        if reg in airborne:
            continue
        h = hist.get(reg)
        if h is None:
            due.append((0, reg))
            continue
        fetched = h.get("fetched_at") or 0
        # It landed after we last looked at its history: the history is
        # out of date (it shows the flight BEFORE this one). Look again
        # once FlightRadar24 has had ~3 minutes to record the landing.
        landed = landed_at.get(reg)
        if landed and fetched < landed + HISTORY_AFTER_LANDING_SEC <= now:
            due.append((0, reg))
            continue
        age = now - fetched
        limit = HISTORY_REFRESH_UNKNOWN_SEC if reg in unknown else HISTORY_REFRESH_KNOWN_SEC
        if age >= limit:
            due.append((1 if reg in unknown else 2, reg))
    due.sort()
    return [reg for _, reg in due]


def _history_loop():
    """Looks up one aircraft's flight history at a time -- quickly at
    startup until every parked aircraft has been checked, then gently."""
    while True:
        if _cooldown_remaining() > 0:
            time.sleep(min(_cooldown_remaining(), 30))
            continue
        todo = _history_candidates()
        if not todo:
            time.sleep(HISTORY_STEADY_GAP_SEC)
            continue
        reg = todo[0]
        catching_up = any(r not in _history for r in todo)
        try:
            h = core.fetch_aircraft_history(reg)
            with _history_lock:
                _history[reg] = h
                snapshot = dict(_history)
            core.save_history(snapshot)
            ld = h.get("last_done")
            print(f"[history] {reg}: last flight "
                  f"{ld['sector'] + ' landed ' + core._fmt_nepal_dt(ld['ts']) if ld else 'none found'}")
        except Exception as e:
            if _is_rate_limited(e):
                _start_ratelimit_cooldown(f"history {reg}", e)
                continue
            print(f"[history fetch failed for {reg}] {e}")
            # Don't retry the same aircraft immediately.
            with _history_lock:
                old = dict(_history.get(reg) or {})
                old["fetched_at"] = time.time() - HISTORY_REFRESH_UNKNOWN_SEC + 120
                _history[reg] = old
        time.sleep(HISTORY_STARTUP_GAP_SEC if catching_up else HISTORY_STEADY_GAP_SEC)


def _fast_loop():
    """Refreshes live positions (the part that needs to feel real-time)
    at the fast cadence, using whatever airport-schedule snapshot the
    slow loop most recently produced.

    Position data is the part FlightRadar24 throttles hardest, and on a
    datacentre IP (Render) it can answer HTTP 429 to every query for long
    stretches. This loop therefore NEVER gives up: a failure leaves the
    last good fleet list in place, reports why, and retries with a
    growing gap instead of hammering a block that is already refusing us.

    An airborne aircraft that is merely MISSING from one poll is held in
    the air (core.AirHold) instead of being moved to the ground list --
    that was what emptied "In the Air" / "Taxiing" into "On Ground".
    """
    fr = FlightRadar24API()
    sector_cache = core.load_sector_cache()
    ground_since = core.load_ground_since()
    air_hold = core.AirHold.load()
    # Aircraft that were airborne on the previous poll -- restored from
    # the air-hold file, so a restart doesn't forget who was flying.
    prev_air = air_hold.airborne_regs()
    failures_in_a_row = 0
    limited_in_a_row = 0
    # Last on-ground sighting of each aircraft. PHH and BIR are only
    # queried every other cycle now, so an aircraft parked there is kept
    # from its sighting one cycle ago instead of blinking out.
    last_ground = {}  # reg -> (Flight, seen_ts)
    GROUND_MEMO_SEC = FAST_POLL_SECONDS * 3

    threading.Thread(target=_slow_loop, args=(fr,), daemon=True).start()
    threading.Thread(target=_history_loop, daemon=True).start()

    while True:
        waited = 0
        if _cooldown_remaining() > 0:
            time.sleep(min(_cooldown_remaining(), FAST_POLL_SECONDS))
            continue
        try:
            by_reg, sweep_ok, rate_limited = core.fetch_positions_ex(fr)
            now_ts = time.time()

            for reg, f in by_reg.items():
                if f.on_ground:
                    last_ground[reg] = (f, now_ts)
                else:
                    last_ground.pop(reg, None)
            for reg, (f, seen) in list(last_ground.items()):
                if reg not in by_reg and now_ts - seen <= GROUND_MEMO_SEC:
                    by_reg[reg] = f
                elif now_ts - seen > GROUND_MEMO_SEC:
                    last_ground.pop(reg, None)

            if rate_limited:
                # Even one refused query means we're going too fast: back
                # off (20s, 40s, 80s, then 120s) instead of carrying on at
                # full speed and keeping the block alive.
                limited_in_a_row += 1
                waited = _start_ratelimit_cooldown(
                    "live positions (some queries refused)", "HTTP 429",
                    seconds=min(RATE_LIMIT_COOLDOWN_SEC, 20 * 2 ** (limited_in_a_row - 1)))
            else:
                limited_in_a_row = 0
            timestamp = core.nepal_now_str()

            # Keep missing-but-airborne aircraft in the air; decide which
            # of them really landed out of sight.
            inferred_landings = air_hold.apply(by_reg, sweep_ok, now_ts)
            air_hold.save()

            with _hub_schedule_lock:
                hub_schedule = _hub_schedule
            with _history_lock:
                history = dict(_history)

            # Only the handful of airborne aircraft still missing a
            # sector after the board lookup need this per-flight call,
            # so it stays cheap enough for the fast loop.
            core.resolve_missing_sectors_via_details(fr, by_reg, hub_schedule)

            sector_cache = core.update_sector_cache(sector_cache, by_reg, timestamp)
            core.save_sector_cache(sector_cache)

            core.update_ground_since(ground_since, prev_air, by_reg, hub_schedule, now_ts,
                                     inferred_landings, history)
            core.save_ground_since(ground_since)

            records = core.build_fleet_records(by_reg, sector_cache, hub_schedule, timestamp,
                                               ground_since, history)
            air = [r for r in records if r["status"] == "air"]
            ground = [r for r in records if r["status"] == "ground"]
            # Most recently arrived aircraft first (by real arrival time).
            # Aircraft whose arrival time isn't known sort to the bottom,
            # and keep a stable order among themselves.
            ground.sort(key=lambda r: r["sort_ts"] if r.get("sort_ts") is not None else -1, reverse=True)

            held = [r["registration"] for r in air if r.get("signal_lost")]
            notice = None
            if not sweep_ok:
                notice = "En-route feed did not answer this cycle -- airborne aircraft shown from their last sighting."
            elif held:
                notice = f"Signal briefly lost for {', '.join(held)} -- shown from last sighting."

            with _state_lock:
                _state["updated"] = timestamp
                _state["counts"] = {"air": len(air), "ground": len(ground)}
                _state["records"] = records
                _state["air"] = air
                _state["ground"] = ground
                _state["error"] = None
                _state["notice"] = notice
                snapshot = dict(_state)

            save_state_snapshot(snapshot)

            failures_in_a_row = 0
            print(f"[{timestamp}] Updated. {len(air)} airborne ({len(held)} held), "
                  f"{len(ground)} on ground. sweep_ok={sweep_ok}")
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


def _watchdog(thread):
    """If the polling thread ever dies, the API would keep answering with
    frozen data forever. Exit instead, so server.js restarts us cleanly."""
    while True:
        time.sleep(15)
        if not thread.is_alive():
            print("[watchdog] polling thread stopped -- exiting so the app restarts it")
            os._exit(1)


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
    threading.Thread(target=_watchdog, args=(t,), daemon=True).start()
    print(f"Buddha Air fleet API running on http://localhost:{PORT}/api/fleet-status")
    print(f"Fast (position) poll: every {FAST_POLL_SECONDS}s -- Slow (schedule board) poll: every {SLOW_POLL_SECONDS}s")
    app.run(host="0.0.0.0", port=PORT)
