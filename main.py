"""
SPHEREx Sky Change viewer - backend API
Run with:   uvicorn main:app --reload
Then open:  http://localhost:8000        (built-in test viewer)
            http://localhost:8000/docs   (API documentation)
"""

import hashlib
import io
import json
import os
import time

import numpy as np
from astropy.io import fits
from astropy.time import Time
from astroquery.ipac.irsa import Irsa
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from PIL import Image

CACHE = "cache"
os.makedirs(f"{CACHE}/queries", exist_ok=True)
os.makedirs(f"{CACHE}/urls", exist_ok=True)
os.makedirs(f"{CACHE}/data", exist_ok=True)
os.makedirs(f"{CACHE}/png", exist_ok=True)

BANDS = [f"SPHEREx-D{i}" for i in range(1, 7)]

app = FastAPI(title="SPHEREx Sky Change API")

# allow the frontend (any origin) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- helpers ----------

def key_of(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def search_irsa(ra: float, dec: float, band: str, size: float):
    """Return list of (cutout_url, mjd) sorted by time. Cached on disk."""
    qkey = key_of(f"{ra:.5f},{dec:.5f},{band},{size}")
    qfile = f"{CACHE}/queries/{qkey}.json"
    if os.path.exists(qfile):
        with open(qfile) as f:
            return json.load(f)

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
    t = Irsa.query_tap(query).to_table()
    rows = [(str(r["url"]), float(r["mjd"])) for r in t]
    with open(qfile, "w") as f:
        json.dump(rows, f)
    return rows


def load_cutout(fid: str) -> np.ndarray:
    """Download the IMAGE data of a cutout (with retries). Cached as .npy."""
    npy = f"{CACHE}/data/{fid}.npy"
    if os.path.exists(npy):
        return np.load(npy)

    url_file = f"{CACHE}/urls/{fid}.txt"
    if not os.path.exists(url_file):
        raise HTTPException(404, "Unknown frame id - call /frames first")
    with open(url_file) as f:
        url = f.read().strip()

    last_err = None
    for attempt in range(4):
        try:
            with fits.open(url, cache=False) as hdul:
                data = hdul["IMAGE"].data.astype(np.float32)
            np.save(npy, data)
            return data
        except Exception as e:  # IRSA sometimes returns 503 when busy
            last_err = e
            time.sleep(2 + attempt * 2)
    raise HTTPException(502, f"IRSA download failed: {last_err}")


def to_png(data: np.ndarray, stretch: str) -> bytes:
    lo, hi = np.nanpercentile(data, [1, 99.5])
    if hi <= lo:
        hi = lo + 1
    x = np.clip((np.nan_to_num(data, nan=lo) - lo) / (hi - lo), 0, 1)
    if stretch == "asinh":           # shows faint stuff and bright cores together
        x = np.arcsinh(x * 10) / np.arcsinh(10)
    elif stretch == "log":
        x = np.log10(1 + 99 * x) / 2
    img = Image.fromarray(np.flipud(x * 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- API ----------

@app.get("/bands")
def bands():
    return BANDS


@app.get("/frames")
def frames(
    ra: float = Query(..., description="Right ascension in degrees"),
    dec: float = Query(..., description="Declination in degrees"),
    band: str = Query("SPHEREx-D2"),
    size: float = Query(0.1, gt=0, le=0.5, description="Cutout size in degrees"),
    n: int = Query(10, ge=2, le=60, description="Max number of frames"),
):
    """List SPHEREx frames covering (ra, dec), spread evenly over time."""
    if band not in BANDS:
        raise HTTPException(400, f"band must be one of {BANDS}")
    try:
        rows = search_irsa(ra, dec, band, size)
    except Exception as e:
        raise HTTPException(502, f"IRSA search failed: {e}")

    if not rows:
        return {"count_total": 0, "frames": []}

    idx = np.unique(np.linspace(0, len(rows) - 1, min(n, len(rows))).astype(int))
    out = []
    for i in idx:
        url, mjd = rows[i]
        fid = key_of(url)
        with open(f"{CACHE}/urls/{fid}.txt", "w") as f:
            f.write(url)
        out.append({
            "id": fid,
            "mjd": mjd,
            "date": Time(mjd, format="mjd").iso[:10],
            "png": f"/frame/{fid}.png",
        })
    return {"count_total": len(rows), "frames": out}


@app.get("/frame/{fid}.png")
def frame_png(fid: str, stretch: str = Query("asinh", pattern="^(linear|asinh|log)$")):
    """One cutout as a PNG image."""
    png_file = f"{CACHE}/png/{fid}_{stretch}.png"
    if os.path.exists(png_file):
        with open(png_file, "rb") as f:
            return Response(f.read(), media_type="image/png")
    png = to_png(load_cutout(fid), stretch)
    with open(png_file, "wb") as f:
        f.write(png)
    return Response(png, media_type="image/png")


# ---------- tiny built-in test viewer (the real frontend is separate) ----------

TEST_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>SPHEREx blink test</title>
<style>
 body{font-family:sans-serif;background:#111;color:#eee;padding:20px}
 input,select,button{padding:6px;margin:3px}
 #img{width:400px;height:400px;image-rendering:pixelated;background:#000;display:block;margin-top:10px}
</style></head><body>
<h2>SPHEREx blink test</h2>
RA <input id="ra" value="210.80227" size="10">
Dec <input id="dec" value="54.34895" size="10">
<select id="band"></select>
<select id="stretch"><option>asinh</option><option>linear</option><option>log</option></select>
<button onclick="load()">Load</button>
<button onclick="playing=!playing">Play/Pause</button>
<div id="msg"></div>
<img id="img">
<input type="range" id="slider" min="0" value="0" style="width:400px" oninput="show(+this.value)">
<script>
let frames=[], i=0, playing=true;
fetch('/bands').then(r=>r.json()).then(b=>{
  band.innerHTML=b.map(x=>`<option>${x}</option>`).join(''); band.value='SPHEREx-D2';});
async function load(){
  msg.textContent='Searching IRSA... (first time can take a minute)';
  const r=await fetch(`/frames?ra=${ra.value}&dec=${dec.value}&band=${band.value}`);
  const j=await r.json();
  if(!r.ok){msg.textContent='Error: '+(j.detail||r.status);return;}
  frames=j.frames; slider.max=frames.length-1;
  msg.textContent=`${j.count_total} images found, showing ${frames.length}. Loading images...`;
  // preload all frames so blinking is smooth
  await Promise.all(frames.map(f=>new Promise(res=>{const im=new Image();
    im.onload=im.onerror=res; im.src=f.png+'?stretch='+stretch.value;})));
  msg.textContent=`${j.count_total} images found, showing ${frames.length}.`;
  show(0);
}
function show(k){ if(!frames.length)return; i=k; slider.value=k;
  img.src=frames[k].png+'?stretch='+stretch.value;
  msg.textContent=msg.textContent.split(' | ')[0]+' | '+frames[k].date; }
setInterval(()=>{ if(playing&&frames.length) show((i+1)%frames.length); },600);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return TEST_PAGE
