from astroquery.ipac.irsa import Irsa
from astropy.io import fits
from astropy.time import Time
from PIL import Image, ImageDraw
import numpy as np
import os

ra, dec = 210.80227, 54.34895
band = "SPHEREx-D2"
size = 0.1          # cutout size in degrees
n_frames = 10       # how many dates to use
zoom = 5            # enlarge small cutouts for viewing

os.makedirs("frames", exist_ok=True)

query = f"""
SELECT 'https://irsa.ipac.caltech.edu/' || a.uri ||
       '?center={ra},{dec}d&size={size}' AS url,
       p.time_bounds_lower AS mjd
FROM spherex.artifact a
JOIN spherex.plane p ON a.planeid = p.planeid
WHERE 1 = CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly)
  AND p.energy_bandpassname = '{band}'
ORDER BY p.time_bounds_lower
"""

print("Searching IRSA...")
t = Irsa.query_tap(query).to_table()
print(f"Found {len(t)} images in {band}")

# pick frames spread evenly over the whole time range
idx = np.linspace(0, len(t) - 1, min(n_frames, len(t))).astype(int)

frames = []
for i in idx:
    url, mjd = t["url"][i], t["mjd"][i]
    date = Time(mjd, format="mjd").iso[:10]
    print(f"Downloading {date} ...")
    try:
        with fits.open(url) as hdul:
            data = hdul["IMAGE"].data.astype(float)
    except Exception as e:
        print("  failed:", e)
        continue

    lo, hi = np.nanpercentile(data, [1, 99.5])
    scaled = np.clip((np.nan_to_num(data, nan=lo) - lo) / (hi - lo), 0, 1)
    img = Image.fromarray(np.flipud(scaled * 255).astype(np.uint8))
    img = img.resize((img.width * zoom, img.height * zoom), Image.NEAREST)

    ImageDraw.Draw(img).text((5, 5), date, fill=255)
    img.save(f"frames/{date}_{i}.png")
    frames.append(img)

if frames:
    w, h = frames[0].size
    frames = [f.resize((w, h)) for f in frames]
    frames[0].save("blink.gif", save_all=True, append_images=frames[1:],
                   duration=600, loop=0)
    print(f"\nDone! {len(frames)} frames saved. Open blink.gif")