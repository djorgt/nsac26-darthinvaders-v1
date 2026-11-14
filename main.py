"""
SPHEREx Sky Change viewer - backend API  (v2, WiseView-style features)

Run with:   uvicorn main:app --reload
Then open:  http://localhost:8000        (built-in viewer with all controls)
            http://localhost:8000/docs   (API documentation for the frontend team)

What it does
------------
1. /frames  : finds every SPHEREx image covering (ra, dec) in one detector band at IRSA,
              groups images taken close together in time into "epochs" (WiseView 'window'),
              and returns a list of epochs.
2. /frame/{id}.png : builds one epoch image:
              - downloads the cutouts (with retries, cached on disk)
              - subtracts the zodiacal-light background (ZODI extension)
              - re-projects every cutout onto ONE common north-up grid
                (SPHEREx visits are rotated differently, so this is needed to compare them)
              - median-combines the images of the epoch (less noise)
              options: stretch, minbright/maxbright, invert, zoom, diff, same scale,
                       proper-motion tracking (pmra/pmdec)
3. /gaia    : known Gaia DR3 stars in the field, in pixel coordinates of the image (overlay).
"""

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
PIXSCALE = 6.2          # arcsec per output pixel (close to SPHEREx native pixels)
CUTOUT_MARGIN = 1.5     # cutouts are larger than the output so rotation leaves no gaps
MJD_J2016 = 57388.0     # Gaia DR3 reference epoch

app = FastAPI(title="SPHEREx Sky Change API", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
pool = ThreadPoolExecutor(max_workers=6)


# =====================================================================
# helpers
# =====================================================================

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


# =====================================================================
# 1. searching IRSA
# =====================================================================

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
    t = Irsa.query_tap(query).to_table()
    rows = [(str(r["url"]), float(r["mjd"])) for r in t]
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


# =====================================================================
# 2. downloading and building images
# =====================================================================

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


# =====================================================================
# API
# =====================================================================

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


# =====================================================================
# Built-in viewer (WiseView-style). The real frontend can copy ideas from it.
# =====================================================================

VIEWER = r"""<!doctype html><html><head><meta charset="utf-8">
<title>SPHEREx Sky Change viewer</title>
<style>
 body{font-family:system-ui,sans-serif;background:#05090d;color:#e8eef5;margin:0;padding:18px}
 h2{margin:0 0 4px} .sub{color:#8fa2b4;font-size:13px;margin-bottom:12px}
 .wrap{display:flex;gap:22px;flex-wrap:wrap;align-items:flex-start}
 .panel{background:#0c141c;border:1px solid #22303d;border-radius:12px;padding:12px;min-width:300px;max-width:360px}
 .panel h3{margin:10px 0 6px;font-size:13px;color:#7cc4ff;text-transform:uppercase;letter-spacing:.05em}
 label{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:13px;margin:5px 0}
 input,select{background:#05090d;color:#e8eef5;border:1px solid #33414f;border-radius:6px;padding:4px 6px}
 input[type=number],input[type=text]{width:110px} input[type=range]{width:150px}
 button{background:#7cc4ff;color:#05090d;border:0;border-radius:8px;padding:8px 14px;font-weight:700;cursor:pointer;margin:3px 3px 3px 0}
 button.alt{background:transparent;color:#e8eef5;border:1px solid #33414f;font-weight:500}
 .chips button{background:transparent;color:#e8eef5;border:1px solid #33414f;font-weight:500;padding:5px 10px;border-radius:99px;font-size:12px}
 #view{position:relative;background:#000;border:1px solid #22303d;border-radius:10px;overflow:hidden;line-height:0}
 #img{image-rendering:pixelated;display:block}
 #overlay{position:absolute;left:0;top:0;pointer-events:none}
 #date{position:absolute;left:8px;top:8px;background:rgba(0,0,0,.65);padding:4px 8px;border-radius:6px;font-size:14px;line-height:1.2}
 #msg{font-size:13px;color:#9fb0c0;margin-top:8px;max-width:600px;line-height:1.4} .err{color:#ff8a7a!important}
 .ctrl{display:flex;align-items:center;gap:6px;margin-top:8px;flex-wrap:wrap}
</style></head><body>
<h2>SPHEREx Sky Change viewer</h2>
<div class="sub">Real NASA SPHEREx images from IRSA, aligned north-up, background removed, combined per epoch.</div>
<div class="wrap">
<div class="panel">
  <div class="chips">
    <button onclick="go(269.452,4.693)">Barnard's Star</button>
    <button onclick="go(133.795,-7.245)">WISE 0855</button>
    <button onclick="go(331.044,-56.783)">Epsilon Indi</button>
    <button onclick="go(76.956,30.401)">RW Aurigae</button>
    <button onclick="go(248.168,66.755)">R Draconis</button>
    <button onclick="go(210.80227,54.34895)">M101</button>
  </div>
  <h3>Where</h3>
  <label>RA (deg) <input id="f_ra" type="text" value="133.795"></label>
  <label>Dec (deg) <input id="f_dec" type="text" value="-7.245"></label>
  <label>Size (deg) <input id="f_size" type="number" step="0.02" min="0.02" max="0.5" value="0.1"></label>
  <label>Band <select id="f_band"></select></label>
  <h3>Time</h3>
  <label>Window (days) <input id="f_window" type="number" min="0" max="200" step="1" value="3"></label>
  <label>Max epochs <input id="f_n" type="number" min="2" max="60" value="10"></label>
  <label>Images per epoch <input id="f_per" type="number" min="1" max="10" value="3"></label>
  <button onclick="load()">Load</button>
  <h3>Display</h3>
  <label>Stretch <select id="f_stretch"><option>asinh</option><option>linear</option><option>log</option></select></label>
  <label>Min bright (%) <input id="f_min" type="number" min="0" max="100" step="0.5" value="1"></label>
  <label>Max bright (%) <input id="f_max" type="number" min="0" max="100" step="0.1" value="99.5"></label>
  <label>Same scale all frames <input id="f_lock" type="checkbox" checked></label>
  <label>Invert <input id="f_invert" type="checkbox"></label>
  <label>Difference <select id="f_diff"><option value="">off</option><option value="first">minus first epoch</option><option value="prev">minus previous epoch</option></select></label>
  <label>Zoom <input id="f_zoom" type="range" min="2" max="14" value="7"></label>
  <label>Speed (ms) <input id="f_speed" type="range" min="150" max="2000" step="50" value="600"></label>
  <h3>Extras</h3>
  <label>Gaia stars <input id="f_gaia" type="checkbox"></label>
  <label>PM RA (mas/yr) <input id="f_pmra" type="number" value="0"></label>
  <label>PM Dec (mas/yr) <input id="f_pmdec" type="number" value="0"></label>
  <button class="alt" onclick="refresh()">Apply display</button>
  <button class="alt" onclick="copyLink()">Copy share link</button>
</div>
<div>
  <div id="view"><img id="img" alt=""><svg id="overlay"></svg><div id="date"></div></div>
  <div class="ctrl">
    <button class="alt" onclick="playing=!playing">Play / Pause</button>
    <button class="alt" onclick="step(-1)">&#9664;</button>
    <button class="alt" onclick="step(1)">&#9654;</button>
    <input id="f_slider" type="range" min="0" value="0" style="width:260px" oninput="playing=false;show(+this.value)">
  </div>
  <div id="msg">Pick a target or type coordinates, then press Load.</div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
const FIELDS=['ra','dec','size','band','window','n','per','stretch','min','max','lock','invert','diff','zoom','speed','gaia','pmra','pmdec'];
let frames=[], npix=0, i=0, playing=true, timer=null, gaiaStars=[];

function val(k){const e=$('f_'+k);return e.type==='checkbox'?e.checked:e.value}
function setv(k,v){const e=$('f_'+k);if(!e)return;if(e.type==='checkbox')e.checked=(v==='1'||v===true);else e.value=v}
function say(t,err){$('msg').textContent=t;$('msg').className=err?'err':''}

fetch('/bands').then(r=>r.json()).then(b=>{
  $('f_band').innerHTML=b.map(x=>`<option>${x}</option>`).join('');
  readHash();
});

function writeHash(){const p=new URLSearchParams();FIELDS.forEach(k=>{let v=val(k);if(typeof v==='boolean')v=v?'1':'0';p.set(k,v)});history.replaceState(null,'','#'+p)}
function readHash(){if(location.hash.length<2){$('f_band').value='SPHEREx-D2';return}
  const p=new URLSearchParams(location.hash.slice(1));FIELDS.forEach(k=>{if(p.has(k))setv(k,p.get(k))});
  if(p.has('ra'))load()}
function copyLink(){writeHash();navigator.clipboard.writeText(location.href).then(()=>say('Share link copied!'),()=>say(location.href))}

function go(ra,dec){setv('ra',ra);setv('dec',dec);load()}

function src(k){
  const f=frames[k], p=new URLSearchParams({stretch:val('stretch'),minbright:val('min'),maxbright:val('max'),invert:val('invert')});
  const pmra=+val('pmra')||0, pmdec=+val('pmdec')||0;
  if(pmra||pmdec){p.set('pmra',pmra);p.set('pmdec',pmdec);p.set('t0',frames[0].mjd)}
  const d=val('diff');
  if(d==='first'&&k>0)p.set('diff',frames[0].id);
  if(d==='prev'&&k>0)p.set('diff',frames[k-1].id);
  if(val('lock')){ if(d==='first'&&frames.length>1)p.set('scale_ref',frames[frames.length-1].id);
                   else if(!d)p.set('scale_ref',frames[0].id); }
  return f.png+'?'+p;
}
function list(){ // with a difference view the first frame has nothing to compare to
  return val('diff')&&frames.length>1?frames.map((_,k)=>k).slice(1):frames.map((_,k)=>k)}

async function load(){
  writeHash();frames=[];$('img').removeAttribute('src');$('date').textContent='';$('overlay').innerHTML='';
  say('Searching the IRSA archive (first time can take a minute)...');
  const q=new URLSearchParams({ra:val('ra'),dec:val('dec'),band:val('band'),size:val('size'),n:val('n'),window:val('window'),per_epoch:val('per')});
  try{
    const r=await fetch('/frames?'+q), j=await r.json();
    if(!r.ok)throw new Error(j.detail||r.status);
    if(!j.frames.length){say('No SPHEREx images of this spot in this band yet.',true);return}
    frames=j.frames;npix=j.npix;
    say(`${j.count_total} images in ${j.epochs_total} epochs. Building ${frames.length} aligned epoch images (downloads can be slow)...`);
    await refresh();
    say(`${j.count_total} SPHEREx images · ${j.epochs_total} epochs · showing ${frames.length} · ${frames[0].date} to ${frames[frames.length-1].date}`);
  }catch(e){say('Error: '+e.message+' - is the backend running?',true)}
}

async function refresh(){
  if(!frames.length)return;writeHash();
  const ks=list();let done=0,fail=0;
  await Promise.all(ks.map(k=>new Promise(res=>{const im=new Image();
    im.onload=()=>{done++;say(`Loading images ${done}/${ks.length}...`);res()};
    im.onerror=()=>{fail++;res()};im.src=src(k)})));
  if(fail)say(`${fail} epoch image(s) failed (archive busy) - press Apply display to retry.`,true);
  if(val('gaia'))await loadGaia();else gaiaStars=[];
  $('f_slider').max=ks.length-1;show(0);restartTimer();
}

async function loadGaia(){
  const q=new URLSearchParams({ra:val('ra'),dec:val('dec'),size:val('size'),mjd:frames[0].mjd});
  try{const r=await fetch('/gaia?'+q),j=await r.json();if(!r.ok)throw new Error(j.detail);gaiaStars=j.stars}
  catch(e){gaiaStars=[];say('Gaia overlay failed: '+e.message,true)}
}

function show(pos){
  const ks=list();if(!ks.length)return;i=((pos%ks.length)+ks.length)%ks.length;$('f_slider').value=i;
  const k=ks[i],z=+val('zoom'),W=npix*z;
  const im=$('img');im.src=src(k);im.style.width=W+'px';im.style.height=W+'px';
  const d=val('diff');
  $('date').textContent=frames[k].date+(d==='first'?' − '+frames[0].date:d==='prev'?' − '+frames[k-1].date:'')+`  (${frames[k].n_images} img)`;
  const svg=$('overlay');svg.setAttribute('width',W);svg.setAttribute('height',W);svg.setAttribute('viewBox',`0 0 ${npix} ${npix}`);
  const c=npix/2;
  svg.innerHTML=`<circle cx="${c}" cy="${c}" r="${Math.max(2,npix/25)}" fill="none" stroke="#ff7a5a" stroke-width="${0.6}" opacity=".8"/>`+
    gaiaStars.map(s=>`<circle cx="${s.x+0.5}" cy="${s.y+0.5}" r="1.6" fill="none" stroke="#7cc4ff" stroke-width="0.25"><title>Gaia ${s.source_id} G=${s.g.toFixed(1)}</title></circle>`).join('');
}
function step(d){playing=false;show(i+d)}
function restartTimer(){clearInterval(timer);timer=setInterval(()=>{if(playing)show(i+1)},+val('speed'))}
$('f_speed').addEventListener('input',()=>{restartTimer();writeHash()});
$('f_zoom').addEventListener('input',()=>{show(i);writeHash()});
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return VIEWER
