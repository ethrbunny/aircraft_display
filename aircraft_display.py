
#!/usr/bin/env python3

import time
import sqlite3
import requests
import json
import traceback
import math

from datetime import datetime
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics


# --------------------------------------------------
# Location and API
# --------------------------------------------------

LAT = 45.44862010151184
LON = -122.68521259844668
RADIUS_NM = 10

API_URL = (
    f"https://api.adsb.lol/v2/point/"
    f"{LAT}/{LON}/{RADIUS_NM}"
)

MIL_API_URL = "https://api.adsb.lol/v2/mil"

REFRESH_SECONDS = 10
MIL_REFRESH_SECONDS = 60
PAGE_SECONDS = 6

DB_FILE = "/var/lib/aircraft-display/aircraft_sightings.db"

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

def aircraft_name(a):
    code = a.get("t") or "N/A"
    return AIRCRAFT_TYPES.get(code, code)

# --------------------------------------------------
# Database
# --------------------------------------------------

db = sqlite3.connect(DB_FILE)

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

military_hexes = set()
last_mil_refresh = 0

def climb_text(a):
    vr = a.get("baro_rate")

    if not isinstance(vr, (int, float)):
        return "V/S ---"

    if vr > 0:
        return f"UP {int(vr):+d}"

    if vr < 0:
        return f"DN {int(vr):+d}"

    return "LEVEL"

def get_military_hexes():

    global military_hexes, last_mil_refresh

    now = time.monotonic()

    if now - last_mil_refresh < MIL_REFRESH_SECONDS:
        return military_hexes

    last_mil_refresh = now

    try:
        response = requests.get(
            MIL_API_URL,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()
        military_aircraft = data.get("ac", [])

        military_hexes = {
            a.get("hex", "").lower()
            for a in military_aircraft
            if a.get("hex")
        }

        # /v2/mil is worldwide; store only positioned aircraft in local range.
        log_api_observations(
            "mil",
            military_aircraft,
            military_hexes=military_hexes,
            nearby_only=True
        )

        print(
            "Military database:",
            len(military_hexes),
            "aircraft; local observations logged"
        )

    except Exception as e:
        print("Military API error:", e)

    return military_hexes


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
options.chain_length = 1
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

def get_aircraft():
    response = requests.get(
        API_URL,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()
    raw_aircraft = data.get("ac", [])

    # Military database is cached separately.
    mil_hexes = get_military_hexes()

    # Log the complete local response before any display filters are applied.
    log_api_observations(
        "point",
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

    alt = a.get(
        "alt_baro",
        a.get("alt_geom")
    )

    if alt == "ground":
        return "GROUND"

    if isinstance(alt, (int, float)):
        return f"FL{int(alt // 100):03d}"

    return "ALT ---"


def bearing_text(a):

    bearing = a.get("dir")

    if not isinstance(bearing, (int, float)):
        return "---"

    directions = [
        "N", "NE", "E", "SE",
        "S", "SW", "W", "NW"
    ]

    return directions[
        int((bearing + 22.5) / 45) % 8
    ]


# --------------------------------------------------
# Aircraft display
# --------------------------------------------------
def draw_aircraft(a, y):

    military = a.get("military", False)

    callsign = (a.get("flight") or "").strip()

    callsign = (
        callsign
        or a.get("r")
        or a.get("hex", "UNKNOWN")
    )

    aircraft_type = a.get("t") or "UNKNOWN"
    distance = a.get("dst", 0)
    speed = a.get("gs")

    callsign_color, category_label = aircraft_style(a)

    draw_text(1, y, callsign_color, callsign[:8])

    if category_label:
        draw_text(46, y, callsign_color, category_label)

    # Aircraft type
    draw_text(
        1, y + 8, CYAN,
        aircraft_name(a)[:8]
    )

    # Altitude
    draw_text(
        37, y + 8, WHITE,
        altitude_text(a)
    )

    # Distance
    draw_text(
        1, y + 16, YELLOW,
        f"{distance:.1f}NM"
    )

    # Ground speed
    speed_text = (
        f"{int(speed)}KT"
        if isinstance(speed, (int, float))
        else "---KT"
    )

    draw_text(30, y + 16, YELLOW, speed_text)

    # Climb / descent rate
    draw_text(
        1, y + 24, GREEN,
        climb_text(a)
    )

    # Heading
    draw_text(
        48, y + 24, WHITE,
        bearing_text(a)
    )
# --------------------------------------------------
# Screen
# --------------------------------------------------

def draw_screen(aircraft, page, error=False):

    canvas.Clear()

    count = len(aircraft)

    mil_count = sum(
        1 for a in aircraft
        if a.get("military")
    )

    #draw_text(
    #    1, 7, RED,
    #    f"MIL {mil_count:02d}"
    #)

    #draw_text(
    #    53, 7, WHITE,
    #    f"{count:02d}"[-2:]
    #)

    if error:

        draw_text(1, 30, RED, "API ERROR")
        draw_text(1, 42, GREY, "RETRYING")

    elif count == 0:

        draw_text(1, 29, YELLOW, "NO AIRCRAFT")
        draw_text(1, 41, GREY, "IN 10 NM")

    else:

        start = page * 2
        visible = aircraft[start:start + 2]

        if not visible:
            visible = aircraft[:2]

        draw_aircraft(visible[0], 8)

        if len(visible) > 1:
            draw_aircraft(visible[1], 39)

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

    api_error = False

    print("Aircraft display starting")
    print("Coverage:", RADIUS_NM, "NM")
    print("Location:", LAT, LON)
    print("Sightings database:", DB_FILE)
    print("Raw API observation logging: enabled")
    print("Point feed: all returned records")
    print("Military feed: positioned aircraft within", RADIUS_NM, "NM")

    while True:

        now = time.monotonic()

        if now >= next_refresh:

            try:
                aircraft = get_aircraft()

                api_error = False
                page = 0

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

        pages = max(
            1,
            (len(aircraft) + 1) // 2
        )

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
