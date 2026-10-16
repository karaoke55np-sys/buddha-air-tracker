"""
FlightRadar24 access test
=========================
Run this ONCE on a server before moving the Buddha Air tracker there.
It tries every kind of FlightRadar24 lookup the tracker uses, gently
(a few seconds apart), and tells you whether that server is blocked.

Setup + run (on the server):
    pip install FlightRadarAPI
    python3 fr24_test.py
"""

import time

from FlightRadarAPI import FlightRadar24API

GAP_SEC = 4      # pause between requests -- same gentle pace as the tracker
ROUNDS = 3       # each test is tried this many times

fr = FlightRadar24API()


def live_positions_nepal():
    # The en-route sweep: all aircraft over Nepal.
    flights = fr.get_flights(bounds="30.7,22.3,79.8,88.9")
    bha = [f for f in flights if (f.callsign or "").startswith("BHA")]
    return f"{len(flights)} aircraft, {len(bha)} Buddha Air"


def ground_at_ktm():
    # The airport check: aircraft parked/taxiing at Kathmandu.
    bounds = fr.get_bounds_by_point(27.6966, 85.3591, 20000)
    return f"{len(fr.get_flights(bounds=bounds))} aircraft near KTM"


def ktm_schedule_board():
    # The airport board: sectors for parked aircraft.
    raw = fr.get_airport_details("KTM", flight_limit=50)
    sched = (((raw or {}).get("airport") or {}).get("pluginData") or {}).get("schedule") or {}
    n = len(((sched.get("arrivals") or {}).get("data")) or [])
    return f"{n} arrivals on the KTM board"


def aircraft_history():
    # The per-aircraft flight history (what fills in parked sectors).
    url = "https://api.flightradar24.com/common/v1/flight/list.json"
    params = {"query": "9n-anz", "fetchBy": "reg", "page": 1, "limit": 5}
    headers = {"accept": "application/json",
               "origin": "https://www.flightradar24.com",
               "referer": "https://www.flightradar24.com/",
               "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}
    try:
        from curl_cffi import requests as http
        r = http.get(url, params=params, headers=headers, timeout=15, impersonate="chrome")
    except ImportError:
        import requests as http
        r = http.get(url, params=params, headers=headers, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP Error {r.status_code}")
    rows = (((r.json().get("result") or {}).get("response") or {}).get("data")) or []
    return f"{len(rows)} recent flights found for 9N-ANZ"


TESTS = [
    ("Live positions over Nepal", live_positions_nepal),
    ("Aircraft on ground at KTM", ground_at_ktm),
    ("KTM schedule board", ktm_schedule_board),
    ("Aircraft flight history", aircraft_history),
]

print("Testing FlightRadar24 access from this server...\n")
results = {}
for name, test in TESTS:
    ok = 0
    for i in range(ROUNDS):
        try:
            detail = test()
            ok += 1
            print(f"  [OK]      {name}: {detail}")
        except Exception as e:
            msg = str(e)
            tag = "BLOCKED" if ("429" in msg or "403" in msg) else "ERROR"
            print(f"  [{tag}] {name}: {msg[:120]}")
        time.sleep(GAP_SEC)
    results[name] = ok

print("\n========== RESULT ==========")
for name, ok in results.items():
    print(f"  {name:28s} {ok}/{ROUNDS} {'PASS' if ok == ROUNDS else ('PARTLY' if ok else 'FAIL')}")
total = sum(results.values())
if total == ROUNDS * len(TESTS):
    print("\nGOOD: this server can reach FlightRadar24. Safe to move the tracker here.")
elif total == 0:
    print("\nBLOCKED: FlightRadar24 refuses this server. Don't move the tracker here.")
else:
    print("\nPARTLY BLOCKED: some lookups are refused. Expect missing data, like on Render.")
