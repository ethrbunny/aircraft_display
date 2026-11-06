import requests
import math
from datetime import datetime

LAT = 45.44862010151184
LON = -122.68521259844668
RADIUS = 250

base = "https://api.adsb.lol/v2"

def get(url):
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    return r.json()

def distance(lat, lon):
    rad = math.pi / 180
    a1, a2 = LAT * rad, lat * rad
    da = (lat - LAT) * rad
    dl = (lon - LON) * rad
    h = math.sin(da/2)**2 + math.cos(a1)*math.cos(a2)*math.sin(dl/2)**2
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
matches = []

for a in aircraft:
    hex_id = (a.get("hex") or "").lower()

    try:
        db_military = bool(int(a.get("dbFlags", 0)) & 1)
    except (ValueError, TypeError):
        db_military = False

    if not (db_military or hex_id in mil_hexes):
        continue

    lat = a.get("lat")
    lon = a.get("lon")

    if lat is None or lon is None:
        continue

    d = distance(lat, lon)
    if d > RADIUS:
        continue

    matches.append((d, a))

print("Military registry:", len(mil_hexes))
print("Aircraft in 250 NM point feed:", len(aircraft))
print("Military matches:", len(matches))
print()

if not matches:
    print("No currently visible military aircraft within 250 NM.")
else:
    print("DIST   HEX       CALLSIGN       TYPE    REGISTRATION   ALTITUDE   SPEED")
    print("-" * 88)
    
    for d, a in sorted(matches, key=lambda x: x[0]):
        print(
            f"{d:5.1f}  "
            f"{(a.get('hex') or ''):8} "
            f"{(a.get('flight') or '').strip():13}  "
            f"{(a.get('t') or ''):7} "
            f"{(a.get('r') or ''):14} "
            f"{str(a.get('alt_baro', '')):9} "
            f"{a.get('gs', '')}"
        )
