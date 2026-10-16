"""
fleet_core.py
=============
All FlightRadar24 fetching + classification logic for the Buddha Air
fleet tracker lives here, so tracker.py (desktop text-file + desktop
notifications) and api_server.py (JSON API for your web app) share one
implementation instead of drifting apart.
"""

import os
import json
import time
import datetime
import threading

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

# Your fleet list (from A/C REG sheet)
FLEET = [
    "9N-AIT", "9N-AJS", "9N-AJX", "9N-AMD", "9N-AMU", "9N-AMY",
    "9N-ANI", "9N-AJL", "9N-ANP", "9N-ANQ", "9N-ANW", "9N-AOC",
    "9N-ANZ", "9N-AOG", "9N-ANH", "9N-AOS",
]

# Hub + secondary airports whose schedule boards we check, to fill in
# sectors for grounded aircraft that aren't broadcasting a flight plan.
HUB_AIRPORT = "KTM"
SECONDARY_AIRPORTS = ["PHH", "BIR"]

# Approximate coordinates for the airports above, used to run a small,
# zoomed-in position query at each one specifically. This matters
# because FlightRadar24's live feed only includes grounded/taxiing
# aircraft when the queried area is small (airport-level zoom) -- a wide
# regional bounding box like CENTER_LAT/RADIUS_KM below silently drops
# ground traffic even though the API's own "gnd=1" default suggests it
# shouldn't. Without this, taxiing aircraft are invisible no matter how
# fast they're actually moving on the ground.
AIRPORT_COORDS = {
    "KTM": (27.6966, 85.3591),
    "PHH": (28.1962, 83.9856),
    "BIR": (26.4815, 87.2640),
}
TAXI_WATCH_RADIUS_KM = 20

# Full coordinate table for every airport in Buddha Air's network, used
# for drawing the route map (origin/destination markers + the aircraft's
# own position when we don't have a live one). Not the same list as
# AIRPORT_COORDS above -- that one drives extra API calls for ground
# detection, this one is just static reference data, safe to extend
# freely with no cost.
AIRPORT_ALL_COORDS = {
    "KTM": (27.6966, 85.3591),   # Kathmandu
    "PKR": (28.2000, 83.9821),   # Pokhara (old domestic strip)
    "PHH": (28.1962, 83.9856),   # Pokhara International
    "BWA": (27.5049, 83.4116),   # Bhairahawa (Gautam Buddha Intl)
    "BIR": (26.4815, 87.2640),   # Biratnagar
    "BHR": (27.6780, 84.4280),   # Bharatpur
    "SIF": (27.1594, 84.9805),   # Simara
    "BDP": (26.5717, 88.0795),   # Bhadrapur
    "KEP": (28.1000, 81.6667),   # Nepalgunj
    "JKR": (26.7147, 85.9227),   # Janakpur
    "DHI": (28.7433, 80.5793),   # Dhangadhi
    "SKH": (28.5833, 81.6333),   # Surkhet
    "RJB": (26.5333, 86.7500),   # Rajbiraj
    "TMI": (27.3167, 87.2000),   # Tumlingtar
    "LUA": (27.6869, 86.7297),   # Lukla
    "RUM": (27.2833, 86.5833),   # Rumjatar
    "VNS": (25.4524, 82.8593),   # Varanasi, India
    "CCU": (22.6547, 88.4467),   # Kolkata, India
}

# Box for the en-route sweep that catches aircraft flying between cities:
# all of Nepal plus the Varanasi / Kolkata corridors, and nothing more.
#
# This used to be a 900 km "radius" around Kathmandu, which
# get_bounds_by_point actually turns into an 1,800 km-wide SQUARE --
# Delhi, Lucknow, Kolkata, Dhaka and half of Tibet. FlightRadar24 only
# returns a limited number of aircraft per query, so in a box that busy
# Buddha Air flights were randomly cut from the answer, and every one cut
# was then shown "On Ground". A tight box keeps the answer small enough
# to come back complete.
#   format: north, south, west, east (degrees)
SWEEP_BOUNDS = (30.7, 22.3, 79.8, 88.9)

# Kept only for anything else that still imports them.
CENTER_LAT = 27.7
CENTER_LON = 85.3
RADIUS_KM = 900

# ----------------------------------------------------------------------
# Holding airborne aircraft through gaps in the feed
# ----------------------------------------------------------------------
# An airborne aircraft can be missing from one poll for reasons that have
# nothing to do with landing: a mountain blocks its ADS-B signal, a query
# was rate-limited, or the feed answered incompletely. "Missing" must not
# mean "landed". Instead the last airborne sighting is kept (clearly
# flagged as not live) until one of these is true:
#   - it is seen again (air or ground) -> live data takes over;
#   - it was last seen low and descending near an airport, and has now
#     been absent from AIR_LANDED_MISSING_POLLS complete sweeps -> it
#     landed there (time = when it was last seen, which is accurate to
#     a poll or two);
#   - AIR_HOLD_MAX_SEC passed without a sighting -> we honestly don't
#     know, it drops to the ground list with no invented landing time.
AIR_HOLD_MAX_SEC = 20 * 60
AIR_LANDED_MISSING_POLLS = 3
LANDING_GUESS_MAX_ALT_FT = 8000
LANDING_GUESS_RADIUS_KM = 25
AIR_HOLD_FILE_NAME = "buddha_air_air_hold.json"
HUB_SCHEDULE_FILE_NAME = "buddha_air_hub_schedule.json"
HUB_SCHEDULE_MAX_AGE_SEC = 45 * 60

# Where things get written on disk (shared between tracker.py and
# api_server.py so they see the same cache).
_env_data_dir = os.environ.get("FLEET_DATA_DIR")
if _env_data_dir:
    DATA_DIR = _env_data_dir
else:
    # Falls back to a local ./data folder when "~/Desktop" doesn't exist
    # (e.g. a Linux server/container has no Desktop, unlike a Windows PC)
    # so this still works out of the box on Render or any other host.
    _desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop")
    DATA_DIR = _desktop_dir if os.path.isdir(_desktop_dir) else \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SECTOR_CACHE_FILE = os.path.join(DATA_DIR, "buddha_air_sector_cache.json")
SCHEDULE_DEBUG_FILE = os.path.join(DATA_DIR, "buddha_air_schedule_debug.json")
AIR_HOLD_FILE = os.path.join(DATA_DIR, AIR_HOLD_FILE_NAME)
HUB_SCHEDULE_FILE = os.path.join(DATA_DIR, HUB_SCHEDULE_FILE_NAME)


def _atomic_write_json(path, data, **kw):
    """Write JSON via a temp file + rename, so a crash mid-write can never
    leave a half-written (unreadable) file behind -- an unreadable cache
    on restart is one way the whole picture used to start from zero."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, **kw)
    os.replace(tmp, path)

# Altitude (ft) below which + descending we treat as "approaching landing"
DESCENT_ALT_THRESHOLD = 8000

# Ground speed (kt) above which a grounded aircraft is considered
# "taxiing" rather than parked. Only meaningful when we have a live
# ADS-B position for it (board/cache-sourced ground entries have no
# speed telemetry at all, so they're never classed as taxiing).
# Every timestamp this app shows to the user (poll "updated" time, cache
# "last seen" times, etc.) must read correctly in Nepal time regardless
# of what timezone the server itself runs in -- Render's containers run
# in UTC, which previously leaked straight through to the display,
# making every one of these timestamps roughly 5h45m off from what a
# Nepal-based user actually sees on a clock.
NEPAL_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=45))


def _nepal_now():
    """Current Nepal wall-clock time as a naive datetime (no tzinfo) --
    deliberately naive so it compares cleanly against timestamp strings
    already formatted in Nepal time elsewhere (mixing naive and
    timezone-aware datetimes raises a TypeError in Python), while still
    being correct regardless of the server's own system timezone."""
    return datetime.datetime.now(tz=NEPAL_TZ).replace(tzinfo=None)


def nepal_now_str():
    """Current time, formatted as Nepal local time, for any 'updated'
    field shown to the user."""
    return _nepal_now().strftime("%Y-%m-%d %H:%M:%S")


TAXI_SPEED_KT = 3

# ----------------------------------------------------------------------
# Request pacing
# ----------------------------------------------------------------------
# FlightRadar24 answers HTTP 429 when requests arrive in a quick burst
# from one address -- the log showed the first query of every cycle
# succeeding and the next three, sent immediately after, all refused.
# Every request to FlightRadar24 from any part of this app goes through
# _pace(), which keeps at least FR24_MIN_GAP_SEC between them.
FR24_MIN_GAP_SEC = 2.5
_pace_lock = threading.Lock()
_last_request_at = [0.0]


def _pace():
    with _pace_lock:
        wait = _last_request_at[0] + FR24_MIN_GAP_SEC - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.time()


def is_rate_limited_error(exc):
    text = str(exc).lower()
    return "429" in text or "too many requests" in text


# Ground position queries: KTM (the hub) every cycle, the secondary
# airports taking turns, so one cycle is 3 requests instead of 4.
_zoom_turn = [0]

# Below this speed, within ON_AIRPORT_KM of an airport, an aircraft is on
# the ground even if the feed's air/ground flag says otherwise.
GROUND_SPEED_MAX_KT = 50

# An aircraft last seen parked at a flight's ORIGIN with that flight plan
# loaded is assumed to have flown it once this much time has passed
# (longest domestic sector is ~1h; this leaves room for delays).
PARKED_PLAN_ASSUMED_FLOWN_SEC = 2 * 3600

# A flight that departed this long ago has landed, whatever FlightRadar24
# still says about it (its tracking sometimes never records the landing).
STALE_DEPARTURE_SEC = 3 * 3600
ON_AIRPORT_KM = 4

# How far into the future a departures-board entry can be and still be
# trusted for its tail assignment. Beyond this, FR24 sometimes shows a
# provisional/typical aircraft that can still change before departure.
NEAR_TERM_DEPARTURE_WINDOW_SEC = 90 * 60

# Relative trust of the schedule-board entries, worst first, used so a
# firmer entry for an aircraft always wins over a weaker one:
#   "scheduled"   -- FR24's least firm wording ("Scheduled"); the tail is
#                    often just the typical aircraft for that route.
#   "provisional" -- a real tail on a flight that is still some way off.
#   "imminent"    -- that flight is due within the near-term window.
#   "done"        -- the event has already happened (landed / departed).
# Anything below "done" is labelled tentative in the app.
_TIER_RANK = {"scheduled": 0, "provisional": 1, "imminent": 2, "done": 3}

# Friendly names for common Nepali domestic airports (extend as needed --
# unknown codes just show as-is).
AIRPORT_NAMES = {
    "KTM": "Kathmandu", "PKR": "Pokhara", "BWA": "Bhairahawa",
    "BIR": "Biratnagar", "KEP": "Nepalgunj", "JKR": "Janakpur",
    "SIF": "Simara", "RJB": "Rajbiraj", "BHR": "Bharatpur",
    "TMI": "Tumlingtar", "LUA": "Lukla", "RUM": "Rumjatar",
    "BDP": "Bhadrapur", "DHI": "Dhangadhi", "SKH": "Surkhet",
    "PHH": "Pokhara",
    "VNS": "Varanasi", "CCU": "Kolkata",
}


def airport_label(code):
    if not code:
        return "?"
    return f"{code}/{AIRPORT_NAMES[code]}" if code in AIRPORT_NAMES else code


def airport_coords(code):
    """[lat, lon] for a known airport code, or None."""
    c = AIRPORT_ALL_COORDS.get(code)
    return [c[0], c[1]] if c else None


def nearest_airport(lat, lon, max_km=15):
    """
    The airport (from Buddha Air's known network) this GPS position is
    actually at, or None if it isn't within max_km of any of them.

    This is the strict way to say where a grounded aircraft IS: its
    transponder's real position. The flight plan riding on the same
    transponder can't be trusted for this -- right after landing it
    still carries the flight that just finished (e.g. PKR->KTM for a
    plane now sitting at KTM), so guessing "origin" or "destination"
    from it was what showed AMD at PKR after it had landed at KTM.

    PKR (old Pokhara strip) is skipped in favour of PHH (the current
    Pokhara airport FlightRadar24 uses) so one city never splits into
    two filter chips.
    """
    import math
    if lat is None or lon is None:
        return None
    best_code, best_km = None, None
    for code, (alat, alon) in AIRPORT_ALL_COORDS.items():
        if code == "PKR":
            continue
        # haversine distance in km
        p1, p2 = math.radians(lat), math.radians(alat)
        dp, dl = p2 - p1, math.radians(alon - lon)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        km = 6371 * 2 * math.asin(math.sqrt(a))
        if best_km is None or km < best_km:
            best_code, best_km = code, km
    return best_code if best_km is not None and best_km <= max_km else None


def _fmt_nepal_dt(ts):
    """Epoch seconds -> 'HH:MM' in Nepal time, or 'DD Mon HH:MM' when it's
    not from today (Nepal's today), so an old arrival can't be mistaken
    for a recent one."""
    if not ts:
        return None
    t = datetime.datetime.fromtimestamp(ts, tz=NEPAL_TZ)
    today = datetime.datetime.now(tz=NEPAL_TZ).date()
    return t.strftime("%H:%M") if t.date() == today else t.strftime("%d %b %H:%M")


def sector_label_of(sector):
    if not sector:
        return "Sector unknown"
    o, d = sector.split("-")
    return f"{airport_label(o)} -> {airport_label(d)}"


# ----------------------------------------------------------------------
# Sector cache (persisted so a grounded aircraft still shows the last
# sector it was personally seen flying, across restarts)
# ----------------------------------------------------------------------

def load_sector_cache():
    if os.path.exists(SECTOR_CACHE_FILE):
        try:
            with open(SECTOR_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_sector_cache(cache):
    try:
        _atomic_write_json(SECTOR_CACHE_FILE, cache, indent=2)
    except Exception as e:
        print(f"[cache save failed] {e}")


def update_sector_cache(cache, by_reg, timestamp):
    """Remember the latest known sector for every aircraft we can see
    right now (air or ground with an active flight plan). Only ever
    caches a sector that passes the plausibility check, so a bad reading
    can't get remembered and then served back later as if trustworthy."""
    for reg, f in by_reg.items():
        if getattr(f, "is_held", False):
            continue  # not a new sighting -- don't refresh "last seen"
        sector = sector_of(f)
        if sector and _is_plausible_sector(sector):
            cache[reg] = {
                "sector": sector,
                "on_ground_when_seen": f.on_ground,
                # If it was on the ground, which airport its GPS put it at
                # -- a fact, so we never have to guess from the sector
                # later when the live signal is gone.
                "at_airport": nearest_airport(f.latitude, f.longitude) if f.on_ground else None,
                "updated": timestamp,
            }
    return cache


# ----------------------------------------------------------------------
# Arrival-time tracking ("landed at ...")
# ----------------------------------------------------------------------
# FlightRadar24 only gives a landing time for a flight once it has landed
# AND it still sits on an airport board we read. For everything else the
# only honest source is our own observation: the moment an aircraft we
# were watching in the air is no longer airborne. That time is recorded
# here, kept on disk, and is what ground/taxi lists sort by (latest
# arrival on top) and what the cards display in Nepal time.

GROUND_SINCE_FILE = os.path.join(DATA_DIR, "buddha_air_ground_since.json")


def load_ground_since():
    try:
        with open(GROUND_SINCE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_ground_since(ground_since):
    try:
        _atomic_write_json(GROUND_SINCE_FILE, ground_since)
    except Exception as e:
        print(f"[ground_since save failed] {e}")


def update_ground_since(ground_since, prev_air, by_reg, hub_schedule, now_ts,
                        inferred_landings=None, history=None):
    """
    Maintains {registration: {"ts": epoch, "source": "board"|"observed"|"inferred"}}
    -- when each currently-grounded aircraft arrived.

      - Airborne now (live, or held through a feed gap): it hasn't
        arrived anywhere; drop any old entry.
      - Was airborne on the previous poll and is now SEEN ON THE GROUND:
        we watched it arrive -> record this moment ("observed").
      - Was airborne and is now simply MISSING from the feed: that is
        NOT a landing. Only if the air-hold logic concluded it landed
        (last seen low and descending near an airport) is a time
        recorded, and that time is when it was last seen ("inferred").
        This is what used to stamp fake "Landed HH:MM" times on aircraft
        that were really still flying, whenever one query failed.
      - Any board entry carrying a real landing time for it: use that
        (FlightRadar24's own touchdown time is more exact than ours),
        unless it's an older landing than the one we already have.
      - Never invented: an aircraft we first meet already on the ground
        with no board landing simply has no arrival time.

    prev_air is a set owned by the caller, updated in place.
    """
    inferred_landings = inferred_landings or {}
    for reg in FLEET:
        f = by_reg.get(reg)
        if f is not None and not f.on_ground:
            ground_since.pop(reg, None)
            prev_air.add(reg)
            continue

        if reg in prev_air:
            prev_air.discard(reg)
            if f is not None:
                ground_since[reg] = {"ts": now_ts, "source": "observed",
                                     "airport": nearest_airport(f.latitude, f.longitude)}
            elif reg in inferred_landings:
                ground_since[reg] = {"ts": inferred_landings[reg]["ts"], "source": "inferred",
                                     "airport": inferred_landings[reg].get("airport")}
        elif f is None and reg in inferred_landings and reg not in ground_since:
            ground_since[reg] = {"ts": inferred_landings[reg]["ts"], "source": "inferred",
                                     "airport": inferred_landings[reg].get("airport")}

        landing = (hub_schedule.get(reg) or {}).get("last_landing")
        if landing and landing.get("ts") and now_ts - 86400 <= landing["ts"] <= now_ts:
            cur = ground_since.get(reg)
            if cur is None or landing["ts"] >= cur["ts"] - 120:
                ground_since[reg] = {"ts": landing["ts"], "source": "board"}

        # The aircraft's own flight history: FlightRadar24's recorded
        # touchdown for its last flight. Fills in aircraft that landed
        # before the tracker started, at any airport.
        if f is None or f.on_ground:
            hl = history_last_done(history, reg, now_ts)
            if hl and hl.get("ts_exact", True):
                cur = ground_since.get(reg)
                if cur is None or hl["ts"] >= cur["ts"] - 120:
                    ground_since[reg] = {"ts": hl["ts"], "source": "board",
                                         "airport": hl.get("at_airport")}
    return ground_since


# ----------------------------------------------------------------------
# Air hold: keeps airborne aircraft airborne through feed gaps
# ----------------------------------------------------------------------

class HeldFlight:
    """Stand-in for a FlightRadarAPI Flight, built from the last real
    airborne sighting of an aircraft that is temporarily missing from the
    feed. Has every attribute the rest of this module reads from a Flight,
    plus is_held / last_seen_ts so the API can say it isn't live."""

    FIELDS = ("registration", "callsign", "altitude", "ground_speed",
              "vertical_speed", "heading", "latitude", "longitude",
              "origin_airport_iata", "destination_airport_iata")

    def __init__(self, snap):
        for k in self.FIELDS:
            setattr(self, k, snap.get(k))
        self.on_ground = False
        self.is_held = True
        self.last_seen_ts = snap.get("seen_ts")


def _snapshot_of(f, now_ts):
    snap = {k: getattr(f, k, None) for k in HeldFlight.FIELDS}
    snap["seen_ts"] = now_ts
    return snap


def _landing_airport_guess(snap):
    """Airport an aircraft was about to land at when last seen: low,
    descending (or very slow) and close to a known airport. None if the
    last sighting doesn't look like the end of a flight."""
    alt = snap.get("altitude")
    vs = snap.get("vertical_speed") or 0
    if alt is None or alt > LANDING_GUESS_MAX_ALT_FT:
        return None
    if vs > 200:  # climbing -- just took off, not landing
        return None
    return nearest_airport(snap.get("latitude"), snap.get("longitude"),
                           max_km=LANDING_GUESS_RADIUS_KM)


class AirHold:
    """Remembers the last airborne sighting of every aircraft and decides,
    when one goes missing, whether to keep showing it in the air, call it
    landed, or give up. State survives restarts via AIR_HOLD_FILE."""

    def __init__(self):
        # reg -> {"snap": {...}, "missing_good_polls": int}
        self.entries = {}

    # -- persistence ------------------------------------------------
    @classmethod
    def load(cls):
        hold = cls()
        try:
            with open(AIR_HOLD_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            now = datetime.datetime.now().timestamp()
            for reg, e in (data.get("entries") or {}).items():
                seen = (e.get("snap") or {}).get("seen_ts") or 0
                if reg in FLEET and now - seen <= AIR_HOLD_MAX_SEC:
                    hold.entries[reg] = e
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[air hold load failed] {e}")
        return hold

    def save(self):
        try:
            _atomic_write_json(AIR_HOLD_FILE, {"entries": self.entries})
        except Exception as e:
            print(f"[air hold save failed] {e}")

    def airborne_regs(self):
        return set(self.entries.keys())

    # -- per-poll update --------------------------------------------
    def apply(self, by_reg, sweep_ok, now_ts):
        """
        Mutates by_reg in place: aircraft that are missing but still
        held get a HeldFlight stand-in so they stay in the air.
        Returns {reg: {"ts": last_seen_ts, "airport": code}} for aircraft
        judged to have landed while out of sight.
        """
        inferred = {}
        for reg in FLEET:
            f = by_reg.get(reg)
            if f is not None:
                if f.on_ground:
                    self.entries.pop(reg, None)
                else:
                    self.entries[reg] = {"snap": _snapshot_of(f, now_ts), "missing_good_polls": 0}
                continue

            e = self.entries.get(reg)
            if e is None:
                continue
            snap = e["snap"]
            if sweep_ok:
                e["missing_good_polls"] = e.get("missing_good_polls", 0) + 1
            age = now_ts - (snap.get("seen_ts") or now_ts)

            landed_at = _landing_airport_guess(snap)
            if landed_at and e["missing_good_polls"] >= AIR_LANDED_MISSING_POLLS:
                inferred[reg] = {"ts": snap.get("seen_ts") or now_ts, "airport": landed_at}
                self.entries.pop(reg, None)
                continue
            if age > AIR_HOLD_MAX_SEC:
                self.entries.pop(reg, None)
                continue
            by_reg[reg] = HeldFlight(snap)
        return inferred


# ----------------------------------------------------------------------
# Per-aircraft flight history (what FlightRadar24's own app shows)
# ----------------------------------------------------------------------
# When you tap a parked aircraft in the official FlightRadar24 app, the
# sector it shows comes from that AIRCRAFT's flight history -- not from an
# airport board. The boards we read only cover KTM/PHH/BIR and only the
# flights nearest "now", so an aircraft that landed before the tracker
# started (or landed at BWA, KEP, BDP...) had nothing to go on and showed
# "Sector unknown". This looks the history up per registration, slowly
# (a few aircraft per minute), and keeps the result on disk.

AIRCRAFT_HISTORY_URL = "https://api.flightradar24.com/common/v1/flight/list.json"
HISTORY_FILE = os.path.join(DATA_DIR, "buddha_air_aircraft_history.json")
HISTORY_MAX_AGE_SEC = 24 * 3600     # landings older than this aren't shown


def _http_get_json(url, params, timeout=12):
    """GET a FlightRadar24 JSON endpoint the same way FlightRadarAPI does
    (browser-like TLS via curl_cffi, which FlightRadarAPI already
    installs), falling back to plain requests if curl_cffi isn't there."""
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://www.flightradar24.com",
        "referer": "https://www.flightradar24.com/",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    }
    _pace()
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, params=params, headers=headers, timeout=timeout, impersonate="chrome")
    except ImportError:
        import requests as creq
        r = creq.get(url, params=params, headers=headers, timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError("HTTP Error 429: Too Many Requests")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP Error {r.status_code}")
    return r.json()


def parse_aircraft_history(raw, now_ts=None):
    """
    Turns FlightRadar24's per-registration flight list into
      {"last_done": {...} | None, "next": {...} | None, "live": {...} | None}
    last_done = the most recent flight that really landed:
      {"sector", "status", "callsign", "ts" (landing time), "at_airport"}
    next      = the next flight it hasn't departed yet (for reference).
    live      = a flight that has departed but not landed.
    """
    now_ts = now_ts or datetime.datetime.now().timestamp()
    rows = _dig(raw, "result", "response", "data") or []
    last_done = nxt = live = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        o = _dig(row, "airport", "origin", "code", "iata")
        d = _dig(row, "airport", "destination", "code", "iata")
        # A diversion lands somewhere else -- that's where it really is.
        real_d = _dig(row, "airport", "real", "code", "iata")
        sector = f"{o}-{d}" if (o and d) else None
        if not _is_plausible_sector(sector):
            continue
        dep_real = _dig(row, "time", "real", "departure")
        arr_real = _dig(row, "time", "real", "arrival")
        dep_sched = _dig(row, "time", "scheduled", "departure")
        status = (_dig(row, "status", "text") or "").strip()
        generic = (_dig(row, "status", "generic", "status", "text") or "").lower()
        is_live = bool(_dig(row, "status", "live"))
        callsign = _dig(row, "identification", "callsign")

        landed_ts = arr_real
        if not landed_ts and dep_real and not is_live:
            est_arr = (_dig(row, "status", "generic", "eventTime", "utc")
                       or _dig(row, "time", "estimated", "arrival")
                       or _dig(row, "time", "other", "eta"))
            sched_arr = _dig(row, "time", "scheduled", "arrival")
            said_landed = generic in ("landed", "arrived") or status.lower().startswith(("landed", "arrived"))
            if said_landed:
                landed_ts = est_arr or sched_arr or dep_real
            elif (est_arr or sched_arr) and (est_arr or sched_arr) < now_ts - 20 * 60:
                # Departed, not live any more, and due 20+ min ago: it has
                # landed even though FR24 never stamped an arrival time
                # (common at small Nepali airports).
                landed_ts = est_arr or sched_arr

        ts_exact = True
        if not landed_ts and dep_real and dep_real < now_ts - STALE_DEPARTURE_SEC:
            # Departed hours ago and FlightRadar24 never recorded the
            # landing (it lost the track -- e.g. AJX's BHA614 PHH->KTM still
            # "Estimated 15:30" at 23:30). No sector in this network is
            # anywhere near that long, so it has landed; we just don't know
            # exactly when, so no "Landed HH:MM" time is shown for it.
            landed_ts = dep_real
            ts_exact = False
            status = "Landed (time not recorded by FlightRadar24)"

        if landed_ts and landed_ts <= now_ts + 300:
            if last_done is None or landed_ts > last_done["ts"]:
                at = real_d if (real_d and real_d in AIRPORT_ALL_COORDS) else d
                last_done = {"sector": sector, "status": status or "Landed",
                             "callsign": callsign, "ts": landed_ts, "at_airport": at,
                             "ts_exact": ts_exact}
        elif dep_real and not landed_ts:
            if live is None or dep_real > live["ts"]:
                live = {"sector": sector, "status": status, "callsign": callsign, "ts": dep_real}
        elif not dep_real and dep_sched and dep_sched >= now_ts - 3 * 3600:
            if nxt is None or dep_sched < nxt["ts"]:
                nxt = {"sector": sector, "status": status or "Scheduled",
                       "callsign": callsign, "ts": dep_sched}
    return {"last_done": last_done, "next": nxt, "live": live}


HISTORY_DEBUG_DIR = os.path.join(DATA_DIR, "buddha_air_history_debug")


def fetch_aircraft_history(reg, limit=10):
    raw = _http_get_json(AIRCRAFT_HISTORY_URL, {
        "query": reg.lower(), "fetchBy": "reg", "page": 1, "limit": limit,
    })
    out = parse_aircraft_history(raw)
    if not out.get("last_done"):
        # Keep FlightRadar24's raw answer so a "none found" can be checked
        # by hand (send this file if an aircraft keeps showing none).
        try:
            os.makedirs(HISTORY_DEBUG_DIR, exist_ok=True)
            _atomic_write_json(os.path.join(HISTORY_DEBUG_DIR, f"{reg}.json"), raw, indent=1)
        except Exception:
            pass
    out["fetched_at"] = datetime.datetime.now().timestamp()
    return out


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Entries with no completed flight are dropped so they're looked
        # up again straight away (they may come from an older version of
        # the reader that couldn't recognise the landing).
        return {k: v for k, v in data.items()
                if k in FLEET and isinstance(v, dict) and v.get("last_done")}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[history load failed] {e}")
        return {}


def save_history(history):
    try:
        _atomic_write_json(HISTORY_FILE, history)
    except Exception as e:
        print(f"[history save failed] {e}")


def history_last_done(history, reg, now_ts=None):
    """The aircraft's last completed flight from its history, if recent."""
    now_ts = now_ts or datetime.datetime.now().timestamp()
    ld = (history or {}).get(reg, {}).get("last_done")
    if ld and ld.get("ts") and now_ts - HISTORY_MAX_AGE_SEC <= ld["ts"] <= now_ts + 300:
        return ld
    return None


def load_hub_schedule():
    """Last schedule-board snapshot saved to disk, if it is recent enough
    to trust. Without this, every restart left the board data empty until
    the first slow poll finished, and every sector that came from a board
    fell to "Unknown" in the meantime."""
    try:
        with open(HUB_SCHEDULE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        age = datetime.datetime.now().timestamp() - (data.get("saved_at") or 0)
        if age <= HUB_SCHEDULE_MAX_AGE_SEC and isinstance(data.get("schedule"), dict):
            return data["schedule"]
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[hub schedule load failed] {e}")
    return {}


def save_hub_schedule(schedule):
    try:
        _atomic_write_json(HUB_SCHEDULE_FILE, {
            "saved_at": datetime.datetime.now().timestamp(),
            "schedule": schedule,
        }, default=str)
    except Exception as e:
        print(f"[hub schedule save failed] {e}")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _dig(d, *keys):
    """Safely walk a nested dict, returning None on any missing key/type."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def sector_of(f):
    """Return 'ORIG-DEST' if both known, else None."""
    o, d = f.origin_airport_iata, f.destination_airport_iata
    if o and d:
        return f"{o}-{d}"
    return None


def _is_plausible_sector(sector):
    """
    Rejects a sector before it's ever shown, regardless of which source
    produced it (live ADS-B, schedule board, or our own cache). This is
    a blanket safety net independent of any one specific bug -- every
    sector must involve two airports actually in Buddha Air's known
    network, and the two sides must differ (except the KTM-KTM loop of
    an Everest Experience mountain flight, which is legitimate).

    Anything failing this check is treated exactly like "no sector data"
    from that source, so the normal priority chain (live -> board ->
    cache -> unknown) just falls through to the next source instead of
    ever displaying something implausible.
    """
    if not sector or "-" not in sector:
        return False
    o, d = sector.split("-", 1)
    if o not in AIRPORT_ALL_COORDS or d not in AIRPORT_ALL_COORDS:
        return False
    if o == d and sector != "KTM-KTM":
        return False
    return True


# ----------------------------------------------------------------------
# FlightRadar24 fetching
# ----------------------------------------------------------------------

def fetch_tracked_flights(fr):
    """Backwards-compatible wrapper: just the {registration: Flight} map."""
    return fetch_tracked_flights_ex(fr)[0]


def zoom_airports_this_cycle():
    """KTM every cycle; PHH and BIR alternate."""
    others = [c for c in AIRPORT_COORDS if c != HUB_AIRPORT]
    pick = others[_zoom_turn[0] % len(others)] if others else None
    _zoom_turn[0] += 1
    return [HUB_AIRPORT] + ([pick] if pick else [])


def fetch_tracked_flights_ex(fr):
    """
    Returns (by_reg, sweep_ok, rate_limited). sweep_ok is False when the
    en-route sweep failed this cycle -- in that case an airborne aircraft
    missing from by_reg says nothing about whether it landed.
    rate_limited is True if FlightRadar24 refused ANY query with HTTP 429,
    so the caller can slow down (not only when every query failed).

    Combines several position queries per cycle:

    1. A small, zoomed-in query centered on each watched airport
       (AIRPORT_COORDS), which is what actually gets FlightRadar24 to
       include grounded/taxiing aircraft in the response.
    2. One wide-area sweep (CENTER_LAT/RADIUS_KM) covering all of Nepal,
       which reliably returns airborne aircraft en route between cities
       but tends to drop ground traffic entirely.

    Running 4 separate queries means the SAME aircraft can occasionally
    turn up in more than one of them with conflicting on_ground states
    (e.g. one query still shows it airborne/descending a moment after
    another correctly shows it's already landed). When that happens, an
    on_ground=True sighting always wins over an on_ground=False one,
    regardless of which query returned it or ran last -- "on the
    ground" is the more specific, harder-to-fake signal, whereas a
    stale "still airborne" reading is exactly the kind of thing that
    can briefly linger after a real landing. Without this priority, an
    aircraft that has genuinely landed (or is taxiing) could keep
    showing as airborne/descending, or lose its taxi state, purely
    based on query ordering.
    """
    sightings = {}  # registration -> list of every Flight object seen this cycle
    failures = []   # (label, exception) for every query that did not answer

    def record(f):
        if f.registration in FLEET:
            sightings.setdefault(f.registration, []).append(f)

    zoomed = []
    for code in zoom_airports_this_cycle():
        lat, lon = AIRPORT_COORDS[code]
        try:
            bounds = fr.get_bounds_by_point(lat, lon, TAXI_WATCH_RADIUS_KM * 1000)
            _pace()
            for f in fr.get_flights(bounds=bounds):
                record(f)
            zoomed.append(code)
        except Exception as e:
            failures.append((f"airport-zoom {code}", e))
            print(f"[airport-zoom fetch failed for {code}] {e}")

    sweep_ok = True
    try:
        n, s_, w, e_ = SWEEP_BOUNDS
        bounds = f"{n},{s_},{w},{e_}"
        _pace()
        for f in fr.get_flights(bounds=bounds):
            record(f)
    except Exception as e:
        sweep_ok = False
        failures.append(("wide-area", e))
        print(f"[wide-area fetch failed] {e}")

    # If NOT ONE query answered, this is not "the fleet is on the ground",
    # it is "we have no data": FlightRadar24 being unreachable or
    # rate-limiting us (HTTP 429) must not be allowed to masquerade as a
    # full fleet of grounded aircraft with no sectors. Raise instead, so
    # the caller keeps showing the last good snapshot and backs off.
    if failures and not zoomed and not sweep_ok:
        raise RuntimeError(f"every position query failed ({failures[0][0]}: {failures[0][1]})")

    by_reg = {}
    for reg, flights in sightings.items():
        on_ground_sightings = [f for f in flights if f.on_ground]
        by_reg[reg] = on_ground_sightings[0] if on_ground_sightings else flights[0]

    # FlightRadar24's air/ground flag flickers during landing roll and
    # taxi -- the altitude it reports there is the airport's own height
    # (KTM is ~4,400 ft), so a plane rolling out at KTM could flip back
    # to "In the Air / CRUISE" for a poll or two. No aircraft in this
    # fleet flies below ~90 kt, so anything slower than that sitting on
    # top of an airport is on the ground, whatever the flag says.
    for f in by_reg.values():
        if not f.on_ground and f.ground_speed is not None \
                and f.ground_speed < GROUND_SPEED_MAX_KT \
                and nearest_airport(f.latitude, f.longitude, max_km=ON_AIRPORT_KM):
            f.on_ground = True

    rate_limited = any(is_rate_limited_error(e) for _, e in failures)
    return by_reg, sweep_ok, rate_limited


def _extract_timing(flight):
    """
    Pulls scheduled / estimated / real arrival timestamps plus FR24's
    own computed ETA out of a flight's "time" tree (same shape whether
    the flight came from an arrivals or departures list -- it always
    describes the whole flight, so "arrival" here always means arrival
    at the flight's actual destination, which is what a delay/ETA
    prediction should be about regardless of which airport's board we
    found it on).

    Returns None if there's not enough to compute anything useful.
    """
    scheduled_arr = _dig(flight, "time", "scheduled", "arrival")
    real_arr = _dig(flight, "time", "real", "arrival")
    estimated_arr = _dig(flight, "time", "estimated", "arrival") or _dig(flight, "time", "other", "eta")

    if not scheduled_arr and not estimated_arr and not real_arr:
        return None

    effective_arr = real_arr or estimated_arr or scheduled_arr
    delay_minutes = None
    if effective_arr and scheduled_arr:
        delay_minutes = round((effective_arr - scheduled_arr) / 60)

    return {
        "scheduled_arrival_ts": scheduled_arr,
        "eta_ts": estimated_arr or real_arr,
        "real_arrival_ts": real_arr,
        "delay_minutes": delay_minutes,
    }


def _fmt_local_time(ts):
    """Unix timestamp -> 'HH:MM' in Nepal time (UTC+5:45)."""
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts, tz=NEPAL_TZ).strftime("%H:%M")


def _fetch_single_airport_schedule(fr, airport_code, best_ts, result, landings=None,
                                   last_done=None, plan_ts=None, plans=None):
    """Query one airport's schedule board and merge matches into the shared
    best_ts/result/plans dicts (mutated in place): `result` keeps the most
    recent thing each aircraft has COMPLETED, and `plans` keeps the flight
    it is due to operate NEXT.

    Trust filtering, to avoid showing a wrong/stale sector -- but without
    throwing away information that's still worth showing at reduced
    confidence (an earlier version of this discarded far-future pending
    entries entirely, which fixed a real accuracy bug but also pushed
    too many aircraft into "Sector unknown" when a reasonable, clearly
    labeled guess was actually available):
      - A bare "Scheduled" status is FlightRadar24's least firm wording,
        so it is kept as the WEAKEST tier rather than trusted: it can
        never replace a confirmed event, and callers show it only when
        nothing better exists, labelled as tentative. It is not dropped,
        because outside a few hours around the middle of the day almost
        every entry on the board for a parked aircraft is exactly this --
        dropping them was what left most of the fleet showing "Sector
        unknown" for hours on end.
      - An entry describing something that's already happened (landed /
        departed) or is imminent/overdue (within
        NEAR_TERM_DEPARTURE_WINDOW_SEC) is marked "confirmed": airline
        tail assignments are reliable this close to or past the event.
      - A pending entry further out than that is marked "provisional"
        instead of being dropped -- still a real tail FR24 is currently
        showing for that route, just with a higher chance of changing
        before it actually happens. A pending ENTRY can never overwrite
        a confirmed one for the same aircraft (regardless of timestamp),
        only another pending entry can replace it.

    Two extra outputs, both needed to describe a parked aircraft without
    ever inventing anything (this is what left the fleet showing "Sector
    unknown" between flights):
      - last_done: the most recent arrival that has really landed, kept
        separately because a later DEPARTURE entry would otherwise
        replace it and hide both the sector it just flew and the airport
        it is now sitting at. A parked aircraft is at the destination of
        the flight it last completed.
      - plan_ts: timestamps for the "next flight" entries kept in
        `result`, so callers can tell a confirmed, imminent assignment
        apart from a tentative one further out.
    """
    try:
        _pace()
        raw = fr.get_airport_details(airport_code, flight_limit=100)
    except Exception as e:
        print(f"[schedule fetch failed for {airport_code}] {e}")
        return False

    plugin = _dig(raw, "airport", "pluginData") or {}
    schedule = plugin.get("schedule", {}) if isinstance(plugin, dict) else {}
    now_ts = datetime.datetime.now().timestamp()

    found_any = False
    for section_name, hub_side in (("arrivals", "destination"), ("departures", "origin")):
        other_side = "origin" if hub_side == "destination" else "destination"
        time_key = "arrival" if hub_side == "destination" else "departure"

        section = schedule.get(section_name, {}) if isinstance(schedule, dict) else {}
        entries = section.get("data", []) if isinstance(section, dict) else []

        for entry in entries:
            flight = entry.get("flight", {}) if isinstance(entry, dict) else {}
            reg = _dig(flight, "aircraft", "registration")
            if not reg or reg not in FLEET:
                continue

            status = (_dig(flight, "status", "text") or "").strip()
            if not status:
                # No status and no timing would mean nothing to go on at
                # all; a status of "Scheduled" is kept, but ranked lowest.
                status = "Scheduled"

            other_code = _dig(flight, "airport", other_side, "code", "iata")
            if not other_code:
                continue

            orig = other_code if hub_side == "destination" else airport_code
            dest = airport_code if hub_side == "destination" else other_code

            # Reject any board entry pointing at an airport outside
            # Buddha Air's known network -- catches bad parses or a
            # coincidental registration collision with unrelated traffic
            # before it ever gets treated as a real sector.
            if not _is_plausible_sector(f"{orig}-{dest}"):
                continue

            ts = (
                _dig(flight, "time", "real", time_key)
                or _dig(flight, "time", "estimated", time_key)
                or _dig(flight, "time", "scheduled", time_key)
                or 0
            )

            if section_name == "departures":
                already_happened = any(
                    word in status.lower() for word in ("departed", "airborne", "en route", "en-route")
                )
            else:  # arrivals
                already_happened = "land" in status.lower()

            # Departed hours ago but never marked landed/departed on the
            # board (FR24 lost the track): it has happened. Without this a
            # long-dead "Estimated 15:30" entry ranked as AJX's "imminent"
            # next flight at 23:30 and kept it at PHH.
            if not already_happened and ts and ts < now_ts - STALE_DEPARTURE_SEC \
                    and _dig(flight, "time", "real", "departure"):
                already_happened = True

            # Remember this aircraft's most recent REAL landing seen on any
            # board, independent of which entry ends up "best" below -- a
            # later departure entry would otherwise hide the landing time.
            if section_name == "arrivals" and landings is not None:
                real_arr = _dig(flight, "time", "real", "arrival")
                if real_arr and (reg not in landings or real_arr > landings[reg]["ts"]):
                    landings[reg] = {"ts": real_arr, "airport": airport_code, "from": other_code}

            confirmed = already_happened or ts <= now_ts + NEAR_TERM_DEPARTURE_WINDOW_SEC
            # Which of the four trust tiers this entry sits in. "Scheduled"
            # is FR24's least firm wording and stays near the bottom no
            # matter how near-term it is.
            if already_happened:
                tier = "done"
            elif status.lower().startswith("scheduled"):
                tier = "scheduled"
            elif confirmed:
                tier = "imminent"
            else:
                tier = "provisional"

            # Also capture the flight's own identification callsign
            # (e.g. "BHA855") straight from the board -- this is what
            # lets us match a grounded aircraft to its flight number even
            # when it has no live radar signal at all (callsign is only
            # ever populated from live ADS-B otherwise; a lot of grounded
            # aircraft never get one until they actually push back).
            board_callsign = _dig(flight, "identification", "callsign")

            entry_data = {
                "sector": f"{orig}-{dest}",
                "status": status or section_name,
                "timing": _extract_timing(flight),
                "callsign": board_callsign,
                "confirmed": confirmed,
                "tier": tier,
                "ts": ts,
                "upcoming": not already_happened,
            }

            # The flight this aircraft most recently COMPLETED -- kept
            # apart from the "best entry" below, because for a parked
            # aircraft this is both the sector it last flew and the
            # airport it is standing at right now.
            if last_done is not None and already_happened and section_name == "arrivals" \
                    and _is_plausible_sector(entry_data["sector"]):
                prev = last_done.get(reg)
                if prev is None or ts >= prev.get("ts", 0):
                    last_done[reg] = dict(entry_data, at_airport=airport_code)

            # Two independent "best" slots per aircraft:
            #   result -- the most recent COMPLETED event (what it just did)
            #   plans  -- the entry that is DUE SOONEST (what it does next)
            # Keeping them separate stops a landing from hours ago from
            # outranking the departure that is about to leave just because
            # "latest timestamp" happens to favour the old event.
            existing = result.get(reg)
            existing_rank = _TIER_RANK.get(existing.get("tier") if existing else None, -1)
            my_rank = _TIER_RANK[tier]

            if already_happened:
                if existing is None or existing.get("upcoming") or ts >= best_ts.get(reg, 0):
                    best_ts[reg] = ts
                    result[reg] = entry_data
                    found_any = True
            elif plans is not None:
                # Keep this as the "next flight": firmer tier wins, and
                # within a tier the soonest departure is the relevant one.
                existing_plan = plans.get(reg)
                plan_rank = _TIER_RANK.get(existing_plan.get("tier") if existing_plan else None, -1)
                if existing_plan is None or my_rank > plan_rank:
                    should_store = True
                elif my_rank < plan_rank:
                    should_store = False
                else:
                    prev_ts = plan_ts.get(reg)
                    should_store = prev_ts is None or ts < prev_ts
                if should_store:
                    plans[reg] = entry_data
                    plan_ts[reg] = ts
                    found_any = True
                if existing is None or existing.get("upcoming"):
                    # Nothing completed is known for this aircraft yet, so
                    # the plan is also the best answer available.
                    result[reg] = entry_data
                    best_ts[reg] = ts
            else:
                # No separate plans store: one slot per aircraft, keeping
                # whichever entry is the firmer of the two.
                if existing is None or my_rank > existing_rank:
                    best_ts[reg] = ts
                    result[reg] = entry_data
                    found_any = True

    if not found_any:
        try:
            debug_path = SCHEDULE_DEBUG_FILE.replace(".json", f"_{airport_code}.json")
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2)
        except Exception:
            pass

    return found_any


def fetch_hub_schedule(fr):
    """
    Check Kathmandu's schedule board plus any SECONDARY_AIRPORTS, and
    return {registration: entry} for every Buddha Air tail number found
    across all boards checked.

    Each entry describes the MOST RELEVANT flight known for that aircraft:
    the one it is due to operate next if that is due within the near-term
    window, otherwise the most recent thing it completed. It carries:
      - "sector", "status", "callsign", "timing": as before.
      - "tier"/"confirmed": "done" (already landed/departed), "imminent"
        (due within the near-term window), "provisional" (a real tail,
        further out) or "scheduled" (FR24's least firm wording). Anything
        below "done" is shown in the app as "Airport board (tentative)".
      - "last_done": the most recent arrival that has really landed, plus
        "at_airport" -- the airport that landing put the aircraft at.
        This is what lets a parked aircraft still show the sector it
        just flew and where it is standing, hours after landing, without
        anyone having to guess from a future flight plan.
      - "last_landing": raw touchdown timestamp/details (see
        update_ground_since).
    """
    result = {}
    best_ts = {}
    plan_ts = {}
    plans = {}
    landings = {}
    last_done = {}
    for airport_code in [HUB_AIRPORT] + SECONDARY_AIRPORTS:
        _fetch_single_airport_schedule(fr, airport_code, best_ts, result, landings,
                                       last_done=last_done, plan_ts=plan_ts, plans=plans)

    # An imminent next flight is the more relevant fact whenever we have
    # one: it is about to happen, and its origin side is where the
    # aircraft is standing. It replaces the completed entry as this
    # aircraft's primary record; anything less certain than "imminent"
    # stays in the background as a labelled fallback (see
    # build_fleet_records).
    for reg, plan in plans.items():
        if plan.get("tier") == "imminent":
            merged = dict(plan)
            if reg in last_done:
                merged["last_done"] = last_done[reg]
            result[reg] = merged
        elif reg not in result:
            result[reg] = plan
        else:
            result[reg].setdefault("plan", plan)

    for reg, landing in landings.items():
        if reg in result:
            result[reg]["last_landing"] = landing
    for reg, done in last_done.items():
        # An aircraft can be known only from its last completed arrival
        # (every later board entry about it fell outside the trusted
        # window) -- that is still a fact worth keeping, so give it a
        # record of its own rather than dropping it.
        entry = result.setdefault(reg, {
            "sector": done["sector"],
            "status": done["status"],
            "timing": done.get("timing"),
            "callsign": done.get("callsign"),
            "ts": done.get("ts", 0),
            "tier": "done",
            "confirmed": True,
        })
        entry["last_done"] = done
    return result


def build_eta_fields(hub_schedule, reg, sector=None):
    """
    Turns a hub_schedule timing entry into the fields the frontend
    actually displays. Returns a dict with everything set to None when
    there's nothing to show, so callers can always merge this in
    unconditionally.

    When `sector` is given, the timing is only used if the board entry
    really describes that same flight -- otherwise an aircraft whose card
    shows the sector it already completed would be tagged with the ETA of
    a completely different, not-yet-flown leg.
    """
    empty = {
        "eta_time": None, "eta_minutes": None,
        "scheduled_arrival_time": None, "delay_minutes": None, "delay_label": None,
    }
    entry = hub_schedule.get(reg)
    if not entry or not entry.get("timing"):
        return empty
    if sector is not None and entry.get("sector") != sector:
        return empty

    timing = entry["timing"]
    now_ts = datetime.datetime.now().timestamp()

    eta_ts = timing.get("eta_ts")
    scheduled_ts = timing.get("scheduled_arrival_ts")
    delay_minutes = timing.get("delay_minutes")

    eta_minutes = None
    if eta_ts and not timing.get("real_arrival_ts"):  # only "minutes remaining" if not already landed
        eta_minutes = max(0, round((eta_ts - now_ts) / 60))

    delay_label = None
    if delay_minutes is not None:
        if delay_minutes > 5:
            delay_label = f"+{delay_minutes} min"
        elif delay_minutes < -5:
            delay_label = f"{delay_minutes} min early"
        else:
            delay_label = "On time"

    return {
        "eta_time": _fmt_local_time(eta_ts),
        "eta_minutes": eta_minutes,
        "scheduled_arrival_time": _fmt_local_time(scheduled_ts),
        "delay_minutes": delay_minutes,
        "delay_label": delay_label,
    }


def resolve_missing_sectors_via_details(fr, by_reg, hub_schedule):
    """
    Last-resort per-flight lookup for airborne aircraft the compact feed
    and the schedule boards both couldn't explain. Patches
    f.origin_airport_iata / f.destination_airport_iata in place.
    """
    for reg, f in by_reg.items():
        if getattr(f, "is_held", False):
            continue  # held stand-in, not a live flight FR24 can look up
        if f.on_ground or sector_of(f) or reg in hub_schedule:
            continue
        try:
            _pace()
            details = fr.get_flight_details(f)
        except Exception as e:
            print(f"[flight details fetch failed for {reg}] {e}")
            continue
        o = _dig(details, "airport", "origin", "code", "iata")
        d = _dig(details, "airport", "destination", "code", "iata")
        if o and d:
            f.origin_airport_iata = o
            f.destination_airport_iata = d


# ----------------------------------------------------------------------
# Classification -> structured records (JSON-friendly)
# ----------------------------------------------------------------------

# How long a cached "last known sector" stays trustworthy before we treat
# it as too stale to show. Without this, an aircraft that's rarely
# re-observed flying (or grounded somewhere our airport boards don't
# cover) could keep showing a sector from many hours or days ago,
# indefinitely, with no way to tell it's outdated.
CACHE_MAX_AGE_HOURS = 12


def _nepal_str_to_epoch(text):
    """'YYYY-mm-dd HH:MM:SS' written in Nepal time -> epoch seconds."""
    try:
        dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=NEPAL_TZ).timestamp()
    except Exception:
        return None


def _cache_entry_is_fresh(entry):
    try:
        updated = datetime.datetime.strptime(entry["updated"], "%Y-%m-%d %H:%M:%S")
        age_hours = (_nepal_now() - updated).total_seconds() / 3600
        return age_hours <= CACHE_MAX_AGE_HOURS
    except Exception:
        return False  # malformed/missing timestamp -- don't trust it


def _current_airport_for_ground(sector, source, note, has_landed=None, cache_entry=None):
    """
    Where a grounded aircraft actually IS right now, as opposed to the
    full sector that explains why we know that. Used for the "filter by
    airport" buttons -- e.g. AJL (PKR->KTM) and AMD (BWA->KTM) should
    both show up under a "KTM" filter, since that's where they landed.

    Heuristic (documented since it's inherently a guess for a couple of
    these sources):
      - source "live" (FR24 still shows an active flight plan while
        parked): normally seen just before departure, so the aircraft
        is at the ORIGIN side of that plan.
      - source "board": whether the aircraft has actually landed is
        decided from `has_landed` -- a real, structured arrival
        timestamp (FR24 only ever sets this once the aircraft has
        genuinely touched down), NOT by guessing from the status text.
        Text-matching for the word "landed" was fragile: FlightRadar24
        uses different wording across entries ("Arrived", locale
        differences, etc.), and a board entry that had actually landed
        but wasn't worded exactly as expected would incorrectly show
        the aircraft as still sitting at its ORIGIN instead of where it
        actually landed -- e.g. AMD landing PKR->KTM showing as still
        "at PKR". If `has_landed` isn't available for some reason, this
        falls back to the old text check rather than guessing blind.
      - source "cache": this is the last sector fleet_core personally
        saw the aircraft flying: since it's no longer visible flying
        anything newer, assume it landed and is sitting at the
        DESTINATION.
      - source "unknown": no sector at all, so no current airport.
    """
    if not sector:
        return None
    origin, dest = sector.split("-")
    if source == "live":
        return origin
    if source == "board":
        if has_landed is not None:
            return dest if has_landed else origin
        return dest if "land" in (note or "").lower() else origin
    if source == "cache":
        # Last seen on the ground with a GPS fix: that airport is a fact.
        if cache_entry and cache_entry.get("on_ground_when_seen") and cache_entry.get("at_airport"):
            return cache_entry["at_airport"]
        return dest
    return None


def build_fleet_records(by_reg, cache, hub_schedule, timestamp, ground_since=None, history=None):
    """
    Returns a list of dicts, one per FLEET registration, e.g.:

    {
      "registration": "9N-AOC",
      "status": "air",                 # "air" | "ground"
      "sector": "BDP-KTM",             # or None
      "sector_label": "BDP/Bhadrapur -> KTM/Kathmandu",
      "sector_source": "live",         # "live" | "board" | "board_provisional"
                                       # | "cache" | "unknown"
      "callsign": "BHA954",
      "altitude_ft": 16525,
      "ground_speed_kt": 237,
      "vertical_speed_fpm": 64,
      "phase": "CRUISE",               # air only: CRUISE|CLIMBING|DESCENDING
      "note": None,                    # ground only: human-readable status
      "destination_airport": "KTM",    # air only: filter key (where it's headed)
      "destination_airport_label": "KTM/Kathmandu",
      "current_airport": None,         # ground only: filter key (where it IS)
      "current_airport_label": None,
      "updated": "2026-09-03 12:12:19"
    }
    """
    records = []
    ground_since = ground_since or {}

    # Board entries are used in two clearly separated roles, because for a
    # parked aircraft the two questions ("which sector did it last fly?"
    # and "where is it standing?") have different right answers:
    #
    #   - An IMMINENT entry (its next flight is due within the near-term
    #     window) is the most relevant thing to show: the aircraft is
    #     about to fly it, and being the origin side tells us where it is
    #     parked right now.
    #   - Otherwise the most recent COMPLETED event is used -- the sector
    #     it just flew, whose destination is where it now stands.
    #   - A TENTATIVE entry (tier "provisional"/"scheduled") still counts,
    #     but only ever as a labelled last resort, and only where nothing
    #     we actually observed is available. Discarding these was what
    #     left most of the fleet showing "Sector unknown" -- outside a few
    #     hours around the middle of the day, every board entry for a
    #     parked aircraft is a future rotation.
    #   - entry["last_done"] is the most recent arrival that really
    #     landed: the sector it just flew, and a hard fact about which
    #     airport it is now parked at.
    for reg in FLEET:
        f = by_reg.get(reg)
        board = hub_schedule.get(reg) or {}
        board_done = board.get("last_done") or None
        hist_done = history_last_done(history, reg)
        # An imminent next flight takes over from the completed one, so
        # the card describes what this aircraft is doing now rather than
        # a landing from hours ago.
        planned = bool(board) and not board.get("confirmed", True)

        if f is not None and not f.on_ground:
            sector = sector_of(f)
            if not _is_plausible_sector(sector):
                sector = None
            source = "live"
            if not sector and board:
                candidate = board["sector"]
                if _is_plausible_sector(candidate):
                    sector = candidate
                    source = "board_provisional" if planned else "board"
            phase = "DESCENDING" if (f.vertical_speed or 0) < -200 else \
                    "CLIMBING" if (f.vertical_speed or 0) > 200 else "CRUISE"
            dest_code = sector.split("-")[1] if sector else None
            orig_code = sector.split("-")[0] if sector else None
            eta_fields = build_eta_fields(hub_schedule, reg, sector)
            held = bool(getattr(f, "is_held", False))
            seen_ts = getattr(f, "last_seen_ts", None)
            lost_sec = int(max(0, datetime.datetime.now().timestamp() - seen_ts)) if (held and seen_ts) else 0
            records.append({
                "registration": reg,
                "status": "air",
                # True when the feed has briefly lost this aircraft and the
                # card shows its last real sighting rather than a live one.
                "signal_lost": held,
                "signal_lost_seconds": lost_sec if held else None,
                "last_seen_time": _fmt_nepal_dt(seen_ts) if held else None,
                "sector": sector,
                "sector_label": sector_label_of(sector),
                "sector_source": source if sector else "unknown",
                "callsign": f.callsign or (board.get("callsign") if sector else None),
                "altitude_ft": f.altitude,
                "ground_speed_kt": f.ground_speed,
                "vertical_speed_fpm": f.vertical_speed,
                "phase": phase,
                "is_taxiing": False,
                "note": None,
                "destination_airport": dest_code,
                "destination_airport_label": airport_label(dest_code) if dest_code else "Unknown",
                "current_airport": None,
                "current_airport_label": None,
                "position": [f.latitude, f.longitude] if f.latitude is not None and f.longitude is not None else None,
                "position_is_live": not held,
                "heading": f.heading,
                "origin_coords": airport_coords(orig_code),
                "destination_coords": airport_coords(dest_code),
                "sort_ts": datetime.datetime.now().timestamp(),
                **eta_fields,
                "updated": timestamp,
            })
            continue

        # Grounded, or simply not in the live feed at all.
        sector = sector_of(f) if f is not None else None
        if not _is_plausible_sector(sector):
            sector = None
        ground_speed = f.ground_speed if f is not None else None
        is_taxiing = bool(f is not None and ground_speed is not None and ground_speed > TAXI_SPEED_KT)
        # Airport this board information says the aircraft is standing at
        # (see the branches below) -- prefer it over inferring a location
        # from the sector's shape.
        board_at = None

        gs_entry = ground_since.get(reg) or {}

        if sector:
            source = "live"
            note = f"Taxiing at {ground_speed} kt (callsign {f.callsign or '-'})" if is_taxiing \
                else f"On ground, flight plan active (callsign {f.callsign or '-'})"
        else:
            # Every piece of evidence about the flight this aircraft last
            # completed, each with the time it refers to. The NEWEST one
            # wins. A fixed order (board/history first, our own sighting
            # last) is what showed an aircraft that had just landed at
            # KTM under the airport its PREVIOUS flight went to: the
            # history lookup was older than what we had just watched.
            evidence = []
            if board_done and _is_plausible_sector(board_done.get("sector")):
                evidence.append((board_done.get("ts") or 0, board_done["sector"], "board",
                                 board_done.get("at_airport"), board_done.get("status") or "On ground"))
            if hist_done:
                evidence.append((hist_done["ts"], hist_done["sector"], "history",
                                 hist_done.get("at_airport"), hist_done.get("status") or "Landed"))
            ce = cache.get(reg)
            if ce and _cache_entry_is_fresh(ce) and _is_plausible_sector(ce.get("sector")):
                seen = _nepal_str_to_epoch(ce.get("updated")) or 0
                o_code, d_code = ce["sector"].split("-")
                at = ce.get("at_airport") if ce.get("on_ground_when_seen") else d_code
                note = f"Last seen on this sector at {ce.get('updated')}"
                if ce.get("on_ground_when_seen") and at == o_code \
                        and time.time() - seen > PARKED_PLAN_ASSUMED_FLOWN_SEC:
                    # We last saw it parked at the origin with this flight
                    # plan loaded, ready to go -- and that was long ago.
                    # Every sector in this network is far shorter than
                    # that, so it has flown it and is at the destination.
                    at = d_code
                    note = f"Seen ready to depart {o_code} at {ce.get('updated')}, so assumed flown to {d_code}"
                evidence.append((seen, ce["sector"], "cache", at, note))

            best = max(evidence, key=lambda e: e[0]) if evidence else None
            here = best[3] if best else None
            # Our own record of where it touched down beats an older
            # inference about its location.
            if gs_entry.get("airport") and (best is None or (gs_entry.get("ts") or 0) >= best[0] - 120):
                here = gs_entry["airport"]

            # A next flight that's about to depart is worth showing -- but
            # only if it departs from where the aircraft actually is. An
            # "imminent" PHH-KTM entry for an aircraft standing at KTM is
            # really the flight it just finished (worded unexpectedly on
            # the board), not its next one.
            imminent_ok = (
                board.get("tier") == "imminent"
                and _is_plausible_sector(board.get("sector"))
                and (here is None or board["sector"].split("-")[0] == here)
            )

            if imminent_ok:
                sector = board["sector"]
                source = "board"
                board_at = sector.split("-")[0]
                note = board.get("status")
            elif best:
                _, sector, source, board_at, note = best
                if here:
                    board_at = here
            elif board and _is_plausible_sector(board.get("sector")) \
                    and (here is None or board["sector"].split("-")[1 if board.get("tier") == "done" else 0] == here):
                # Nothing completed or recently observed is known, but the
                # board shows this aircraft's next rotation -- shown at
                # reduced confidence and labelled as such.
                sector = board["sector"]
                source = "board_provisional" if planned else "board"
                # A flight that has already happened (departed or landed)
                # left the aircraft at its destination; an upcoming one
                # means it is waiting at the origin.
                board_at = sector.split("-")[1] if board.get("tier") == "done" else sector.split("-")[0]
                note = board.get("status")
            else:
                sector = None
                source = "unknown"
                board_at = here
                note = "No sector data yet"

        current_code = board_at or _current_airport_for_ground(sector, source, note)
        # A live GPS fix beats every inference above: the airport the
        # aircraft is physically at IS where it is.
        if f is not None:
            gps_airport = nearest_airport(f.latitude, f.longitude)
            if gps_airport:
                current_code = gps_airport
        eta_fields = build_eta_fields(hub_schedule, reg, sector)

        # When this aircraft arrived, from the tracker's own record (see
        # update_ground_since). Unknown stays unknown -- it is never
        # replaced by a guess or by a future ETA, which is what used to
        # float not-yet-departed aircraft above ones that had just landed.
        gs = ground_since.get(reg)
        arrival_ts = gs["ts"] if gs else None

        orig_code = sector.split("-")[0] if sector else None
        dest_code = sector.split("-")[1] if sector else None

        # Best-known physical position for the map marker: a real live
        # ADS-B fix if we have one (taxiing or parked-with-flight-plan
        # aircraft do broadcast position), otherwise fall back to the
        # airport's reference coordinate -- an approximation (won't show
        # the exact gate/stand), clearly flagged via position_is_live.
        if f is not None and f.latitude is not None and f.longitude is not None:
            position = [f.latitude, f.longitude]
            position_is_live = True
        elif current_code:
            position = airport_coords(current_code)
            position_is_live = False
        else:
            position = None
            position_is_live = False

        records.append({
            "registration": reg,
            "status": "ground",
            "sector": sector,
            "sector_label": sector_label_of(sector),
            "sector_source": source,
            "callsign": (f.callsign if f is not None else None)
                        or (board.get("callsign") if sector else None),
            "altitude_ft": None,
            "signal_lost": False,
            "signal_lost_seconds": None,
            "last_seen_time": None,
            "ground_speed_kt": ground_speed,
            "vertical_speed_fpm": None,
            "phase": None,
            "is_taxiing": is_taxiing,
            "note": note,
            "destination_airport": None,
            "destination_airport_label": None,
            "current_airport": current_code,
            "current_airport_label": airport_label(current_code) if current_code else "Unknown",
            "position": position,
            "position_is_live": position_is_live,
            "heading": f.heading if f is not None else None,
            "origin_coords": airport_coords(orig_code),
            "destination_coords": airport_coords(dest_code),
            "sort_ts": arrival_ts,
            "ground_since_ts": arrival_ts,
            "ground_since_time": _fmt_nepal_dt(arrival_ts),
            "ground_since_source": gs["source"] if gs else None,
            **eta_fields,
            "updated": timestamp,
        })

    return records


def group_by_sector(records):
    """sector_label -> list of records, for either the air or ground set."""
    groups = {}
    for r in records:
        groups.setdefault(r["sector_label"], []).append(r)
    return groups


# ----------------------------------------------------------------------
# One full poll cycle -- the single function both front ends call
# ----------------------------------------------------------------------

def fetch_positions(fr):
    """The fast, cheap part of a poll cycle: live positions only (air/
    ground state, altitude, speed, heading). No schedule-board or
    per-flight-detail lookups here -- those are the slower, heavier part
    (see fetch_enrichment) that doesn't need refreshing nearly as often,
    since sector/ETA/delay data doesn't change second-to-second the way
    position does. Splitting these lets the visible map/altitude/speed
    stay close to real-time without hammering FlightRadar24's free feed
    at a rate that risks getting rate-limited or blocked entirely.
    """
    return fetch_tracked_flights(fr)


def fetch_positions_ex(fr):
    """Like fetch_positions, but also returns (by_reg, sweep_ok,
    rate_limited). Used by api_server.py so a failed sweep can't be
    mistaken for the whole airborne fleet landing."""
    return fetch_tracked_flights_ex(fr)


def fetch_enrichment(fr, by_reg):
    """The slower, heavier part of a poll cycle: airport schedule boards
    (sector/ETA/delay data) and per-flight detail resolution for any
    airborne aircraft still missing a sector. Mutates by_reg in place
    (the resolve step) and returns hub_schedule.
    """
    hub_schedule = fetch_hub_schedule(fr)
    resolve_missing_sectors_via_details(fr, by_reg, hub_schedule)
    return hub_schedule


def poll_once(fr, cache):
    """
    Does one complete cycle: fetch live positions, check schedule boards,
    resolve remaining unknowns, update the cache, and classify.
    Returns (records, updated_cache, timestamp).

    Used by tracker.py (the desktop tool), which only needs one cadence.
    api_server.py instead calls fetch_positions/fetch_enrichment directly
    at two different speeds -- see the "TWO-SPEED POLLING" section there.
    """
    by_reg = fetch_positions(fr)
    timestamp = nepal_now_str()

    hub_schedule = fetch_enrichment(fr, by_reg)

    cache = update_sector_cache(cache, by_reg, timestamp)
    save_sector_cache(cache)

    records = build_fleet_records(by_reg, cache, hub_schedule, timestamp)
    return records, cache, timestamp
