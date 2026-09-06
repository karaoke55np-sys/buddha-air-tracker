# Merging the Live Tracker into your Buddha Air web app

## What changed

- **`index.html`** — added a "Flight Board" / "Live Tracker" tab bar right
  under the header. Your existing board (schedule, chat, stats, history)
  is untouched, just wrapped so it can be hidden when the Live Tracker
  tab is active. The new tab has its own sub-tabs: **Approaching/Final**,
  **In the Air**, **On Ground** — each a grid of aircraft cards pulling
  from FlightRadar24 data.
- **`server.js`** — added one new route, `GET /api/fleet-status`, which
  proxies to the Python `api_server.py` (the FlightRadar24 tracker) at
  `http://localhost:5051/api/fleet-status`, the same way your existing
  `frontend-server.js` already proxies to your Node backend. Nothing
  else in `server.js` was touched.
- **`fleet_core.py` / `api_server.py`** — unchanged from before, just
  included here again for convenience since `server.js` now depends on
  `api_server.py` running.

**`app.js` and `style.css` weren't touched or used** — I noticed your
`index.html` already has all its CSS and JS inline (`<style>` /
`<script>` tags), so those two uploaded files aren't actually wired into
the page as-is. If they're meant to be used, let me know and I'll fold
this same integration into them instead — for now this works with what
`index.html` actually loads.

## Why a separate Python process for this one feature
FlightRadar24 blocks plain browser/axios requests — the Python library
gets past that with TLS-fingerprint handling that's non-trivial to
replicate in Node. Rather than reimplement that, `api_server.py` keeps
doing the FlightRadar24 polling in Python, and `server.js` just forwards
to it — the same proxy pattern your app already uses elsewhere.

## One command instead of two
`server.js` now auto-starts `api_server.py` as a child process when it
boots, so `node server.js` (or `npm run dev`) is the only command you
need — Ctrl+C stops both cleanly (tested: killing Node also kills the
Python child, no orphaned process left running).

**Requirements for this to work:**
1. Put `api_server.py` and `fleet_core.py` in the **same folder** as
   `server.js` (not a separate one).
2. `python` (or `py`) must be on your Windows PATH.
3. `pip install FlightRadarAPI flask` still needs to be run once, same
   as before — Node starts the script, it doesn't install its
   dependencies for you.

Python's console output shows up prefixed with `[fleet-api]` in the same
terminal as your Node logs, so you can still see what it's doing. If
`python`/`py` isn't found, or `api_server.py` is missing from the
folder, Node logs a clear warning and keeps running normally — the board
and chat features aren't affected, only the Live Tracker tab.

## Running it
```
# Terminal 1 -- everything (Node auto-starts the Python tracker too)
node server.js

# Then open http://localhost:3000 as usual
```
(Only needed once beforehand: `pip install FlightRadarAPI flask`.)

If `api_server.py` isn't running, the Live Tracker tab won't error out —
it shows "Could not reach live tracker (api_server.py) ... is it
running?" in place of data, and the rest of the app (board, chat) keeps
working normally.

## What "Approaching / Final" means
An aircraft counts as approaching when it's airborne, descending
(vertical speed below -200 fpm), and under 8,000 ft — the same threshold
`tracker.py`'s desktop notifications use. Change `APPROACH_ALT_FT` near
the bottom of `index.html`'s script (and `DESCENT_ALT_THRESHOLD` in
`fleet_core.py` if you want the desktop notifications to match) if you
want a different cutoff.

## Sector filter buttons (Approaching + On Ground tabs)
Both tabs now show clickable filter chips (reusing the same chip style
as the Flight Board's On Time/Boarding/Revised filters) for airports
actually present in that tab's current data, plus an "All" reset.

- **On Ground** filters by where the aircraft **currently is**, not the
  full sector. So if `9N-AJL` landed at KTM from Pokhara and `9N-AMD`
  landed at KTM from Bhairahawa, both show up under a **KTM** filter
  chip together — exactly the behavior asked for. Verified with a jsdom
  test simulating that exact scenario before shipping this.
- **Approaching/Final** filters by destination — e.g. click **PKR** to
  see only aircraft currently on approach into Pokhara.
- Chips rebuild every refresh from whatever's actually in the data, so
  an airport only appears as an option when there's something there to
  filter to.

**One documented heuristic worth knowing:** a grounded aircraft's
current airport is *derived*, not something FR24 states directly — see
`_current_airport_for_ground()` in `fleet_core.py` for the exact logic
(which source + status combinations mean "at origin" vs "at
destination"). Correct for the common cases (landed, waiting to depart)
but a best-effort inference for a couple of edge-case status texts. If a
specific aircraft ever files under the wrong airport, send the snapshot
the way you did for the ANZ/AJS issue earlier and it can be refined the
same way.

## Sector coverage check + international airports added
Went through your full sector list against what the app already
recognizes for friendly names. Important thing to understand first:
**no sector was ever being hidden or filtered out** — `airport_label()`
just falls back to showing the raw 3-letter code if it doesn't have a
friendly name for it, so every route always displayed, just sometimes
as a plain code instead of a name. That said, everything on your
domestic list (Pokhara, Bhairahawa, Biratnagar, Bharatpur, Simara,
Bhadrapur, Nepalgunj, Janakpur, Dhangadhi, Surkhet, Rajbiraj,
Tumlingtar) was already mapped. Added the two that weren't: **VNS**
(Varanasi) and **CCU** (Kolkata) for the international sectors. The
Pokhara cross-sectors (Bhairahawa/Bharatpur/Nepalgunj) are covered since
Pokhara's own schedule board is already one of the `SECONDARY_AIRPORTS`
checked. Everest Experience mountain flights loop back to KTM, so they
may show as "KTM -> KTM" if FR24 reports them that way — harmless, just
a quirk worth knowing about if you see it.

## New: Taxiing tab
Added a 4th tab next to On Ground, plus a matching stat card. An
aircraft counts as taxiing when it's grounded **and** FR24's live feed
shows it moving (>3 kt) — that live speed telemetry is only available
for aircraft we can currently see on radar (not for ones sourced from
the airport schedule board or from memory of where they last flew), so
this only ever reflects real-time movement, not a guess.

Taxiing aircraft are moved **out of** the On Ground tab into this one
(not duplicated in both) — verified with a jsdom test simulating an
aircraft taxiing after landing and one waiting to depart, confirming
each ends up in exactly one tab, with the right count in each. The
Taxiing tab has its own sector filter chips too, working the same way
as On Ground's.

## Fix: taxiing aircraft weren't showing up at all
Root cause found: FlightRadar24's live feed only includes
grounded/taxiing aircraft when the queried area is small and
zoomed-in — the wide regional sweep used to catch aircraft flying
between cities (all of Nepal in one box) silently drops ground traffic
entirely, no matter how fast a plane is actually moving on the tarmac.

Fixed by adding a small, zoomed-in query centered on each watched
airport (KTM, Pokhara, Biratnagar) specifically for ground/taxi
detection, merged with the wide sweep for airborne aircraft. Verified
with a test simulating exactly this situation — two aircraft taxiing
that the wide sweep genuinely couldn't see at all, both correctly
picked up once the airport-zoomed queries were added, while the
airborne aircraft from the wide sweep was still preserved. This adds 3
small extra API calls per poll cycle (well within the existing 2-minute
polling budget).

If you add more bases to `SECONDARY_AIRPORTS` later, also add their
coordinates to `AIRPORT_COORDS` in `fleet_core.py` or ground/taxi
detection won't extend to them.

## Fix: occasional wrong departure/arrival sector
Likely cause: FlightRadar24's schedule board can show a tail assignment
for a **departure that hasn't happened yet**, and that assignment isn't
always final — airlines sometimes swap aircraft, and FR24 occasionally
shows a provisional/typical tail well before it's confirmed (similar to
the plain "Scheduled" issue fixed earlier, just with an "Estimated" or
"Delayed" label on it instead, which we were trusting).

Fixed by only trusting a departures-board entry's tail assignment when
either the flight has actually departed already, or its departure is
imminent (within 90 minutes) — assignments get much more reliable close
to the real event. Farther-out departure entries are now skipped rather
than risking a wrong sector; the aircraft falls back to its last
confirmed sector (arrival data, or your own tracker's memory) instead.
Arrivals-board entries aren't affected by this — an arrival reflects a
flight already in progress or completed, so its tail is a fact, not a
guess.

Tested against exactly this pattern before sending: a far-future
"Estimated" departure was correctly excluded, a near-term one (30 min
out) was correctly kept, and an already-"Departed" entry was correctly
trusted regardless of its timestamp.

**Honest caveat:** this reduces the failure mode, it doesn't eliminate
it entirely — a last-minute tail swap in that final 90-minute window is
still possible and would show briefly wrong until FR24's data corrects
itself. If you keep seeing a *specific* aircraft showing a wrong sector,
send me that snapshot (like the ANZ case earlier) and I'll dig into
whether it's this same pattern or something else.

## New: ETA + delay predictions per aircraft
Every aircraft card (Approaching, In the Air, On Ground, Taxiing) now
shows an ETA and delay chip when the data's available — e.g.
`ETA 20:19 (22m)` with a red `+12 min` badge, or green `-8 min early`,
or a neutral `On time` for anything within a 5-minute band either way.

**No extra API calls needed for this** — the airport schedule boards
we already fetch every cycle (KTM + Pokhara + Biratnagar) include each
flight's scheduled/estimated/real arrival times and FR24's own computed
ETA; this just extracts and surfaces data that was already being pulled
down. Since virtually every Buddha Air sector touches one of those three
airports, coverage should be high without added load.

Tested before sending:
- A still-airborne flight running 12 minutes late showed the correct
  ETA, minutes-remaining, and `+12 min` delay chip.
- An already-landed flight that arrived 3 minutes early correctly showed
  `On time` (inside the noise band) with no "minutes remaining" (since
  it's already down).
- A flight with no board timing data at all correctly renders with no
  ETA block, rather than a blank or broken one.

One honest limitation: ETA/delay is only as good as FlightRadar24's own
board data — if a flight isn't on any of the three watched boards (rare
for this fleet, but possible for something like an ad-hoc charter), it
simply won't have an ETA chip rather than showing a wrong one.

## New: click any aircraft for a live map + full detail view
Clicking any aircraft card, in any tab (Approaching, In the Air, On
Ground, Taxiing), opens a modal with:

- **A live map** (Leaflet, free dark-themed tiles, no API key needed)
  showing the aircraft's own position, its origin and destination
  airports as markers, and a dashed route line between them. The map
  auto-fits to whatever's actually available — one point, two, or three.
- The aircraft's own icon **rotates to match its real heading** (for
  airborne/taxiing aircraft with live position data).
- **A full detail panel** next to the map: sector, callsign, altitude/
  speed/vertical speed/heading (airborne) or current airport (ground),
  the ETA/delay chip from the last feature, the status note, data
  source tag, and last-updated time — all reusing data already in each
  record, nothing new to fetch.

**On real aircraft position vs. approximation:** for airborne and
taxiing aircraft, the marker is the actual live ADS-B position. For a
grounded aircraft with no live signal (board/cache-sourced), there's no
gate-level position data available at all, so it's placed at the
airport's reference coordinate instead — the popup and info panel are
upfront about this ("Approximate (at KTM/Kathmandu)") rather than
implying false precision.

**Backend additions (fleet_core.py):** every record now carries
`position`, `position_is_live`, `heading`, `origin_coords`, and
`destination_coords` — computed from a new `AIRPORT_ALL_COORDS` table
covering every airport in your full sector list (separate from the
smaller `AIRPORT_COORDS` used for ground-detection zoom queries — extend
the "ALL" table freely, it's just static reference data with no API
cost).

**Testing note:** I can't load real map tiles or run real Leaflet
rendering from this environment (no network path to tile servers here),
so I validated this two ways instead: unit-tested the coordinate/heading
data on the Python side against known scenarios, and tested the
JavaScript integration logic against a mocked Leaflet object confirming
it places markers at the exact right coordinates, draws the route line
correctly, and calls fitBounds with the right points for three different
click scenarios (airborne with live position, approximate-position
ground, and modal close). The actual tile rendering itself is Leaflet's
own well-established code, not something this integration needed to
reimplement — but give the visuals a look on your machine and let me
know if anything looks off.

## Bug fix: "Next Boarding" was showing as active Boarding
Root cause confirmed in `server.js`: the desk board's status column
literally says `"NEXT BOARDING"` for flights queued after the current
one — but the old detection was just `statusText.includes('boarding')`,
which matches that phrase too, incorrectly flagging queued flights as
actively boarding right now (e.g. BUD 855 / BUD 611 both showed
"BOARDING" while the real boarding flight, per the desk site's own gate
banner, was Simara 553).

Three fixes, all tested against the exact scenario before sending:

1. **"Next Boarding" is now its own status** (`Next` / "UP NEXT" badge,
   neutral gray) — distinct from actively `Boarding` (orange). Verified:
   a row saying "NEXT BOARDING" now classifies as `Next`, not `Boarding`.
2. **The gate banner is now parsed as the authoritative source** for
   which flight is actually boarding — e.g. "GATE 4 SIMARA 553" is read
   directly from the page text (regex-based, not tied to a specific HTML
   selector, so it's resilient to markup changes) and that flight number
   is matched against the table rows and force-classified as `Boarding`
   regardless of what its own row's status text says. If this parsing
   ever fails to find a gate banner, it now saves `desk_board_debug.html`
   next to `server.js` so it can be calibrated the same way we fixed the
   FlightRadar24 schedule board earlier — send me that file if you see
   the "Could not find a gate banner" warning in the console.
3. **Departure status is now cross-checked against FlightRadar24**, per
   your request. After scraping, each flight's callsign (`BHA<number>`)
   is matched against the live tracker's airborne aircraft; a match
   forces status to `Departed` (blue badge), overriding whatever the
   desk board's text still says — since the manual board can lag behind
   reality, but FlightRadar24 showing it airborne is definitive proof.
   Verified: a flight with a matching airborne callsign correctly flips
   to `Departed` even though its scraped status said "Next".

This does add one HTTP call from `server.js` to the Python tracker
(`localhost:5051`) per board refresh — cheap, and it fails silently back
to desk-board-only status if the Python tracker isn't running.

## New aircraft: 9N-AOS
Added to `FLEET` in `fleet_core.py` — now tracked across the whole live
tracker (air/ground/taxi/approach, filters, map, ETA) the same as every
other tail number.

## Fix: wrong sector data + missing taxiing aircraft (same root cause)
Both bugs you reported — AMD showing as still "descending Nepalgunj to
Kathmandu" while actually on the ground, and taxiing aircraft not
showing up — traced back to the same bug in `fetch_tracked_flights`.

We run 4 separate position queries per cycle (KTM/Pokhara/Biratnagar
zoomed queries + one wide-area sweep). The same aircraft can
occasionally show up in more than one of them with **conflicting**
on-ground states — e.g. one query already reflects a real landing while
another still has a moment-old "airborne, descending" snapshot. The old
code just let whichever query happened to run last silently overwrite
the others, with no regard for which one was actually correct — so an
aircraft that had genuinely landed (or started taxiing) could keep
showing as airborne purely based on query order, and vice versa.

Fixed by collecting every sighting of each aircraft across all 4
queries, then always keeping an **on_ground=True** sighting over a
conflicting on_ground=False one, regardless of which query found it or
when. Ground state is the harder, more specific signal to obtain in the
first place (that's the entire reason the airport-zoomed queries exist)
— a stale "still airborne" reading is exactly the kind of thing that
can briefly linger after a real landing, so it should never win.

Tested against the exact reported scenario before sending: simulated
KTM's zoom correctly showing an aircraft on the ground while the wide
sweep still had a stale "descending KEP→KTM" reading for the same tail
— confirmed the ground reading now wins. Also confirmed a taxiing
aircraft's speed and taxi-flag survive the identical kind of conflict,
which is what was silently breaking taxi detection too.

## Deploying to Render
Three new files handle this: `Dockerfile`, `requirements.txt`, and
`render.yaml`. The app was already ported to run as one process that
auto-starts its Python sidecar (see "One command instead of two"
above) — deploying to Render is the same architecture, just
containerized.

### What I fixed to make this actually deployable
Two things in the code assumed a Windows desktop, and would have failed
silently or crashed on a Linux server:

1. **Hardcoded port 3000.** Render assigns a dynamic port via `$PORT`
   and expects the app to listen on it. `server.js` now uses
   `process.env.PORT || 3000` — unchanged locally, correct on Render.
2. **`fleet_core.py` wrote to `~/Desktop`**, which doesn't exist on
   Linux. It now checks for `FLEET_DATA_DIR` first (set to `/app/data`
   in the Dockerfile), falls back to `~/Desktop` if that exists
   (unchanged behavior on your Windows PC), and otherwise creates a
   local `data/` folder next to the code. Verified all three paths
   actually resolve and create directories correctly before shipping
   this. Chat history (`chat-data.json`) now follows the same
   `FLEET_DATA_DIR` setting too, so one persistent disk (see below)
   covers both if you add one.

### Deploy steps
1. Push this project (including the new `Dockerfile`) to a GitHub repo.
2. On Render: **New +** → **Blueprint**, point it at the repo — it'll
   read `render.yaml` and set everything up automatically. (Or: **New +**
   → **Web Service** → select the repo → set **Environment: Docker** —
   Render auto-detects the `Dockerfile`.)
3. Deploy. Render builds the image (installs both Node and Python
   dependencies — verified the Python side installs cleanly with no
   compilation needed, including `curl_cffi`, the trickiest dependency)
   and starts it with `node server.js`, which auto-spawns
   `api_server.py` inside the same container exactly like it does on
   your PC.

### Important: ephemeral storage
Render's default filesystem **resets on every redeploy/restart** —
chat messages/users and the sector-memory cache will be lost each time
you deploy a change, unless you add a **persistent Disk** (Render
dashboard → your service → Disks → mount path `/app/data`, matches the
`FLEET_DATA_DIR` already set in `render.yaml`). This is a paid-tier
Render feature. Without it, the app still works fine — flight tracking
rebuilds its picture within a couple of poll cycles either way — it
just means chat history won't survive a redeploy.

### What I could and couldn't verify from here
I don't have Docker available in this environment, so I couldn't run
the actual `docker build`. What I did verify directly: the Python
virtualenv + pip install steps the Dockerfile performs succeed cleanly
(including the `curl_cffi` wheel, which is the dependency most likely
to cause build issues), and the port/data-directory portability fixes
behave correctly under Linux (tested with no `~/Desktop` present, and
with `FLEET_DATA_DIR` both set and unset). The Dockerfile itself follows
standard, well-established patterns — but give the actual Render build
logs a look on first deploy in case anything about your specific repo
layout needs a tweak.

## Tested before sending
I ran this for real rather than just writing it blind:
- Started the actual `server.js` and a fleet-status API together and
  confirmed `GET /api/fleet-status` on port 3000 correctly proxies real
  JSON from port 5051.
- Confirmed `index.html` still serves and contains the new tab markup.
- Confirmed the proxy fails *gracefully* (502 + clear message, no crash)
  when `api_server.py` isn't running, and that message surfaces in the
  tab's status line.
- Syntax-checked the modified `server.js` and the modified inline
  `<script>` block in `index.html` with Node directly.

One thing I couldn't test from here: the real FlightRadar24 data feeding
through to the actual rendered cards in a browser (no network path to
flightradar24.com in this environment) — the plumbing is verified, but
give the cards a look once both servers are running for real and let me
know if anything renders oddly.
