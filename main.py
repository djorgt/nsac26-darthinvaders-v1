import hashlib
import io
import json
import math
import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS, FITSFixedWarning
from astroquery.ipac.irsa import Irsa
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from PIL import Image

warnings.simplefilter("ignore", FITSFixedWarning)

CACHE = "cache"
for sub in ["queries", "epochs", "raw", "stack", "gaia"]:
    os.makedirs(f"{CACHE}/{sub}", exist_ok=True)

BANDS = [f"SPHEREx-D{i}" for i in range(1, 7)]
PIXSCALE = 6.2          
CUTOUT_MARGIN = 1.5    
MJD_J2016 = 57388.0     

app = FastAPI(title="SPHEREx Sky Change API", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
pool = ThreadPoolExecutor(max_workers=6)

def key_of(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def mjd_to_date(mjd: float) -> str:
    return Time(mjd, format="mjd").iso[:10]


def make_grid(ra: float, dec: float, size: float):
    """A north-up, east-left TAN grid centred on (ra, dec)."""
    npix = max(16, int(round(size * 3600 / PIXSCALE)))
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(npix + 1) / 2, (npix + 1) / 2]
    w.wcs.cdelt = [-PIXSCALE / 3600, PIXSCALE / 3600]
    return w, npix


def shifted_center(ra, dec, pmra, pmdec, mjd, t0):
    """Move the grid with a star's proper motion (mas/yr) so the star stays still."""
    if not (pmra or pmdec) or t0 is None:
        return ra, dec
    dt = (mjd - t0) / 365.25
    dec2 = dec + pmdec * dt / 3.6e6
    ra2 = ra + pmra * dt / 3.6e6 / max(math.cos(math.radians(dec)), 1e-6)
    return ra2, dec2

def search_irsa(ra: float, dec: float, band: str, size: float):
    """All SPHEREx images covering (ra, dec) in one band: list of (cutout_url, mjd)."""
    cut = round(size * CUTOUT_MARGIN, 4)
    qfile = f"{CACHE}/queries/{key_of(f'{ra:.5f},{dec:.5f},{band},{cut}')}.json"
    if os.path.exists(qfile):
        with open(qfile) as f:
            return json.load(f)

    query = f"""
    SELECT 'https://irsa.ipac.caltech.edu/' || a.uri ||
           '?center={ra},{dec}d&size={cut}' AS url,
           p.time_bounds_lower AS mjd
    FROM spherex.artifact a
    JOIN spherex.plane p ON a.planeid = p.planeid
    WHERE 1 = CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly)
      AND p.energy_bandpassname = '{band}'
    ORDER BY p.time_bounds_lower
    """
    t, last = None, None
    for attempt, use_async in enumerate([False, True, False, True]):
        try:
            t = Irsa.query_tap(query, async_job=use_async).to_table()
            break
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    if t is None:
        raise RuntimeError(f"NASA's IRSA archive is busy or down right now ({last})")
    rows = [(str(r["url"]), float(r["mjd"])) for r in t if np.isfinite(float(r["mjd"]))]
    with open(qfile, "w") as f:
        json.dump(rows, f)
    return rows


def group_epochs(rows, window_days: float, per_epoch: int):
    """Group images taken within `window_days` of each other (WiseView 'window')."""
    groups, cur = [], []
    for url, mjd in rows:
        if cur and mjd - cur[0][1] > window_days:
            groups.append(cur)
            cur = []
        cur.append((url, mjd))
    if cur:
        groups.append(cur)

    epochs = []
    for g in groups:
        # keep at most per_epoch images, spread over the group
        idx = np.unique(np.linspace(0, len(g) - 1, min(per_epoch, len(g))).astype(int))
        members = [g[i] for i in idx]
        epochs.append({
            "urls": [u for u, _ in members],
            "mjd": float(np.mean([m for _, m in members])),
            "n_available": len(g),
        })
    return epochs

def download_raw(url: str):
    """Download one cutout: zodi-subtracted IMAGE + header. Cached as a small FITS file."""
    path = f"{CACHE}/raw/{key_of(url)}.fits"
    if os.path.exists(path):
        with fits.open(path) as h:
            return h["IMAGE"].data.astype(np.float32), h["IMAGE"].header.copy()

    last = None
    for attempt in range(4):
        try:
            with fits.open(url, cache=False) as h:
                img = h["IMAGE"].data.astype(np.float32)
                hdr = h["IMAGE"].header.copy()
                try:
                    zodi = h["ZODI"].data
                    if zodi is not None and zodi.shape == img.shape:
                        img = img - zodi.astype(np.float32)
                except KeyError:
                    pass
            fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(img, hdr, name="IMAGE")]).writeto(path, overwrite=True)
            return img, hdr
        except Exception as e:  # IRSA sometimes answers 503 when busy
            last = e
            time.sleep(2 + 2 * attempt)
    raise RuntimeError(f"download failed: {last}")


def bilinear(img, x, y):
    h, w = img.shape
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    ok = (x0 >= 0) & (y0 >= 0) & (x0 < w - 1) & (y0 < h - 1)
    x0c, y0c = np.clip(x0, 0, w - 2), np.clip(y0, 0, h - 2)
    fx, fy = x - x0c, y - y0c
    v = (img[y0c, x0c] * (1 - fx) * (1 - fy) + img[y0c, x0c + 1] * fx * (1 - fy)
         + img[y0c + 1, x0c] * (1 - fx) * fy + img[y0c + 1, x0c + 1] * fx * fy)
    v[~ok] = np.nan
    return v


def reproject(img, hdr, grid_wcs, npix):
    """Put one cutout onto the common grid."""
    src = WCS(hdr, naxis=2)
    yy, xx = np.mgrid[0:npix, 0:npix]
    gra, gdec = grid_wcs.pixel_to_world_values(xx, yy)
    sx, sy = src.world_to_pixel_values(gra, gdec)
    return bilinear(img, sx, sy)


def load_epoch(eid: str):
    path = f"{CACHE}/epochs/{eid}.json"
    if not os.path.exists(path):
        raise HTTPException(404, "Unknown frame id - call /frames first")
    with open(path) as f:
        return json.load(f)


def epoch_image(eid: str, pmra=0.0, pmdec=0.0, t0=None):
    """Median-combined, re-projected image of one epoch (cached)."""
    ep = load_epoch(eid)
    ra, dec = shifted_center(ep["ra"], ep["dec"], pmra, pmdec, ep["mjd"], t0)
    grid_wcs, npix = make_grid(ra, dec, ep["size"])
    path = f"{CACHE}/stack/{key_of(f'{eid},{ra:.7f},{dec:.7f},{npix}')}.npy"
    if os.path.exists(path):
        return np.load(path)

    def one(url):
        try:
            img, hdr = download_raw(url)
            return reproject(img, hdr, grid_wcs, npix)
        except Exception:
            return None

    layers = [l for l in pool.map(one, ep["urls"]) if l is not None]
    if not layers:
        raise HTTPException(502, "All downloads for this epoch failed (IRSA busy?) - try again")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        stack = np.nanmedian(np.stack(layers), axis=0).astype(np.float32)
    np.save(path, stack)
    return stack


def render(data, stretch, minbright, maxbright, invert, zoom, diff=False, limits=None):
    """Turn numbers into a grey PNG."""
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        finite = np.zeros(1)

    if diff:
        # symmetric around zero: grey = no change, white = got brighter, black = got fainter
        lim = limits if limits else float(np.percentile(np.abs(finite), maxbright)) or 1.0
        x = np.clip((np.nan_to_num(data, nan=0.0) / lim + 1) / 2, 0, 1)
    else:
        lo, hi = limits if limits else (float(np.percentile(finite, minbright)),
                                         float(np.percentile(finite, maxbright)))
        if hi <= lo:
            hi = lo + 1e-6
        x = np.clip((np.nan_to_num(data, nan=lo) - lo) / (hi - lo), 0, 1)
        if stretch == "asinh":
            x = np.arcsinh(x * 10) / np.arcsinh(10)
        elif stretch == "log":
            x = np.log10(1 + 99 * x) / 2

    if invert:
        x = 1 - x
    img = Image.fromarray(np.flipud(x * 255).astype(np.uint8))  # north up
    if zoom > 1:
        img = img.resize((img.width * zoom, img.height * zoom), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

@app.get("/bands")
def bands():
    return BANDS


@app.get("/frames")
def frames(
    ra: float = Query(..., description="Right ascension (deg)"),
    dec: float = Query(..., ge=-90, le=90, description="Declination (deg)"),
    band: str = Query("SPHEREx-D2"),
    size: float = Query(0.1, gt=0, le=0.5, description="Image size (deg)"),
    n: int = Query(10, ge=2, le=60, description="Max number of epochs to return"),
    window: float = Query(3.0, ge=0, le=200, description="Images within this many days form one epoch"),
    per_epoch: int = Query(3, ge=1, le=10, description="Max images combined per epoch"),
):
    """List epochs (groups of SPHEREx images close in time) covering (ra, dec)."""
    if band not in BANDS:
        raise HTTPException(400, f"band must be one of {BANDS}")
    ra = ra % 360
    try:
        rows = search_irsa(ra, dec, band, size)
    except Exception as e:
        raise HTTPException(502, f"IRSA search failed: {e}")

    _, npix = make_grid(ra, dec, size)
    if not rows:
        return {"count_total": 0, "epochs_total": 0, "npix": npix, "pixscale": PIXSCALE, "frames": []}

    epochs = group_epochs(rows, window, per_epoch)
    pick = np.unique(np.linspace(0, len(epochs) - 1, min(n, len(epochs))).astype(int))

    out = []
    for i in pick:
        ep = dict(epochs[i], ra=ra, dec=dec, size=size, band=band)
        eid = key_of(json.dumps(ep["urls"]) + f"{size}")
        with open(f"{CACHE}/epochs/{eid}.json", "w") as f:
            json.dump(ep, f)
        out.append({
            "id": eid,
            "mjd": ep["mjd"],
            "date": mjd_to_date(ep["mjd"]),
            "n_images": len(ep["urls"]),
            "n_available": ep["n_available"],
            "png": f"/frame/{eid}.png",
        })
    return {"count_total": len(rows), "epochs_total": len(epochs),
            "npix": npix, "pixscale": PIXSCALE, "frames": out}


@app.get("/frame/{eid}.png")
def frame_png(
    eid: str,
    stretch: str = Query("asinh", pattern="^(linear|asinh|log)$"),
    minbright: float = Query(1.0, ge=0, le=100, description="Lower brightness limit (percentile)"),
    maxbright: float = Query(99.5, ge=0, le=100, description="Upper brightness limit (percentile)"),
    invert: bool = Query(False, description="Black stars on white"),
    zoom: int = Query(1, ge=1, le=10, description="Enlarge pixels"),
    diff: str = Query("", description="Id of another epoch: show this one MINUS that one"),
    scale_ref: str = Query("", description="Id of an epoch whose brightness limits are used (same scale for all frames)"),
    pmra: float = Query(0.0, description="Track proper motion in RA (mas/yr)"),
    pmdec: float = Query(0.0, description="Track proper motion in Dec (mas/yr)"),
    t0: float = Query(None, description="Reference MJD for proper-motion tracking (usually the first epoch)"),
):
    """One epoch as a PNG, with WiseView-style display options."""
    data = epoch_image(eid, pmra, pmdec, t0)
    limits = None

    if diff:
        ref = epoch_image(diff, pmra, pmdec, t0)
        data = data - ref
        if scale_ref:
            d0 = epoch_image(scale_ref, pmra, pmdec, t0) - ref
            f = np.abs(d0[np.isfinite(d0)])
            limits = float(np.percentile(f, maxbright)) if f.size else None
    elif scale_ref:
        r = epoch_image(scale_ref, pmra, pmdec, t0)
        f = r[np.isfinite(r)]
        if f.size:
            limits = (float(np.percentile(f, minbright)), float(np.percentile(f, maxbright)))

    png = render(data, stretch, minbright, maxbright, invert, zoom, bool(diff), limits)
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/gaia")
def gaia(
    ra: float = Query(...),
    dec: float = Query(...),
    size: float = Query(0.1, gt=0, le=0.5),
    maglim: float = Query(17.0, description="Faintest Gaia G magnitude"),
    mjd: float = Query(None, description="Move stars to this date using their proper motion"),
):
    """Known Gaia DR3 stars, as pixel positions in the images (top-left origin)."""
    ra = ra % 360
    gfile = f"{CACHE}/gaia/{key_of(f'{ra:.5f},{dec:.5f},{size},{maglim}')}.json"
    if os.path.exists(gfile):
        with open(gfile) as f:
            stars = json.load(f)
    else:
        try:
            from astroquery.gaia import Gaia
            radius = size * 0.75
            q = f"""
            SELECT TOP 3000 source_id, ra, dec, phot_g_mean_mag, pmra, pmdec
            FROM gaiadr3.gaia_source
            WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius}))
              AND phot_g_mean_mag < {maglim}
            """
            t = Gaia.launch_job(q).get_results()
            col = {c.lower(): c for c in t.colnames}  # column names can be upper or lower case

            def num(r, name):
                try:
                    v = float(r[col[name]])
                    return v if math.isfinite(v) else 0.0
                except Exception:  # masked / missing value
                    return 0.0

            stars = []
            for r in t:
                stars.append({
                    "source_id": str(r[col["source_id"]]),
                    "ra": num(r, "ra"), "dec": num(r, "dec"),
                    "g": num(r, "phot_g_mean_mag"),
                    "pmra": num(r, "pmra"), "pmdec": num(r, "pmdec"),
                })
            with open(gfile, "w") as f:
                json.dump(stars, f)
        except Exception as e:
            raise HTTPException(502, f"Gaia search failed: {e}")

    grid_wcs, npix = make_grid(ra, dec, size)
    dt = ((mjd or MJD_J2016) - MJD_J2016) / 365.25
    out = []
    for s in stars:
        sdec = s["dec"] + s["pmdec"] * dt / 3.6e6
        sra = s["ra"] + s["pmra"] * dt / 3.6e6 / max(math.cos(math.radians(s["dec"])), 1e-6)
        x, y = grid_wcs.world_to_pixel_values(sra, sdec)
        y = npix - 1 - y  # images are flipped so north is up
        if -1 <= x <= npix and -1 <= y <= npix:
            out.append({**s, "x": float(x), "y": float(y)})
    return {"npix": npix, "stars": out}


@app.get("/resolve")
def resolve(name: str = Query(..., min_length=1, description="Object name, e.g. 'M101' or 'Vega'")):
    """Look up an object's coordinates by name (CDS Sesame). Positions are for the year 2000."""
    try:
        from astropy.coordinates import SkyCoord
        c = SkyCoord.from_name(name)
        return {"name": name, "ra": float(c.ra.deg), "dec": float(c.dec.deg)}
    except Exception as e:
        raise HTTPException(404, f"Unknown object: {e}")

VIEWER = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPHEREx Sky Change Explorer · Darth Invaders</title>
<style>
:root{--bg:#000;--panel:#263745;--field:#12191f;--line:#33414f;--text:#fff;--soft:#cfd9e3;--muted:#8fa2b4;--blue:#4b90da;--red:#da4839;--coral:#ff7a5a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,"Segoe UI",Arial,sans-serif;font-size:14px}
button,input,select{font:inherit}
/* header */
header{display:flex;align-items:center;gap:18px;padding:14px 26px 10px}
.brand{font-size:44px;font-weight:700;color:var(--blue);letter-spacing:.5px}
.title{font-size:23px;font-weight:700;margin-top:10px}
.spacer{flex:1}
.team{line-height:1.15;font-size:20px}.team span{color:var(--blue)}.team b{color:var(--red);display:block}
.divider{width:3px;height:52px;background:#fff;margin:0 10px}
.event{color:var(--blue);font-weight:700;font-size:23px;line-height:1.15}
/* layout */
.grid{display:grid;grid-template-columns:240px minmax(0,1fr) 210px;gap:20px;padding:4px 26px 24px;align-items:start}
@media(max-width:1100px){.grid{grid-template-columns:240px minmax(0,1fr)}.right{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media(max-width:760px){.grid{grid-template-columns:1fr}.right{grid-template-columns:1fr}header{flex-wrap:wrap}}
.panel{background:var(--panel);border-radius:16px;padding:12px 12px 14px}
h3{margin:12px 0 4px;color:var(--blue);font-size:14px;text-transform:uppercase;letter-spacing:.03em}
h3:first-child{margin-top:0}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:5px 0;color:#e8eef5}
.field{background:var(--field);color:var(--text);border:0;border-radius:8px;padding:4px 8px;width:88px;height:22px}
select.field{padding:0 6px;width:88px}
.source{background:var(--field);border-radius:8px;padding:6px 10px;margin:2px 0 4px;color:#cfd9e3;font-size:13px}
input[type=range]{width:88px;accent-color:#1a73e8}
input[type=checkbox]{width:13px;height:13px;accent-color:var(--blue)}
.btn{background:#3b82d6;color:#000;border:0;border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer}
.btn:disabled{opacity:.5;cursor:wait}
.ghost{background:var(--field);color:#b3aaaa;border:0;border-radius:6px;padding:4px 8px;font-size:13px;cursor:pointer}
/* centre */
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 14px 28px}
.chip{background:var(--field);border:1.5px solid var(--line);color:#e8eef5;border-radius:99px;padding:4px 12px;font-size:13px;cursor:pointer}
.chip.on{background:#1d3b5a;border-color:var(--blue);color:#fff;font-weight:700}
.stage{background:#05090d;border-radius:12px;border:1px solid #1b2530;height:350px;margin-left:28px;position:relative;overflow:auto;display:flex;align-items:center;justify-content:center}
#view{position:relative;line-height:0}
#img{image-rendering:pixelated;display:none}
#overlay{position:absolute;left:0;top:0}
.badge{position:absolute;left:10px;top:10px;background:rgba(5,9,13,.8);border-radius:6px;padding:4px 9px;font-size:13px;line-height:1.3;display:none;z-index:2}
.zoombtns{position:absolute;right:10px;top:10px;display:flex;flex-direction:column;border:1px solid #6b7480;border-radius:6px;overflow:hidden;z-index:2}
.zoombtns button{background:rgba(0,0,0,.5);color:#fff;border:0;width:24px;height:24px;cursor:pointer;font-size:16px}
.zoombtns button+button{border-top:1px solid #6b7480}
.empty{color:var(--muted);text-align:center;line-height:1.5;padding:20px;font-size:14px}
.player{display:flex;align-items:center;gap:12px;background:#0b1015;padding:10px 6px;border-radius:0 0 10px 10px}
.player button{background:transparent;color:#fff;border:1px solid #3a4450;border-radius:8px;height:40px;cursor:pointer;font-size:16px}
.player .pp{padding:0 16px}.player .sq{width:48px}
.player input{flex:1;accent-color:#1a73e8}
#msg{color:var(--muted);font-size:13px;margin:6px 0 6px 28px}
#msg.err{color:#ff8a7a}
.legend{display:flex;gap:40px;margin-left:28px;font-size:12px;color:var(--soft);flex-wrap:wrap}
.legend span{display:flex;align-items:center;gap:6px}
.legend i{width:11px;height:11px;border-radius:2px;display:inline-block}
/* right */
.right .panel+.panel{margin-top:14px}
.about p{margin:4px 0 6px;font-weight:700;font-size:14px;line-height:1.35}
.about ul{margin:0;padding:0;list-style:none;font-size:12.5px;line-height:1.45;color:#e8eef5}
.about small{display:block;color:var(--muted);font-size:11px;margin-top:12px;line-height:1.4}
.find{display:block;width:100%;background:var(--coral);color:#1a0904;border:0;border-radius:10px;padding:11px;font-weight:700;margin-top:14px;cursor:pointer}
#markinfo{font-size:12px;color:var(--soft);margin-top:8px;line-height:1.45}
</style></head><body>

<header>
  <div class="brand">SPHEREx</div><div class="title">Sky Change Explorer</div>
  <div class="spacer"></div>
  <div class="team"><span>Team</span><b>Darth Invaders</b></div>
  <div class="divider"></div>
  <div class="event">NASA Space Apps<br>Challenge</div>
</header>

<main class="grid">
  <!-- LEFT: controls -->
  <aside class="panel">
    <div class="row" style="margin-top:0">Data source</div>
    <div class="source" title="All images come from NASA's SPHEREx all-sky survey (IRSA archive)">SPHEREx All-Sky Survey</div>
    <h3>Where</h3>
    <div class="row">RA (deg) <input id="f_ra" class="field" value="269.446"></div>
    <div class="row">Dec (deg) <input id="f_dec" class="field" value="4.768"></div>
    <div class="row">Size (deg) <input id="f_size" class="field" type="number" step="0.02" min="0.02" max="0.5" value="0.1"></div>
    <div class="row">Band <select id="f_band" class="field">
      <option value="SPHEREx-D1">D1</option><option value="SPHEREx-D2" selected>D2</option><option value="SPHEREx-D3">D3</option>
      <option value="SPHEREx-D4">D4</option><option value="SPHEREx-D5">D5</option><option value="SPHEREx-D6">D6</option></select></div>
    <h3>Time</h3>
    <div class="row">Window (days) <input id="f_window" class="field" type="number" min="0" max="200" value="3"></div>
    <div class="row">Max epochs <input id="f_n" class="field" type="number" min="2" max="60" value="10"></div>
    <div class="row">Images per epoch <input id="f_per" class="field" type="number" min="1" max="10" value="3"></div>
    <button id="loadbtn" class="btn" onclick="load()" style="margin:6px 0 2px">Load</button>
    <h3>Display</h3>
    <div class="row">Stretch <select id="f_stretch" class="field" onchange="refresh()"><option>asinh</option><option>linear</option><option>log</option></select></div>
    <div class="row">Min bright (%) <input id="f_min" class="field" type="number" min="0" max="100" step="0.5" value="1" onchange="refresh()"></div>
    <div class="row">Max bright (%) <input id="f_max" class="field" type="number" min="0" max="100" step="0.1" value="99.5" onchange="refresh()"></div>
    <div class="row">Same scale all frames <input id="f_lock" type="checkbox" checked onchange="refresh()"></div>
    <div class="row">Invert <input id="f_invert" type="checkbox" onchange="refresh()"></div>
    <div class="row">Difference <select id="f_diff" class="field" onchange="refresh()"><option value="">Off</option><option value="first">− first</option><option value="prev">− previous</option></select></div>
    <div class="row">Zoom <input id="f_zoom" type="range" min="2" max="14" value="5"></div>
    <div class="row">Speed <input id="f_speed" type="range" min="150" max="2000" step="50" value="600" style="direction:rtl"></div>
  </aside>

  <!-- CENTRE: viewer -->
  <section>
    <div class="chips" id="chips"></div>
    <div class="stage">
      <div class="badge" id="badge"></div>
      <div class="zoombtns"><button onclick="zoomBy(1)" title="Zoom in">+</button><button onclick="zoomBy(-1)" title="Zoom out">−</button></div>
      <div id="view">
        <div class="empty" id="empty">Pick a target above or type coordinates, then press <b>Load</b>.</div>
        <img id="img" alt="SPHEREx image"><svg id="overlay"></svg>
      </div>
    </div>
    <div class="player" style="margin-left:28px">
      <button class="pp" id="playbtn" onclick="togglePlay()">Play / Pause</button>
      <button class="sq" onclick="step(-1)">◀</button>
      <button class="sq" onclick="step(1)">▶</button>
      <input type="range" id="f_slider" min="0" max="0" value="0" oninput="playing=false;show(+this.value)">
    </div>
    <div id="msg">Real SPHEREx images from NASA's IRSA archive.</div>
    <div class="legend">
      <span><i style="background:#fff"></i>White: brighter / moved in</span>
      <span><i style="background:#000;border:1px solid #8fa2b4"></i>Black: fainter / moved out</span>
      <span><i style="background:#808080"></i>Grey: no change</span>
    </div>
  </section>

  <!-- RIGHT: extras + about -->
  <aside class="right">
    <div class="panel">
      <h3>Extras</h3>
      <div class="row">Gaia stars <input id="f_gaia" type="checkbox" onchange="refresh()"></div>
      <div class="row">PM RA (mas/yr) <input id="f_pmra" class="field" type="number" value="0" style="width:62px"></div>
      <div class="row">PM Dec (mas/yr) <input id="f_pmdec" class="field" type="number" value="0" style="width:62px"></div>
      <div class="row" style="margin-top:10px">
        <button class="ghost" onclick="refresh()">Apply display</button>
        <button class="ghost" onclick="copyLink()">Copy share link</button>
      </div>
    </div>
    <div class="panel about">
      <h3>About</h3>
      <p>SPHEREx Sky Change Explorer lets anyone watch the infrared sky change over time.</p>
      <ul>
        <li>• NASA's SPHEREx mission maps the whole sky every 6 months in 102 infrared colours.</li>
        <li>• Images come from NASA's IRSA archive, aligned north-up and combined per epoch.</li>
        <li>• Blink between dates: anything that moves or changes brightness jumps out.</li>
        <li>• Inspired by WiseView.</li>
      </ul>
      <small>Made by Darth Invaders · NASA Space Apps Challenge 2026</small>
    </div>
    <div>
      <button class="find" id="markbtn" onclick="toggleMark()">I found something moving!</button>
      <div id="markinfo"></div>
    </div>
  </aside>
</main>

<script>
const $=id=>document.getElementById(id);
const PIX=6.2; // arcsec per pixel, same as the backend
const FIELDS=['ra','dec','size','band','window','n','per','stretch','min','max','lock','invert','diff','zoom','speed','gaia','pmra','pmdec'];
// Approximate positions for 2026 (fast stars have moved since the year 2000)
const TARGETS={
  barnard:{name:"Barnard's Star",ra:269.446,dec:4.768,band:"SPHEREx-D2"},
  wise0855:{name:"WISE 0855",ra:133.770,dec:-7.243,band:"SPHEREx-D6"},
  epsindi:{name:"Epsilon Indi",ra:331.096,dec:-56.801,band:"SPHEREx-D5"},
  rwaur:{name:"RW Aurigae",ra:76.956,dec:30.401,band:"SPHEREx-D3"},
  rdra:{name:"R Draconis",ra:248.168,dec:66.755,band:"SPHEREx-D2"},
  m101:{name:"M101",ra:210.80227,dec:54.34895,band:"SPHEREx-D2"}
};
let frames=[],npix=0,cur=0,playing=true,timer=null,gaiaStars=[],active='',marking=false,mark=null;

function val(k){const e=$('f_'+k);return e.type==='checkbox'?e.checked:e.value}
function setv(k,v){const e=$('f_'+k);if(!e)return;if(e.type==='checkbox')e.checked=(v==='1'||v===true);else e.value=v}
function say(t,err){$('msg').textContent=t;$('msg').className=err?'err':''}

// quick targets
$('chips').innerHTML=Object.entries(TARGETS).map(([k,t])=>`<button class="chip" data-k="${k}" onclick="pick('${k}')">${t.name}</button>`).join('');
function paintChips(){document.querySelectorAll('.chip').forEach(c=>c.classList.toggle('on',c.dataset.k===active))}
function pick(k){const t=TARGETS[k];active=k;setv('ra',t.ra);setv('dec',t.dec);setv('band',t.band);mark=null;$('markinfo').textContent='';paintChips();load()}
['f_ra','f_dec'].forEach(id=>$(id).addEventListener('input',()=>{active='';paintChips()}));

// share link (all settings live in the URL)
function writeHash(){const p=new URLSearchParams();FIELDS.forEach(k=>{let v=val(k);if(typeof v==='boolean')v=v?'1':'0';p.set(k,v)});
  if(active)p.set('t',active);if(mark)p.set('mark',mark.x.toFixed(1)+','+mark.y.toFixed(1));history.replaceState(null,'','#'+p)}
function readHash(){if(location.hash.length<2)return;const p=new URLSearchParams(location.hash.slice(1));
  FIELDS.forEach(k=>{if(p.has(k))setv(k,p.get(k))});active=p.get('t')||'';
  if(p.has('mark')){const [x,y]=p.get('mark').split(',').map(Number);mark={x,y}}paintChips();if(p.has('ra'))load()}
function copyLink(){writeHash();navigator.clipboard.writeText(location.href).then(()=>say('Share link copied!'),()=>say(location.href))}

// image urls
function src(k){
  const f=frames[k],p=new URLSearchParams({stretch:val('stretch'),minbright:val('min'),maxbright:val('max'),invert:val('invert')});
  const a=+val('pmra')||0,b=+val('pmdec')||0;
  if(a||b){p.set('pmra',a);p.set('pmdec',b);p.set('t0',frames[0].mjd)}   // proper-motion tracking
  const d=val('diff');
  if(d==='first'&&k>0)p.set('diff',frames[0].id);
  if(d==='prev'&&k>0)p.set('diff',frames[k-1].id);
  if(val('lock')){if(d==='first'&&frames.length>1)p.set('scale_ref',frames[frames.length-1].id);else if(!d)p.set('scale_ref',frames[0].id)}
  return f.png+'?'+p;
}
function list(){return val('diff')&&frames.length>1?frames.map((_,k)=>k).slice(1):frames.map((_,k)=>k)}

async function load(){
  writeHash();frames=[];gaiaStars=[];$('img').style.display='none';$('badge').style.display='none';$('overlay').innerHTML='';
  $('empty').style.display='block';$('empty').innerHTML='Searching NASA\'s IRSA archive…<br><small>The first time for a new spot can take a minute or two.</small>';
  $('loadbtn').disabled=true;say('');
  const q=new URLSearchParams({ra:val('ra'),dec:val('dec'),band:val('band'),size:val('size'),n:val('n'),window:val('window'),per_epoch:val('per')});
  try{
    const r=await fetch('/frames?'+q),j=await r.json();
    if(!r.ok)throw new Error(j.detail||r.status);
    if(!j.frames.length){$('empty').textContent='No SPHEREx images of this spot in this band yet. Try another band.';return}
    frames=j.frames;npix=j.npix;
    $('empty').innerHTML=`Found ${j.count_total} images in ${j.epochs_total} epochs.<br>Aligning and combining…`;
    await refresh(true);
    say(`${j.count_total} SPHEREx images · ${j.epochs_total} epochs · ${frames[0].date} to ${frames[frames.length-1].date}`);
  }catch(e){
    const busy=/IRSA|502|503|Proxy/i.test(e.message);
    $('empty').innerHTML=busy
      ?'NASA\'s IRSA archive is busy right now (too many people using it).<br>Wait a minute and press <b>Load</b> again. Targets you loaded before still work.'
      :'Something went wrong: '+e.message+'.<br>Is the backend (uvicorn) still running?';
  }
  finally{$('loadbtn').disabled=false}
}

async function refresh(first){
  if(!frames.length)return;writeHash();
  const ks=list();let done=0,fail=0;
  await Promise.all(ks.map(k=>new Promise(res=>{const im=new Image();
    im.onload=()=>{done++;if(first)$('empty').innerHTML=`Building epoch images… ${done}/${ks.length}`;else say(`Updating images… ${done}/${ks.length}`);res()};
    im.onerror=()=>{fail++;res()};im.src=src(k)})));
  if(val('gaia'))await loadGaia();else gaiaStars=[];
  $('empty').style.display='none';$('img').style.display='block';$('badge').style.display='block';
  $('f_slider').max=ks.length-1;show(Math.min(cur,ks.length-1));restartTimer();
  if(fail)say(`${fail} image(s) failed because the archive is busy. Press Apply display to retry.`,true);
  else if(!first)say(`${frames.length} epochs · ${frames[0].date} to ${frames[frames.length-1].date}`);
}

async function loadGaia(){
  const q=new URLSearchParams({ra:val('ra'),dec:val('dec'),size:val('size'),mjd:frames[0].mjd});
  try{const r=await fetch('/gaia?'+q),j=await r.json();if(!r.ok)throw new Error(j.detail);gaiaStars=j.stars}
  catch(e){gaiaStars=[];say('Gaia overlay failed: '+e.message,true)}
}

function show(pos){
  const ks=list();if(!ks.length)return;cur=((pos%ks.length)+ks.length)%ks.length;$('f_slider').value=cur;
  const k=ks[cur],z=+val('zoom'),W=npix*z,im=$('img');
  im.src=src(k);im.style.width=W+'px';im.style.height=W+'px';
  const d=val('diff'),f=frames[k];
  $('badge').textContent=f.date+(d==='first'?' − '+frames[0].date:d==='prev'?' − '+frames[k-1].date:'');
  const svg=$('overlay'),c=npix/2;svg.setAttribute('width',W);svg.setAttribute('height',W);svg.setAttribute('viewBox',`0 0 ${npix} ${npix}`);
  svg.style.cursor=marking?'crosshair':'default';svg.style.pointerEvents=marking?'auto':'none';
  svg.innerHTML=`<circle cx="${c}" cy="${c}" r="${Math.max(2,npix/25)}" fill="none" stroke="#ff7a5a" stroke-width=".5" opacity=".75"/>`+
    gaiaStars.map(s=>`<circle cx="${s.x+.5}" cy="${s.y+.5}" r="1.6" fill="none" stroke="#4b90da" stroke-width=".3"><title>Gaia ${s.source_id} · G=${s.g.toFixed(1)}</title></circle>`).join('')+
    (mark?`<g stroke="#ff7a5a" stroke-width=".4"><circle cx="${mark.x}" cy="${mark.y}" r="2.2" fill="none"/><path d="M${mark.x-4} ${mark.y}h1.6M${mark.x+2.4} ${mark.y}h1.6M${mark.x} ${mark.y-4}v1.6M${mark.x} ${mark.y+2.4}v1.6"/></g>`:'');
}
function step(d){playing=false;show(cur+d)}
function togglePlay(){playing=!playing}
function zoomBy(d){const e=$('f_zoom');e.value=Math.max(+e.min,Math.min(+e.max,+e.value+d));show(cur);writeHash()}
function restartTimer(){clearInterval(timer);timer=setInterval(()=>{if(playing&&frames.length)show(cur+1)},+val('speed'))}
$('f_speed').addEventListener('input',()=>{restartTimer();writeHash()});
$('f_zoom').addEventListener('input',()=>{show(cur);writeHash()});

// "I found something moving!": click on the dot to mark it
function toggleMark(){
  if(!frames.length){say('Load a spot first.',true);return}
  marking=!marking;playing=!marking;
  $('markbtn').textContent=marking?'Now click on the moving dot…':'I found something moving!';show(cur);
}
$('overlay').addEventListener('click',e=>{
  if(!marking)return;const r=$('overlay').getBoundingClientRect();
  const x=(e.clientX-r.left)/r.width*npix,y=(e.clientY-r.top)/r.height*npix;mark={x,y};
  const ra0=+val('ra'),dec0=+val('dec'),dx=(x-npix/2)*PIX/3600,dy=(npix/2-y)*PIX/3600;
  const dec=dec0+dy,ra=ra0-dx/Math.cos(dec0*Math.PI/180);
  marking=false;$('markbtn').textContent='I found something moving!';writeHash();show(cur);
  $('markinfo').innerHTML=`<b>Marked at RA ${ra.toFixed(4)}°, Dec ${dec.toFixed(4)}°</b> on ${frames[list()[cur]].date}.<br>Turn on Gaia stars: no blue circle means it may be new! <a href="#" onclick="copyLink();return false" style="color:#4b90da">Copy link to this find</a>`;
});

readHash();
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return VIEWER
