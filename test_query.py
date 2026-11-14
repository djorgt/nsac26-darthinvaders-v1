from astroquery.ipac.irsa import Irsa

ra, dec = 210.80227, 54.34895   # test position from the IRSA tutorial

query = f"""
SELECT TOP 50 a.uri, p.time_bounds_lower, p.energy_bandpassname
FROM spherex.artifact a
JOIN spherex.plane p ON a.planeid = p.planeid
WHERE 1 = CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly)
ORDER BY p.time_bounds_lower
"""

print("Asking IRSA... (can take 30-60 seconds)")
t = Irsa.query_tap(query).to_table()

print(f"\nFound {len(t)} images\n")
print(t)
print("\nBand names:", sorted(set(t["energy_bandpassname"])))