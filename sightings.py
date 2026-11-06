import sqlite3
db = sqlite3.connect(
    "/var/lib/aircraft-display/aircraft_sightings.db"
)
print("\n--- OBSERVATION COUNTS ---")
for row in db.execute("""
    SELECT source, COUNT(*), COUNT(DISTINCT hex)
    FROM api_observations
    WHERE source IN ('point', 'mil')
    GROUP BY source
"""):
    print(f"{row[0]:8} {row[1]:6} records, {row[2]:5} aircraft")
print("\n--- MILITARY CLASSIFICATION ---")
for row in db.execute("""
    SELECT source, military_source, COUNT(*)
    FROM api_observations
    WHERE source IN ('point', 'mil')
    GROUP BY source, military_source
    ORDER BY source, COUNT(*) DESC
"""):
    print(f"{row[0]:8} {row[1]:20} {row[2]}")
print("\n--- RECENT MILITARY OBSERVATIONS ---")
for row in db.execute("""
    SELECT timestamp, source, hex, callsign,
          registration, aircraft_type, altitude, distance_nm
    FROM api_observations
    WHERE military = 1
      AND source IN ('point', 'mil')
    ORDER BY id DESC
    LIMIT 15
"""):
    print(row)
db.close()
