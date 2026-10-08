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
import datetime

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

# Center point + radius (km) for the wide-area sweep that catches
# aircraft actually en route between cities.
CENTER_LAT = 27.7
CENTER_LON = 85.3
RADIUS_KM = 900

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

# Altitude (ft) below which + descending we treat as "approaching landing"
DESCENT_ALT_THRESHOLD = 8000

# Ground speed (kt) above which a grounded aircraft is considered
# "taxiing" rather than parked. Only meaningful when we have a live
# ADS-B position for it (board/cache-sourced ground entries have no
# speed telemetry at all, so they're never classed as taxiing).
TAXI_SPEED_KT = 3

# How far into the future a departures-board entry can be and still be
# trusted for its tail assignment. Beyond this, FR24 sometimes shows a
# provisional/typical aircraft that can still change before departure.
NEAR_TERM_DEPARTURE_WINDOW_SEC = 90 * 60

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
        with open(SECTOR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[cache save failed] {e}")


def update_sector_cache(cache, by_reg, timestamp):
    """Remember the latest known sector for every aircraft we can see
    right now (air or ground with an active flight plan). Only ever
    caches a sector that passes the plausibility check, so a bad reading
    can't get remembered and then served back later as if trustworthy."""
    for reg, f in by_reg.items():
        sector = sector_of(f)
        if sector and _is_plausible_sector(sector):
            cache[reg] = {
                "sector": sector,
                "on_ground_when_seen": f.on_ground,
                "updated": timestamp,
            }
    return cache


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
    """
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

    def record(f):
        if f.registration in FLEET:
            sightings.setdefault(f.registration, []).append(f)

    for code, (lat, lon) in AIRPORT_COORDS.items():
        try:
            bounds = fr.get_bounds_by_point(lat, lon, TAXI_WATCH_RADIUS_KM * 1000)
            for f in fr.get_flights(bounds=bounds):
                record(f)
        except Exception as e:
            print(f"[airport-zoom fetch failed for {code}] {e}")

    try:
        bounds = fr.get_bounds_by_point(CENTER_LAT, CENTER_LON, RADIUS_KM * 1000)
        for f in fr.get_flights(bounds=bounds):
            record(f)
    except Exception as e:
        print(f"[wide-area fetch failed] {e}")

    by_reg = {}
    for reg, flights in sightings.items():
        on_ground_sightings = [f for f in flights if f.on_ground]
        by_reg[reg] = on_ground_sightings[0] if on_ground_sightings else flights[0]

    return by_reg


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
    nepal_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=45))
    return datetime.datetime.fromtimestamp(ts, tz=nepal_tz).strftime("%H:%M")


def _fetch_single_airport_schedule(fr, airport_code, best_ts, result):
    """Query one airport's schedule board and merge matches into the
    shared best_ts/result dicts (mutated in place), keeping whichever
    entry per registration has the latest timestamp.

    Trust filtering, to avoid showing a wrong/stale sector:
      - A bare "Scheduled" status carries a default/typical aircraft for
        that route, not a confirmed real tail -- always skipped.
      - Any entry (arrival OR departure) describing something that
        hasn't happened yet and is still far in the future is *also*
        often provisional -- airlines can swap the assigned tail right
        up until close to the event, and FR24 sometimes shows a
        tentative assignment (status "Estimated"/"Delayed") well before
        that's firm, on either side of the board. These are skipped too,
        UNLESS the event has already happened (landed / departed) or is
        imminent (within NEAR_TERM_DEPARTURE_WINDOW_SEC), since
        assignments are much more reliable close to the actual event.
    """
    try:
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
            if status.lower() == "scheduled":
                continue

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

            # A pending (not-yet-happened) event far in the future carries
            # the same provisional-tail risk on either side of the board --
            # an "Estimated" arrival that hasn't landed yet can be just as
            # much of a placeholder/typical-aircraft guess as a pending
            # departure. Only trust it once it's already happened, or the
            # event is imminent/overdue (ts at or before now, or within the
            # near-term window ahead).
            if not already_happened and ts > now_ts + NEAR_TERM_DEPARTURE_WINDOW_SEC:
                continue  # too far out to trust the assigned tail yet

            # Also capture the flight's own identification callsign
            # (e.g. "BHA855") straight from the board -- this is what
            # lets us match a grounded aircraft to its flight number even
            # when it has no live radar signal at all (callsign is only
            # ever populated from live ADS-B otherwise; a lot of grounded
            # aircraft never get one until they actually push back).
            board_callsign = _dig(flight, "identification", "callsign")

            if reg not in best_ts or ts >= best_ts[reg]:
                best_ts[reg] = ts
                result[reg] = {
                    "sector": f"{orig}-{dest}",
                    "status": status or section_name,
                    "timing": _extract_timing(flight),
                    "callsign": board_callsign,
                }
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
    return {registration: {"sector": "ORIG-DEST", "status": text}} for
    any Buddha Air tail number found, keeping the most recent sighting
    per aircraft across all boards checked.
    """
    result = {}
    best_ts = {}
    for airport_code in [HUB_AIRPORT] + SECONDARY_AIRPORTS:
        _fetch_single_airport_schedule(fr, airport_code, best_ts, result)
    return result


def build_eta_fields(hub_schedule, reg):
    """
    Turns a hub_schedule timing entry into the fields the frontend
    actually displays. Returns a dict with everything set to None when
    there's nothing to show, so callers can always merge this in
    unconditionally.
    """
    empty = {
        "eta_time": None, "eta_minutes": None,
        "scheduled_arrival_time": None, "delay_minutes": None, "delay_label": None,
    }
    entry = hub_schedule.get(reg)
    if not entry or not entry.get("timing"):
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
        if f.on_ground or sector_of(f) or reg in hub_schedule:
            continue
        try:
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


def _cache_entry_is_fresh(entry):
    try:
        updated = datetime.datetime.strptime(entry["updated"], "%Y-%m-%d %H:%M:%S")
        age_hours = (datetime.datetime.now() - updated).total_seconds() / 3600
        return age_hours <= CACHE_MAX_AGE_HOURS
    except Exception:
        return False  # malformed/missing timestamp -- don't trust it


def _current_airport_for_ground(sector, source, note, has_landed=None):
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
        return dest
    return None


def build_fleet_records(by_reg, cache, hub_schedule, timestamp):
    """
    Returns a list of dicts, one per FLEET registration, e.g.:

    {
      "registration": "9N-AOC",
      "status": "air",                 # "air" | "ground"
      "sector": "BDP-KTM",             # or None
      "sector_label": "BDP/Bhadrapur -> KTM/Kathmandu",
      "sector_source": "live",         # "live" | "board" | "cache" | "unknown"
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

    for reg in FLEET:
        f = by_reg.get(reg)

        if f is not None and not f.on_ground:
            sector = sector_of(f)
            if not _is_plausible_sector(sector):
                sector = None
            source = "live"
            if not sector and reg in hub_schedule:
                candidate = hub_schedule[reg]["sector"]
                if _is_plausible_sector(candidate):
                    sector = candidate
                    source = "board"
            phase = "DESCENDING" if (f.vertical_speed or 0) < -200 else \
                    "CLIMBING" if (f.vertical_speed or 0) > 200 else "CRUISE"
            dest_code = sector.split("-")[1] if sector else None
            orig_code = sector.split("-")[0] if sector else None
            eta_fields = build_eta_fields(hub_schedule, reg)
            records.append({
                "registration": reg,
                "status": "air",
                "sector": sector,
                "sector_label": sector_label_of(sector),
                "sector_source": source if sector else "unknown",
                "callsign": f.callsign or hub_schedule.get(reg, {}).get("callsign"),
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
                "position_is_live": True,
                "heading": f.heading,
                "origin_coords": airport_coords(orig_code),
                "destination_coords": airport_coords(dest_code),
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

        if sector:
            source = "live"
            note = f"Taxiing at {ground_speed} kt (callsign {f.callsign or '-'})" if is_taxiing \
                else f"On ground, flight plan active (callsign {f.callsign or '-'})"
        elif reg in hub_schedule and _is_plausible_sector(hub_schedule[reg]["sector"]):
            sector = hub_schedule[reg]["sector"]
            source = "board"
            note = hub_schedule[reg]["status"]
        elif reg in cache and _cache_entry_is_fresh(cache[reg]) and _is_plausible_sector(cache[reg]["sector"]):
            sector = cache[reg]["sector"]
            source = "cache"
            note = f"Last seen on this sector at {cache[reg]['updated']}"
        else:
            sector = None
            source = "unknown"
            note = "No sector data yet"

        # Distinguish "we know it hasn't landed" (timing exists, no real
        # arrival yet -> trust that) from "we have no timing data at all"
        # (timing missing entirely -> has_landed=None, so the function
        # falls back to the text-based check instead of wrongly assuming
        # not-landed when the status text might still say otherwise).
        board_timing = hub_schedule.get(reg, {}).get("timing") if source == "board" else None
        has_landed = bool(board_timing.get("real_arrival_ts")) if board_timing is not None else None

        current_code = _current_airport_for_ground(sector, source, note, has_landed=has_landed)
        eta_fields = build_eta_fields(hub_schedule, reg)
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
            "callsign": f.callsign if f is not None else hub_schedule.get(reg, {}).get("callsign"),
            "altitude_ft": None,
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
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    hub_schedule = fetch_enrichment(fr, by_reg)

    cache = update_sector_cache(cache, by_reg, timestamp)
    save_sector_cache(cache)

    records = build_fleet_records(by_reg, cache, hub_schedule, timestamp)
    return records, cache, timestamp
