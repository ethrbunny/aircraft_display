
#!/usr/bin/env python3

import time
import sqlite3
import requests
import json
import traceback
import math
import os

from datetime import datetime,timedelta
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

# --------------------------------------------------
# Location and API
# --------------------------------------------------

LAT = 45.44862010151184
LON = -122.68521259844668
RADIUS_NM = 10

# Rotate between independent community networks. Both nearby-aircraft
# endpoints return ADSBexchange-compatible aircraft records.
API_PROVIDERS = [
    {
        "name": "adsb.lol",
        "point_url": (
            f"https://api.adsb.lol/v2/point/{LAT}/{LON}/{RADIUS_NM}"
        ),
        "mil_url": "https://api.adsb.lol/v2/mil",
    },
    {
        "name": "adsb.fi",
        "point_url": (
            f"https://opendata.adsb.fi/api/v3/lat/{LAT}/lon/{LON}/dist/{RADIUS_NM}"
        ),
        "mil_url": "https://opendata.adsb.fi/api/v2/mil",
    },
]

REFRESH_SECONDS = 10
# Each provider is used on alternate refreshes, so each receives a point
# request about every 20 seconds. Refresh each provider's military list no
# more than once per minute.
MIL_REFRESH_SECONDS = 60
PAGE_SECONDS = 6

DB_FILE = "/var/lib/aircraft-display/aircraft_sightings.db"
# Keep aircraft.json beside this script (for example, /root/aircraft.json).
AIRCRAFT_REGISTRY_FILE = "/var/lib/aircraft-display/aircraft.json"
AIRCRAFT_REGISTRY = {}

AIRCRAFT_TYPES = {
    "F15": "F-15",
    "F16": "F-16",
    "A10": "A-10",
    "C172": "Cess172",
    "C152": "C152",
    "B738": "B737-800",
    "B744": "B747-400",
    "B77W": "B777-300",
    "A320": "A320",
    "H60": "Blkhawk",
    "H47": "Chinook",
    "E75L": "E175",
    "RV7": "RV-7",
    "C182": "Cess Skylane",
    "RV10": "RV-10",
    "RV12": "RV-12",
    "S22T": "Cirrus SR22T",
}

REFRESH_SECONDS = 10
MAX_RETRY_SECONDS = 120
OBSERVATION_LOG_INTERVAL = 30  # seconds per aircraft and API source

def load_aircraft_registry(path=AIRCRAFT_REGISTRY_FILE):
    """Load the JSON-array aircraft registry."""
    registry = {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)

        for record in records:
            if not isinstance(record, dict):
                continue

            hex_id = str(record.get("icao24") or "").strip().lower()
            if hex_id:
                registry[hex_id] = record

    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: could not load aircraft registry {path}: {exc}")

    print(f"Aircraft registry: loaded {len(registry):,} records from {path}")
    return registry

def enrich_aircraft_from_registry(a):
    """Fill missing ADS-B registration/type details using ICAO hex."""
    hex_id = str(a.get("hex") or "").strip().lower()
    record = AIRCRAFT_REGISTRY.get(hex_id)
    if not record:
        return

    if not (a.get("r") or "").strip():
        registration = record.get("registration")
        if registration:
            a["r"] = registration

    # The registry's operator field contains the descriptive aircraft model
    # (e.g. 'CESSNA 182 Skylane'); its model field is often just '00'.
    if record.get("operator"):
        a["_registry_operator"] = record["operator"]
    if not a.get("t") and record.get("typecode"):
        a["t"] = record["typecode"]


def aircraft_name(a):
    model = a.get("_registry_operator")
    if model:
        return model
    code = a.get("t") or "N/A"
    return AIRCRAFT_TYPES.get(code, code)

# --------------------------------------------------
# Database
# --------------------------------------------------

db = sqlite3.connect(DB_FILE, timeout=10)
db.execute("PRAGMA busy_timeout = 10000")

db.execute("""
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    hex TEXT,
    callsign TEXT,
    registration TEXT,
    aircraft_type TEXT,
    altitude TEXT,
    speed REAL,
    distance REAL
)
""")

db.execute("""
CREATE INDEX IF NOT EXISTS idx_sightings_hex
ON sightings(hex)
""")

# Raw API observations are separate from display candidates.
db.execute("""
CREATE TABLE IF NOT EXISTS api_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    hex TEXT,
    callsign TEXT,
    registration TEXT,
    aircraft_type TEXT,
    latitude REAL,
    longitude REAL,
    altitude TEXT,
    speed REAL,
    distance_nm REAL,
    seen_position REAL,
    db_flags TEXT,
    military_listed INTEGER,
    military INTEGER,
    military_source TEXT,
    raw_json TEXT
)
""")
db.execute("""
CREATE INDEX IF NOT EXISTS idx_api_observations_time
ON api_observations(timestamp)
""")
db.execute("""
CREATE INDEX IF NOT EXISTS idx_api_observations_hex
ON api_observations(hex)
""")
db.execute("""
CREATE INDEX IF NOT EXISTS idx_api_observations_source_time
ON api_observations(source, timestamp)
""")

db.commit()

# Prevent repeated entries for the same aircraft
# within a two-minute window.

last_logged = {}
last_observation_logged = {}


def _number(value):
    """Return a finite float, or None for missing/non-numeric values."""
    try:
        if value is None or value == "ground":
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def distance_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    lat1, lon1, lat2, lon2 = map(
        math.radians, (lat1, lon1, lat2, lon2)
    )
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(h)))

_last_retention_cleanup = None
_last_retention_attempt = None


def purge_old_non_mil_observations():
    """
    Delete non-military raw observations older than seven days.

    Use the application's existing SQLite connection. This function must only
    be called after any surrounding `with db:` logging transaction has exited.
    """
    global _last_retention_cleanup, _last_retention_attempt

    now = datetime.now()

    if (
        _last_retention_cleanup is not None
        and (now - _last_retention_cleanup).total_seconds() < 86400
    ):
        return

    if (
        _last_retention_attempt is not None
        and (now - _last_retention_attempt).total_seconds() < 300
    ):
        return

    _last_retention_attempt = now
    cutoff = (now - timedelta(days=7)).isoformat(timespec="seconds")

    try:
        # Reuse the main connection rather than opening a second connection.
        with db:
            cursor = db.execute(
                """
                DELETE FROM api_observations
                WHERE military = 0
                  AND timestamp < ?
                """,
                (cutoff,),
            )
            deleted = cursor.rowcount

        _last_retention_cleanup = datetime.now()

        if deleted:
            print(
                f"Database cleanup: deleted {deleted} "
                "non-military observations older than 7 days"
            )
        else:
            print("Database cleanup: nothing older than 7 days to delete")

    except sqlite3.OperationalError as e:
        print(f"Database cleanup temporarily unavailable: {e}")

    except Exception as e:
        print(f"Database cleanup failed: {e}")

def log_api_observations(source, aircraft, military_hexes=None,
                         nearby_only=False):
    """
    Store API observations independently of display filtering.

    Point feed: store every returned aircraft, including stale/incomplete.
    Military feed: store positioned records within the local coverage radius;
    /v2/mil is a worldwide feed and should not be copied wholesale.
    """
    now = time.time()
    military_hexes = military_hexes or set()
    inserted = 0

    with db:
        for a in aircraft:
            hex_id = (a.get("hex") or "").lower() or None
            lat = _number(a.get("lat"))
            lon = _number(a.get("lon"))

            if nearby_only:
                if lat is None or lon is None:
                    continue
                dist = distance_nm(LAT, LON, lat, lon)
                if dist > RADIUS_NM:
                    continue
            else:
                dist = _number(a.get("dst"))

            key_hex = hex_id or (
                "nohex:" + str(a.get("flight") or a.get("r") or "unknown")
            )
            cache_key = (source, key_hex)
            if now - last_observation_logged.get(cache_key, 0) < OBSERVATION_LOG_INTERVAL:
                continue

            db_flags = a.get("dbFlags")
            try:
                db_military = bool(int(db_flags or 0) & 1)
            except (TypeError, ValueError):
                db_military = False

            listed_military = bool(hex_id and hex_id in military_hexes)
            if source == "mil":
                is_military = True
                military_source = "mil_endpoint"
            else:
                is_military = db_military or listed_military
                if db_military and listed_military:
                    military_source = "dbFlags+mil_list"
                elif db_military:
                    military_source = "dbFlags"
                elif listed_military:
                    military_source = "mil_list"
                else:
                    military_source = "not_marked"

            altitude = a.get("alt_baro", a.get("alt_geom"))
            seen = _number(a.get("seen_pos"))

            db.execute("""
                INSERT INTO api_observations (
                    timestamp, source, hex, callsign, registration,
                    aircraft_type, latitude, longitude, altitude, speed,
                    distance_nm, seen_position, db_flags, military_listed,
                    military, military_source, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(timespec="seconds"),
                source,
                hex_id,
                (a.get("flight") or "").strip(),
                a.get("r"),
                a.get("t"),
                lat,
                lon,
                str(altitude) if altitude is not None else None,
                _number(a.get("gs")),
                dist,
                seen,
                str(db_flags) if db_flags is not None else None,
                int(listed_military),
                int(is_military),
                military_source,
                json.dumps(a, separators=(",", ":"), sort_keys=True)
            ))

            last_observation_logged[cache_key] = now
            inserted += 1

    # Run only after the logging transaction has committed. Calling cleanup
    # inside `with db:` was causing SQLite to lock against its own transaction.
    purge_old_non_mil_observations()

    for key in list(last_observation_logged):
        if now - last_observation_logged[key] > 3600:
            del last_observation_logged[key]

    if inserted:
        print(f"API observations: source={source}, inserted={inserted}")


def log_sightings(aircraft):

    now = time.time()

    with db:
        for a in aircraft:

            if not a.get("military"):
                continue

            hex_id = a.get("hex", "").lower()

            if not hex_id:
                continue

            if now - last_logged.get(hex_id, 0) < 120:
                continue

            callsign = (a.get("flight") or "").strip()

            altitude = a.get(
                "alt_baro",
                a.get("alt_geom")
            )

            speed = a.get("gs")

            db.execute("""
                INSERT INTO sightings (
                    timestamp,
                    hex,
                    callsign,
                    registration,
                    aircraft_type,
                    altitude,
                    speed,
                    distance
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(timespec="seconds"),
                hex_id,
                callsign,
                a.get("r"),
                a.get("t"),
                str(altitude),
                speed,
                a.get("dst")
            ))

            last_logged[hex_id] = now

            print(
                "MILITARY SIGHTING:",
                hex_id,
                callsign,
                a.get("t")
            )

    # Clean old entries from the in-memory cache
    for hex_id in list(last_logged):
        if now - last_logged[hex_id] > 3600:
            del last_logged[hex_id]


# --------------------------------------------------
# Military aircraft lookup
# --------------------------------------------------

military_hexes_by_provider = {}
last_mil_refresh_by_provider = {}

def climb_text(a):
    vr = a.get("baro_rate")

    if not isinstance(vr, (int, float)):
        return "V/S ---"

    if vr > 0:
        return f"UP {int(vr):+d}"

    if vr < 0:
        return f"DN {int(vr):+d}"

    return "LEVEL"

def get_military_hexes(provider):
    """Get and cache the military list independently for each provider."""
    name = provider["name"]
    cached = military_hexes_by_provider.get(name, set())
    now = time.monotonic()

    if now - last_mil_refresh_by_provider.get(name, 0) < MIL_REFRESH_SECONDS:
        return cached

    last_mil_refresh_by_provider[name] = now

    try:
        response = requests.get(
            provider["mil_url"],
            timeout=15,
            headers={"User-Agent": "HomeAircraftDisplay/1.0"}
        )
        response.raise_for_status()

        data = response.json()
        military_aircraft = data.get("ac", [])
        cached = {
            a.get("hex", "").lower()
            for a in military_aircraft
            if a.get("hex")
        }
        military_hexes_by_provider[name] = cached

        # /mil is worldwide; store only positioned aircraft in local range.
        log_api_observations(
            f"mil:{name}",
            military_aircraft,
            military_hexes=cached,
            nearby_only=True
        )

        print(
            f"Military database ({name}):",
            len(cached),
            "aircraft; local observations logged"
        )

    except Exception as e:
        # Keep the last successful cache if a provider is temporarily down.
        print(f"Military API error ({name}):", e)

    return cached


# --------------------------------------------------
# Font
# --------------------------------------------------

font = graphics.Font()
font.LoadFont("/root/rpi-rgb-led-matrix/fonts/5x8.bdf")


# --------------------------------------------------
# Matrix configuration
# --------------------------------------------------

options = RGBMatrixOptions()
options.rows = 64
options.cols = 64
options.chain_length = 2
options.parallel = 1
options.hardware_mapping = "adafruit-hat"
options.gpio_slowdown = 2
options.brightness = 60
options.pwm_bits = 8

matrix = RGBMatrix(options=options)
canvas = matrix.CreateFrameCanvas()


# --------------------------------------------------
# Colors
# --------------------------------------------------

WHITE = graphics.Color(255, 255, 255)
GREEN = graphics.Color(0, 255, 80)
CYAN = graphics.Color(0, 200, 255)
YELLOW = graphics.Color(255, 220, 0)
RED = graphics.Color(255, 50, 50)
GREY = graphics.Color(130, 130, 130)
PURPLE = graphics.Color(180, 0, 255)
ORANGE = graphics.Color(255, 120, 0)
BLUE = graphics.Color(50, 100, 255)

def draw_text(x, y, color, text):

    graphics.DrawText(
        canvas, font, x, y, color, str(text)
    )

def aircraft_style(a):

    # Military always takes priority
    if a.get("military"):
        return RED, "MIL"

    category = (a.get("category") or "").upper()

    styles = {
        "B1": (YELLOW, "GLD"),   # Glider
        "B2": (CYAN, "BAL"),     # Balloon / blimp
        "B4": (ORANGE, "ULT"),   # Ultralight
        "B6": (PURPLE, "UAV"),   # Unmanned aircraft
        "A7": (BLUE, "ROT"),     # Rotorcraft
    }

    return styles.get(category, (GREEN, ""))

# --------------------------------------------------
# Aircraft retrieval
# --------------------------------------------------

def get_aircraft(provider):
    name = provider["name"]
    response = requests.get(
        provider["point_url"],
        timeout=15,
        headers={"User-Agent": "HomeAircraftDisplay/1.0"}
    )

    response.raise_for_status()

    data = response.json()
    raw_aircraft = data.get("ac", [])

    # Military database is cached separately for each provider.
    mil_hexes = get_military_hexes(provider)

    # Log the complete local response before any display filters are applied.
    log_api_observations(
        f"point:{name}",
        raw_aircraft,
        military_hexes=mil_hexes,
        nearby_only=False
    )

    aircraft = []

    for a in raw_aircraft:
        # Display filtering starts here; raw observations above are retained.
        if a.get("alt_baro") == "ground":
            continue

        if a.get("gs") == 0:
            continue

        if a.get("lat") is None or a.get("lon") is None:
            continue

        if a.get("seen_pos", 0) > 60:
            continue

        distance = a.get("dst")

        if distance is None or distance > RADIUS_NM:
            continue

        hex_id = (a.get("hex") or "").lower()
        enrich_aircraft_from_registry(a)

        # dbFlags bit 0 indicates military.
        db_flags = a.get("dbFlags", 0)

        try:
            db_military = bool(int(db_flags) & 1)
        except (TypeError, ValueError):
            db_military = False

        a["military"] = (
            db_military or hex_id in mil_hexes
        )

        aircraft.append(a)

    aircraft.sort(
        key=lambda a: (
            not a.get("military", False),
            a.get("dst", 999)
        )
    )

    return aircraft


# --------------------------------------------------
# Display helpers
# --------------------------------------------------

def altitude_text(a):
    alt = a.get("alt_baro", a.get("alt_geom"))

    if alt == "ground":
        return "ON GROUND"

    if isinstance(alt, (int, float)):
        return f"ALT {int(alt)}"

    return "ALT ---"


def bearing_text(a):
    # ADSB.lol point responses normally use `track`; retain `dir` fallback.
    bearing = a.get("track", a.get("dir"))

    if not isinstance(bearing, (int, float)):
        return "---"

    directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return directions[int((bearing + 22.5) / 45) % 8]


def aircraft_identity(a):
    return (
        (a.get("flight") or "").strip()
        or a.get("r")
        or a.get("hex")
        or "UNKNOWN"
    )


# --------------------------------------------------
# Recent military sightings cache
# --------------------------------------------------

_recent_military_cache = []
_last_recent_military_query = 0


def get_recent_military_sightings():
    """Return military sightings from the last 24 hours, newest first."""
    global _recent_military_cache, _last_recent_military_query

    now = time.monotonic()
    if now - _last_recent_military_query < 15:
        return _recent_military_cache

    cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")

    try:
        rows = db.execute(
            """
            SELECT timestamp, hex, callsign, registration,
                   aircraft_type, altitude, speed, distance
            FROM sightings
            WHERE timestamp >= ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (cutoff,),
        ).fetchall()

        _recent_military_cache = [
            {
                "timestamp": row[0],
                "hex": row[1],
                "callsign": row[2],
                "registration": row[3],
                "aircraft_type": row[4],
                "altitude": row[5],
                "speed": row[6],
                "distance": row[7],
            }
            for row in rows
        ]
        _last_recent_military_query = now

    except sqlite3.Error as e:
        print(f"Recent military sightings query failed: {e}")

    return _recent_military_cache


# --------------------------------------------------
# Aircraft display: 128x64 split into two 64x64 panels
# --------------------------------------------------

def draw_local_panel(aircraft, page, error=False):
    """Show local aircraft; retain the last good data during feed outages."""
    if error and aircraft:
        # Keep showing the last successful response and make its age/status
        # obvious rather than replacing useful aircraft data with an error.
        draw_text(1, 7, RED, "STALE DATA")
    else:
        draw_text(1, 7, CYAN, "LOCAL 10NM")

    if error and not aircraft:
        draw_text(1, 27, RED, "API ERROR")
        draw_text(1, 39, GREY, "RETRYING")
        return

    if not aircraft:
        draw_text(1, 27, YELLOW, "NO AIRCRAFT")
        draw_text(1, 39, GREY, "IN 10 NM")
        return

    a = aircraft[page % len(aircraft)]
    color, _ = aircraft_style(a)

    draw_text(1, 18, color, aircraft_identity(a)[:10])
    draw_text(1, 25, CYAN, aircraft_name(a)[:10])
    draw_text(1, 32, WHITE, altitude_text(a)[:10])

    distance = _number(a.get("dst"))
    distance_text = f"DST {distance:.1f}NM" if distance is not None else "DST ---"
    draw_text(1, 40, YELLOW, distance_text[:10])

    speed = _number(a.get("gs"))
    speed_text = f"{int(speed)}KT" if speed is not None else "---KT"
    motion_text = f"{speed_text} {bearing_text(a)}"
    draw_text(1, 48, GREEN, motion_text[:10])

    # Climb / descent rate
    draw_text(
        1, 56, ORANGE,
        climb_text(a)
    )

def _short_altitude(altitude):
    if altitude is None:
        return "---"

    try:
        if str(altitude).lower() == "ground":
            return "GND"
        # Hundreds of feet keeps the value readable in the narrow panel.
        return f"{int(float(altitude)) // 100:03d}"
    except (TypeError, ValueError):
        return "---"


def draw_military_panel(aircraft):
    """Show current military aircraft followed by recent logged sightings."""
    current_military = [
        a for a in aircraft
        if a.get("military")
    ]

    draw_text(66, 7, RED, "MILITARY")
    draw_text(114, 7, YELLOW, f"{len(current_military):02d}"[-2:])

    entries = []
    seen_hexes = set()

    # Current aircraft take priority and are already sorted by distance.
    for a in current_military:
        hex_id = (a.get("hex") or "").lower()
        if hex_id:
            seen_hexes.add(hex_id)

        entries.append({
            "identity": aircraft_identity(a),
            "altitude": a.get("alt_baro", a.get("alt_geom")),
            "current": True,
        })

    # Fill remaining rows with recent military sightings from SQLite.
    for sighting in get_recent_military_sightings():
        hex_id = (sighting.get("hex") or "").lower()
        if hex_id and hex_id in seen_hexes:
            continue

        if hex_id:
            seen_hexes.add(hex_id)

        entries.append({
            "identity": (
                (sighting.get("callsign") or "").strip()
                or sighting.get("registration")
                or sighting.get("hex")
                or "UNKNOWN"
            ),
            "altitude": sighting.get("altitude"),
            "current": False,
        })

        if len(entries) >= 5:
            break

    if not entries:
        draw_text(66, 27, GREY, "NO MIL")
        draw_text(66, 38, GREY, "SIGHTINGS")
        draw_text(66, 51, GREY, "LAST 24H")
        return

    for index, entry in enumerate(entries[:5]):
        y = 18 + index * 10
        color = RED if entry["current"] else GREY
        identity = str(entry["identity"])[:6].ljust(6)
        altitude = _short_altitude(entry["altitude"])
        # Ten characters fit within the 64-pixel panel using the 5x8 font.
        draw_text(66, y, color, f"{identity} {altitude}")


def draw_screen(aircraft, page, error=False):
    canvas.Clear()

    # Subtle divider between the two physical 64x64 panels.
    for y in range(64):
        canvas.SetPixel(63, y, 55, 55, 55)

    draw_local_panel(aircraft, page, error)
    draw_military_panel(aircraft)
    # Visible provider attribution (adsb.fi requests its name and homepage link).
    draw_text(66, 63, GREY, "ADSB.FI / LOL")

    return matrix.SwapOnVSync(canvas)


# --------------------------------------------------
# Main loop
# --------------------------------------------------

def main():

    aircraft = []

    page = 0
    last_refresh = 0
    last_page = 0

    retry_delay = REFRESH_SECONDS
    next_refresh = 0
    provider_index = 0

    api_error = False

    global AIRCRAFT_REGISTRY
    AIRCRAFT_REGISTRY = load_aircraft_registry()

    print("Aircraft display starting")
    print("Coverage:", RADIUS_NM, "NM")
    print("Location:", LAT, LON)
    print("Sightings database:", DB_FILE)
    print("Raw API observation logging: enabled")
    print("Rotating providers:", ", ".join(p["name"] for p in API_PROVIDERS))
    print("Provider attribution: https://adsb.fi/ and https://adsb.lol/")
    print("Point feed: all returned records")
    print("Military feed: positioned aircraft within", RADIUS_NM, "NM")
    print("Matrix: 128x64 (two chained 64x64 panels)")
    print("Retention: non-military raw observations older than 7 days")
    purge_old_non_mil_observations()

    while True:

        now = time.monotonic()

        if now >= next_refresh:

            provider = API_PROVIDERS[provider_index % len(API_PROVIDERS)]
            provider_index += 1

            try:
                print(time.strftime("%H:%M:%S"), "Polling", provider["name"])
                aircraft = get_aircraft(provider)

                api_error = False
                page = 0
                last_page = time.monotonic()

                log_sightings(aircraft)

                mil_count = sum(
                    1 for a in aircraft
                    if a.get("military")
                )

#        print(
#            time.strftime("%H:%M:%S"),
#            "Aircraft:", len(aircraft),
#            "Military:", mil_count
#        )

               # Success: reset backoff
                retry_delay = REFRESH_SECONDS
                next_refresh = time.monotonic() + REFRESH_SECONDS

            except Exception as e:

                api_error = True
                traceback.print_exc()

                # Respect Retry-After if supplied
                response = getattr(e, "response", None)
                retry_after = None

                if response is not None:
                    try:
                        retry_after = int(
                            response.headers.get("Retry-After")
                        )
                    except (TypeError, ValueError):
                        pass

                wait = max(retry_delay, retry_after or 0)

                print(
                    time.strftime("%H:%M:%S"),
                    "API error:", e,
                    "- retrying in", wait, "seconds"
                 )

                next_refresh = time.monotonic() + wait

                retry_delay = min(
                    retry_delay * 2,
                    MAX_RETRY_SECONDS
                )

        last_refresh = now

        if now - last_page >= PAGE_SECONDS:

            page += 1
            last_page = now

        pages = max(1, len(aircraft))

        page %= pages

        draw_screen(
            aircraft,
            page,
            api_error
        )

        time.sleep(0.5)


# --------------------------------------------------
# Startup
# --------------------------------------------------

if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        print("\nStopping aircraft display")

    finally:
        matrix.Clear()
        db.close()
