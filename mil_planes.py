
import requests
import math
from datetime import datetime

LAT = 45.44862010151184
LON = -122.68521259844668
RADIUS = 250

base = "https://api.adsb.lol/v2"

# Aircraft types worth flagging for further investigation.
POTENTIAL_TYPES = {
    "F5",
}

# Known callsign patterns observed in the local feed.
POTENTIAL_CALLSIGNS = (
    "VILLN",
)


def get(url):
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    return r.json()


def distance(lat, lon):
    rad = math.pi / 180
    a1, a2 = LAT * rad, lat * rad
    da = (lat - LAT) * rad
    dl = (lon - LON) * rad

    h = (
        math.sin(da / 2)**2
        + math.cos(a1) * math.cos(a2)
        * math.sin(dl / 2)**2
    )

    return 3440.065 * 2 * math.asin(math.sqrt(h))


print("Checking military aircraft near home")
print("Time:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("Radius:", RADIUS, "nautical miles")
print()

mil_data = get(base + "/mil")
point_data = get(f"{base}/point/{LAT}/{LON}/{RADIUS}")

mil_hexes = {
    a["hex"].lower()
    for a in mil_data.get("ac", [])
    if a.get("hex")
}

aircraft = point_data.get("ac", [])

confirmed = []
potential = []

for a in aircraft:
    hex_id = (a.get("hex") or "").lower()
    aircraft_type = (a.get("t") or "").upper()
    callsign = (a.get("flight") or "").strip().upper()

    try:
        db_military = bool(int(a.get("dbFlags", 0)) & 1)
    except (ValueError, TypeError):
        db_military = False

    registry_match = hex_id in mil_hexes

    lat = a.get("lat")
    lon = a.get("lon")

    if lat is None or lon is None:
        continue

    d = distance(lat, lon)

    if d > RADIUS:
        continue

    # Confirmed by ADS-B.lol military classification
    if db_military or registry_match:
        confirmed.append((d, a))
        continue

    # Potential military aircraft - not confirmed
    type_match = aircraft_type in POTENTIAL_TYPES

    callsign_match = any(
        callsign.startswith(prefix)
        for prefix in POTENTIAL_CALLSIGNS
    )

    if type_match or callsign_match:
        potential.append((d, a))


def print_aircraft(title, entries):
    print()
    print(title)
    print("-" * 88)

    if not entries:
        print("None found.")
        return

    print(
        "DIST   HEX       CALLSIGN       TYPE    "
        "REGISTRATION   ALTITUDE   SPEED"
    )
    print("-" * 88)

    for d, a in sorted(entries, key=lambda x: x[0]):
        print(
            f"{d:5.1f}  "
            f"{(a.get('hex') or ''):8} "
            f"{(a.get('flight') or '').strip():13}  "
            f"{(a.get('t') or ''):7} "
            f"{(a.get('r') or ''):14} "
            f"{str(a.get('alt_baro', '')):9} "
            f"{a.get('gs', '')}"
        )


print("Military registry:", len(mil_hexes))
print("Aircraft in point feed:", len(aircraft))
print("Confirmed military:", len(confirmed))
print("Potential military:", len(potential))

print_aircraft("CONFIRMED MILITARY", confirmed)
print_aircraft("POTENTIAL MILITARY - VERIFY", potential)

print()

