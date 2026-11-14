import React, { useEffect, useMemo, useRef, useState } from 'react';
import { BANDS, EPOCHS, PRESETS, createSimulation, parseCoordinate } from './sky-engine.js';
import featured from './nasa-featured.json';
import destinations from './objects.json';
import './Explore.css';

// This is a drop-in replacement for the existing Explore component.
// It preserves the original data files, NASA API and simulated SPHEREx engine.
const TARGETS = {
  barnard: 'Barnard’s Star',
  epsindi: 'Epsilon Indi B',
  wise0855: 'WISE 0855',
  rwaur: 'RW Aurigae',
  rdra: 'R Draconis',
};
const MODE_HINTS = {
  flip: 'Animate four simulated visits to reveal motion and brightness.',
  stack: 'Combine four simulated visits into one deep exposure.',
  diff: 'Compare with visit 1. Coral = increase; cyan = decrease.',
};
const TABS = [
  { id: 'media', symbol: '✦', title: 'Cosmic gallery', caption: 'NASA imagery & video' },
  { id: 'flipbook', symbol: '◎', title: 'Infrared observatory', caption: 'SPHEREx simulator' },
  { id: 'data', symbol: '⌁', title: 'Mission archives', caption: 'Science & open data' },
];
const SOURCES = [
  ['01', 'NASA Worldview', 'See Earth from orbit', 'Compare satellite images through time with NASA’s interactive Earth map.', 'https://worldview.earthdata.nasa.gov/', '◉'],
  ['02', 'NASA Earthdata', 'Decode our planet', 'Explore Earth-observation datasets, access tools, and scientific documentation.', 'https://www.earthdata.nasa.gov/', '⌁'],
  ['03', 'NASA Earth Science', 'Explore a living world', 'Meet the missions studying our atmosphere, land, oceans, and ice.', 'https://science.nasa.gov/earth/', '✧'],
  ['04', 'SPHEREx at IRSA', 'Discover the infrared sky', 'View released mission data and documentation, separate from this simulation.', 'https://irsa.ipac.caltech.edu/Missions/spherex.html', '◎'],
];

function Segmented({ value, onChange, items, label }) {
  return (
    <div className="cosmos-segmented" role="group" aria-label={label}>
      {items.map(([id, title]) => (
        <button
          key={id}
          type="button"
          aria-pressed={id === value}
          className={id === value ? 'is-active' : ''}
          onClick={() => onChange(id)}
        >
          {title}
        </button>
      ))}
    </div>
  );
}

function Starfield() {
  // Decorative only, deterministic and light-weight: no canvas animation or dependency.
  const stars = useMemo(() => Array.from({ length: 60 }, (_, i) => {
    const n = (i + 1) * 127.1;
    const frac = x => x - Math.floor(x);
    return {
      left: `${frac(Math.sin(n) * 43758.5453) * 100}%`,
      top: `${frac(Math.sin(n + 81.17) * 24634.6345) * 100}%`,
      width: i % 19 === 0 ? 2 : 1,
      opacity: 0.08 + frac(Math.sin(n + 9) * 1543) * 0.3,
      animationDelay: `${(i % 17) * -0.43}s`,
      animationDuration: `${3 + (i % 7)}s`,
    };
  }), []);
  return (
    <div className="cosmos-atmosphere" aria-hidden="true">
      <div className="cosmos-nebula cosmos-nebula-a" />
      <div className="cosmos-nebula cosmos-nebula-b" />
      <div className="cosmos-nebula cosmos-nebula-c" />
      <div className="cosmos-starfield">
        {stars.map((style, i) => <i key={i} style={style} />)}
      </div>
      <div className="cosmos-stardust" />
    </div>
  );
}

function SignalGraph({ spectrum, selected, isDifference }) {
  const max = Math.max(8, ...spectrum.map(x => Math.abs(x)));
  return (
    <div className="cosmos-panel cosmos-spectrum">
      <div className="cosmos-panel-heading">
        <div>
          <span className="cosmos-eyebrow">SPECTRAL ANALYSIS / 06 CHANNELS</span>
          <h3>{isDifference ? 'Change in detector signal' : 'Infrared signal spectrum'}</h3>
        </div>
        <span className="cosmos-mini-status">SIMULATED</span>
      </div>
      <svg viewBox="0 0 510 162" role="img" aria-label="Simulated relative signal across six infrared bands" className="cosmos-spectral-svg">
        <defs>
          <linearGradient id="cosmos-signal-positive" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#f2b5fe" />
            <stop offset="100%" stopColor="#746bff" />
          </linearGradient>
          <linearGradient id="cosmos-signal-negative" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#5de1e6" />
            <stop offset="100%" stopColor="#3189df" />
          </linearGradient>
        </defs>
        {[22, 49, 76, 103].map(y => <line key={y} x1="16" x2="496" y1={y} y2={y} stroke="rgba(166,178,238,.16)" strokeDasharray="3 7" />)}
        <line x1="16" x2="496" y1="76" y2="76" stroke="#6975aa" strokeWidth="1" />
        {spectrum.map((v, i) => {
          const height = Math.abs(v) / max * 52;
          const active = selected === 'all' || +selected === i;
          return (
            <g key={i} opacity={active ? 1 : 0.2}>
              <rect x={34 + i * 80} y={v >= 0 ? 76 - height : 76} width="46" height={Math.max(1, height)} rx="5" fill={v >= 0 ? 'url(#cosmos-signal-positive)' : 'url(#cosmos-signal-negative)'} />
              <text x={57 + i * 80} y="139" fill="#aab6d6" fontSize="12" textAnchor="middle">{((BANDS[i][0] + BANDS[i][1]) / 2).toFixed(1)}</text>
            </g>
          );
        })}
        <text x="497" y="157" fill="#7f8ba9" fontSize="10" textAnchor="end">µm</text>
      </svg>
      <p className="cosmos-helper">Model aperture signal (arbitrary units), not calibrated measurements.</p>
    </div>
  );
}

export function Flipbook() {
  const [target, setTarget] = useState('barnard');
  const [ra, setRa] = useState(PRESETS.barnard.ra);
  const [dec, setDec] = useState(PRESETS.barnard.dec);
  const [fov, setFov] = useState('6');
  const [loaded, setLoaded] = useState({ ra: PRESETS.barnard.raD, dec: PRESETS.barnard.decD, fov: 6, name: TARGETS.barnard });
  const [errors, setErrors] = useState({});
  const [epoch, setEpoch] = useState(0);
  const [band, setBand] = useState('all');
  const [mode, setMode] = useState('flip');
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(2);
  const canvas = useRef(null);
  const simulation = useMemo(() => {
    const engine = createSimulation();
    engine.load(loaded.ra, loaded.dec, loaded.fov);
    return engine;
  }, [loaded]);
  const spectrum = useMemo(() => simulation.spectrum(epoch, mode), [simulation, epoch, mode]);

  useEffect(() => {
    if (!playing || mode === 'stack') return undefined;
    const timer = window.setInterval(() => setEpoch(value => (value + 1) % EPOCHS.length), 1000 / speed);
    return () => window.clearInterval(timer);
  }, [playing, speed, mode]);

  useEffect(() => {
    const canvasElement = canvas.current;
    const context = canvasElement?.getContext('2d');
    if (!canvasElement || !context) return;

    const { pixels, grid } = simulation.image(epoch, band, mode);
    const frame = document.createElement('canvas');
    frame.width = grid;
    frame.height = grid;
    const frameContext = frame.getContext('2d');
    if (!frameContext) return;

    const image = frameContext.createImageData(grid, grid);
    for (let i = 0; i < pixels.length; i += 1) {
      let red;
      let green;
      let blue;
      if (mode === 'diff') {
        const value = Math.max(-1, Math.min(1, pixels[i] / 6));
        [red, green, blue] = value >= 0
          ? [242 * value, 165 * value, 65 * value]
          : [-90 * value, -160 * value, -240 * value];
      } else {
        red = green = blue = 255 * Math.max(
          0,
          Math.min(1, Math.asinh(Math.max(0, pixels[i]) / 1.2) / Math.asinh(35 / 1.2)),
        );
      }
      image.data.set([red, green, blue, 255], i * 4);
    }

    frameContext.putImageData(image, 0, 0);
    context.imageSmoothingEnabled = false;
    context.clearRect(0, 0, canvasElement.width, canvasElement.height);
    context.drawImage(frame, 0, 0, canvasElement.width, canvasElement.height);
    context.strokeStyle = '#d5beff';
    context.lineWidth = 1.5;
    context.beginPath();
    for (const [startX, startY, endX, endY] of [
      [216, 260, 234, 260],
      [286, 260, 304, 260],
      [260, 216, 260, 234],
      [260, 286, 260, 304],
    ]) {
      context.moveTo(startX, startY);
      context.lineTo(endX, endY);
    }
    context.stroke();
    context.fillStyle = '#cbd5e1';
    context.font = '11px monospace';
    context.fillText('N ↑  E ←', 16, 500);
    context.fillStyle = '#d5beff';
    context.fillText('SIMULATED', 420, 22);
  }, [simulation, epoch, band, mode]);

  function load(event) {
    event.preventDefault();
    const rightAscension = parseCoordinate(ra, true);
    const declination = parseCoordinate(dec);
    const nextErrors = {};
    if (rightAscension === null || rightAscension < 0 || rightAscension >= 360) {
      nextErrors.ra = 'Use 0–360 degrees or 0h–24h (exclusive).';
    }
    if (declination === null || declination < -90 || declination > 90) {
      nextErrors.dec = 'Use a declination from −90° to +90°.';
    }
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) return;

    setLoaded({
      ra: rightAscension,
      dec: declination,
      fov: Number(fov),
      name: TARGETS[target] || 'Custom sky patch',
    });
    setPlaying(false);
    setEpoch(0);
  }

  function selectTarget(key) {
    setTarget(key);
    setErrors({});
    const preset = PRESETS[key];
    if (!preset) return;
    setRa(preset.ra);
    setDec(preset.dec);
    setLoaded({ ra: preset.raD, dec: preset.decD, fov: Number(fov), name: TARGETS[key] });
    setEpoch(0);
    setPlaying(false);
  }

  function visit(value) {
    setPlaying(false);
    setEpoch((value + EPOCHS.length) % EPOCHS.length);
  }

  return (
    <div className="cosmos-lab-layout">
      <form className="cosmos-panel cosmos-controls" onSubmit={load}>
        <div className="cosmos-panel-heading">
          <div>
            <span className="cosmos-eyebrow">SIMULATION SETUP</span>
            <h3>Choose your sky</h3>
          </div>
          <span className="cosmos-mini-status">01—06</span>
        </div>

        <label className="cosmos-field-label" htmlFor="sky-target">Test target</label>
        <select id="sky-target" className="cosmos-field" value={target} onChange={event => selectTarget(event.target.value)}>
          {Object.entries(TARGETS).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
          <option value="custom">Custom coordinates</option>
        </select>

        <div className="cosmos-field-group">
          <label className="cosmos-field-label" htmlFor="sky-ra">Right ascension</label>
          <input
            id="sky-ra"
            className="cosmos-field cosmos-monospace"
            value={ra}
            spellCheck="false"
            aria-invalid={Boolean(errors.ra)}
            aria-describedby="sky-ra-error"
            onChange={event => {
              setRa(event.target.value);
              setTarget('custom');
              setErrors(current => ({ ...current, ra: undefined }));
            }}
          />
          <p className="cosmos-field-error" id="sky-ra-error">{errors.ra}</p>
        </div>
        <div className="cosmos-field-group">
          <label className="cosmos-field-label" htmlFor="sky-dec">Declination</label>
          <input
            id="sky-dec"
            className="cosmos-field cosmos-monospace"
            value={dec}
            spellCheck="false"
            aria-invalid={Boolean(errors.dec)}
            aria-describedby="sky-dec-error"
            onChange={event => {
              setDec(event.target.value);
              setTarget('custom');
              setErrors(current => ({ ...current, dec: undefined }));
            }}
          />
          <p className="cosmos-field-error" id="sky-dec-error">{errors.dec}</p>
          <p className="cosmos-helper">Degrees, or 17h57m48.5s and +4d41m36s.</p>
        </div>
        <div className="cosmos-field-group">
          <label className="cosmos-field-label" htmlFor="sky-fov">Field of view</label>
          <select id="sky-fov" className="cosmos-field" value={fov} onChange={event => setFov(event.target.value)}>
            {[3, 6, 10].map(value => <option key={value} value={value}>{value} arcmin</option>)}
          </select>
        </div>
        <button className="cosmos-primary cosmos-submit" type="submit">Show this sky <span aria-hidden="true">↗</span></button>

        <fieldset className="cosmos-fieldset">
          <legend className="cosmos-field-label">Detector</legend>
          <Segmented value={band} onChange={setBand} label="Detector" items={[['all', 'All'], ...BANDS.map((_, index) => [String(index), String(index + 1)])]} />
          <p className="cosmos-helper">{band === 'all' ? 'Average of the six simulated detector bands.' : `${BANDS[Number(band)][0]}–${BANDS[Number(band)][1]} µm`}</p>
        </fieldset>
        <fieldset className="cosmos-fieldset">
          <legend className="cosmos-field-label">View</legend>
          <Segmented
            value={mode}
            onChange={value => {
              setMode(value);
              if (value === 'stack') setPlaying(false);
            }}
            label="Sky view"
            items={[['flip', 'Flipbook'], ['stack', 'Stack'], ['diff', 'Difference']]}
          />
          <p className="cosmos-helper">{MODE_HINTS[mode]}</p>
        </fieldset>
      </form>

      <div className="cosmos-lab-display">
        <div className="cosmos-panel cosmos-image-panel">
          <div className="cosmos-image-heading">
            <span>{loaded.name}</span>
            <span>{loaded.fov}′ × {loaded.fov}′</span>
          </div>
          <canvas
            ref={canvas}
            width={520}
            height={520}
            aria-label={`Simulated ${loaded.name}, ${mode} view, visit ${epoch + 1}`}
            className="cosmos-detector"
          />
        </div>
        <div className="cosmos-panel cosmos-transport">
          <button type="button" className="cosmos-control-button" aria-label="Previous visit" disabled={mode === 'stack'} onClick={() => visit(epoch - 1)}>←</button>
          <button type="button" className="cosmos-primary" aria-label={playing ? 'Pause visits' : 'Play visits'} disabled={mode === 'stack'} onClick={() => setPlaying(value => !value)}>
            {playing ? 'Ⅱ Pause' : '▶ Play'}
          </button>
          <button type="button" className="cosmos-control-button" aria-label="Next visit" disabled={mode === 'stack'} onClick={() => visit(epoch + 1)}>→</button>
          <input type="range" aria-label="Visit" min="0" max={EPOCHS.length - 1} step="1" value={epoch} disabled={mode === 'stack'} onChange={event => visit(Number(event.target.value))} />
          <label className="cosmos-speed">
            Speed
            <select aria-label="Playback speed" value={speed} onChange={event => setSpeed(Number(event.target.value))}>
              {[1, 2, 3, 4, 6].map(value => <option key={value} value={value}>{value} fps</option>)}
            </select>
          </label>
        </div>
        <div className="cosmos-panel cosmos-readouts" aria-live="polite">
          {[
            ['Model date', mode === 'stack' ? 'All visits' : EPOCHS[epoch]],
            ['Visit', mode === 'stack' ? '4 combined' : mode === 'diff' ? `${epoch + 1} minus 1` : `${epoch + 1} of 4`],
            ['Wavelength', band === 'all' ? '0.75–5.00 µm' : `${BANDS[Number(band)][0]}–${BANDS[Number(band)][1]} µm`],
            ['Data source', 'Simulation'],
          ].map(([label, value]) => (
            <div key={label}><span>{label}</span><strong>{value}</strong></div>
          ))}
        </div>
        <SignalGraph spectrum={spectrum} selected={band} isDifference={mode === 'diff'} />
        <p className="cosmos-helper cosmos-simulation-note">
          Dates, motion, and detector images are simulated, not downloaded SPHEREx observations.{' '}
          <a href="https://irsa.ipac.caltech.edu/Missions/spherex.html" target="_blank" rel="noreferrer">Browse the real archive at IRSA ↗</a>
        </p>
      </div>
    </div>
  );
}

function normalise(item) {
  const data = item.data?.[0] || {};
  return {
    id: data.nasa_id || data.title,
    title: data.title || 'Untitled NASA media',
    kind: data.media_type || 'image',
    date: data.date_created?.slice(0, 10) || '',
    credit: data.photographer || data.secondary_creator || `NASA / ${data.center || ''}`,
    description: (data.description || '').replace(/<[^>]*>/g, ''),
    thumbnail: item.links?.find(link => link.render === 'image')?.href?.replace(/^http:/, 'https:')
      || item.links?.[0]?.href?.replace(/^http:/, 'https:'),
  };
}

function MediaDialog({ item, onClose }) {
  const dialog = useRef(null);
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  const [captions, setCaptions] = useState('');

  useEffect(() => {
    const previouslyFocused = document.activeElement;
    if (!dialog.current?.open) dialog.current?.showModal();
    return () => previouslyFocused?.focus();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    async function loadMedia() {
      try {
        const response = await fetch(`https://images-api.nasa.gov/asset/${encodeURIComponent(item.id)}`, { signal: controller.signal });
        if (!response.ok) throw new Error(`NASA media request failed with status ${response.status}`);
        const data = await response.json();
        const urls = data.collection.items.map(entry => entry.href.replace(/^http:/, 'https:'));
        const choices = urls.filter(value => item.kind === 'video'
          ? /\.mp4(?:\?|$)/i.test(value)
          : /\.(jpg|jpeg|png)(?:\?|$)/i.test(value));
        const best = choices.find(value => /~medium\./.test(value))
          || choices.find(value => /~small\./.test(value))
          || choices.find(value => /~large\./.test(value))
          || choices[0];
        if (!best) throw new Error('NASA did not provide a playable file for this media item.');
        setUrl(best);
        setCaptions(urls.find(value => /\.vtt(?:\?|$)/i.test(value)) || '');
      } catch (loadError) {
        if (loadError.name !== 'AbortError') {
          setError('This media could not be loaded. You can still open the original NASA record.');
        }
      }
    }
    loadMedia();
    return () => controller.abort();
  }, [item]);

  return (
    <dialog
      ref={dialog}
      className="cosmos-dialog"
      aria-labelledby="cosmos-media-title"
      onCancel={onClose}
      onClick={event => {
        if (event.target === dialog.current) onClose();
      }}
    >
      <div className="cosmos-dialog-heading">
        <div>
          <p className="cosmos-eyebrow">NASA / {item.kind.toUpperCase()}</p>
          <h2 id="cosmos-media-title">{item.title}</h2>
        </div>
        <button type="button" autoFocus onClick={onClose} aria-label="Close media">×</button>
      </div>
      <div className="cosmos-dialog-media">
        {error
          ? <p role="alert">{error}</p>
          : !url
            ? <p role="status">Loading NASA media…</p>
            : item.kind === 'video'
              ? (
                <video
                  controls
                  playsInline
                  preload="metadata"
                  crossOrigin="anonymous"
                  poster={item.thumbnail}
                  src={url}
                  aria-label={item.title}
                  onError={() => setError('Video playback is unavailable. Open the original NASA record below.')}
                >
                  {captions && <track kind="captions" src={captions} srcLang="en" label="English" />}
                </video>
              )
              : <img src={url} alt={item.title} onError={() => setError('The photo could not load. Open the original NASA record below.')} />}
      </div>
      <div className="cosmos-dialog-copy">
        <p>{item.credit}{item.date ? ` · ${item.date}` : ''}</p>
        <p>{item.description}</p>
        <a href={`https://images.nasa.gov/details/${encodeURIComponent(item.id)}`} target="_blank" rel="noreferrer">View original and downloads at NASA ↗</a>
      </div>
    </dialog>
  );
}

function Library({ initialQuery = 'Earth' }) {
  const isEarth = initialQuery === 'Earth';
  const [query, setQuery] = useState(initialQuery);
  const [request, setRequest] = useState(isEarth ? null : { query: initialQuery, kind: 'all', page: 1 });
  const [kind, setKind] = useState('all');
  const [items, setItems] = useState(isEarth ? featured : []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [page, setPage] = useState(1);
  const [next, setNext] = useState(false);
  const [total, setTotal] = useState(null);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    if (!request) return undefined;
    const controller = new AbortController();
    async function search() {
      setBusy(true);
      setError('');
      try {
        const parameters = new URLSearchParams({
          q: request.query,
          media_type: request.kind === 'all' ? 'image,video' : request.kind,
          page: String(request.page),
          page_size: '12',
        });
        const response = await fetch(`https://images-api.nasa.gov/search?${parameters}`, { signal: controller.signal });
        if (!response.ok) throw new Error(`NASA search failed with status ${response.status}`);
        const result = await response.json();
        setItems(result.collection.items.map(normalise));
        setNext(result.collection.links?.some(link => link.rel === 'next') || false);
        setTotal(result.collection.metadata.total_hits);
        setPage(request.page);
      } catch (searchError) {
        if (searchError.name !== 'AbortError') {
          setError('NASA search is unavailable right now. The previous collection is still shown; try again or visit NASA’s library.');
        }
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    }
    search();
    return () => controller.abort();
  }, [request]);

  function submitSearch(event) {
    event.preventDefault();
    setRequest({ query: query.trim() || 'Earth', kind, page: 1 });
  }

  function changeKind(value) {
    setKind(value);
    setRequest({ query: query.trim() || 'Earth', kind: value, page: 1 });
  }

  return (
    <>
      <form className="cosmos-search-form" onSubmit={submitSearch}>
        <label className="cosmos-sr-only" htmlFor="nasa-query">Search NASA photos and videos</label>
        <input id="nasa-query" className="cosmos-field" placeholder="Earth, aurora, ocean, Blue Marble…" value={query} onChange={event => setQuery(event.target.value)} />
        <button className="cosmos-primary" type="submit">Search NASA <span aria-hidden="true">↗</span></button>
        <Segmented value={kind} onChange={changeKind} label="Media type" items={[['all', 'All media'], ['image', 'Photos'], ['video', 'Videos']]} />
      </form>
      <p role="status" aria-live="polite" className="cosmos-library-status">
        {busy
          ? 'Searching the NASA Image and Video Library…'
          : error || (total === null
            ? 'A curated starting point. Search to explore NASA’s full photo and video library.'
            : `${total.toLocaleString()} results · Page ${page}`)}
      </p>
      <div className={`cosmos-media-grid${busy ? ' is-loading' : ''}`} aria-busy={busy}>
        {items.map(item => (
          <button key={item.id} type="button" className="cosmos-media-card" onClick={() => setSelected(item)}>
            <span className="cosmos-media-thumbnail">
              {item.thumbnail && <img src={item.thumbnail} alt="" loading="lazy" />}
              <span>{item.kind === 'video' ? '▶ VIDEO' : '✦ PHOTO'}</span>
            </span>
            <span className="cosmos-media-copy">
              <span>{item.credit}{item.date ? ` · ${item.date.slice(0, 4)}` : ''}</span>
              <strong>{item.title}</strong>
            </span>
          </button>
        ))}
      </div>
      {!busy && !items.length && <p className="cosmos-empty-state">No results. Try “Earth”, “aurora”, or “ocean”.</p>}
      {total !== null && (
        <div className="cosmos-pagination">
          <button type="button" className="cosmos-control-button" disabled={busy || page <= 1} onClick={() => setRequest({ ...request, page: page - 1 })}>← Previous</button>
          <span>Page {page}</span>
          <button type="button" className="cosmos-control-button" disabled={busy || !next} onClick={() => setRequest({ ...request, page: page + 1 })}>Next →</button>
        </div>
      )}
      <p className="cosmos-library-note">
        Media and metadata are provided by NASA. Check each original record for credits and usage conditions.{' '}
        <a href="https://images.nasa.gov/" target="_blank" rel="noreferrer">Open NASA’s library ↗</a>
      </p>
      {selected && <MediaDialog key={selected.id} item={selected} onClose={() => setSelected(null)} />}
    </>
  );
}

function DataSources() {
  return (
    <div className="cosmos-source-grid">
      {SOURCES.map(([number, name, title, description, url, symbol]) => (
        <a key={name} className="cosmos-source-card" href={url} target="_blank" rel="noreferrer">
          <span className="cosmos-source-top"><span>{number} / {name.toUpperCase()}</span><span>{symbol}</span></span>
          <h3>{title}</h3>
          <p>{description}</p>
          <span className="cosmos-source-link">Open {name} →</span>
        </a>
      ))}
    </div>
  );
}

export default function Explore({ destination = destinations[0], mediaRequest }) {
  const [tab, setTab] = useState('media');
  useEffect(() => {
    if (mediaRequest) setTab('media');
  }, [mediaRequest]);
  useEffect(() => {
    function syncTabToHash() {
      const hashTabs = { '#earth-library': 'media', '#spherex': 'flipbook', '#earth-data': 'data' };
      const nextTab = hashTabs[window.location.hash];
      if (nextTab) {
        setTab(nextTab);
        document.getElementById('explore')?.scrollIntoView();
      }
    }
    syncTabToHash();
    window.addEventListener('hashchange', syncTabToHash);
    return () => window.removeEventListener('hashchange', syncTabToHash);
  }, []);

  const description = (destination.description || '').replace(/<[^>]*>/g, '').replace(/&quot;/g, '"').replace(/&amp;/g, '&');

  return (
    <section id="explore" className="cosmos-explore">
      <Starfield />
      <span id="spherex" className="cosmos-anchor" />
      <span id="earth-library" className="cosmos-anchor" />
      <span id="earth-data" className="cosmos-anchor" />
      <div className="cosmos-content">
        <div className="cosmos-utility-row">
          <a className="cosmos-back" href="#sky-home">↑ CHOOSE ANOTHER LIGHT</a>
          <span className="cosmos-utility-coordinate"><i className="cosmos-status-dot" /> NASA COLLECTION / OPEN ARCHIVES</span>
        </div>

        <header className="cosmos-hero">
          <div className="cosmos-hero-copy">
            <p className="cosmos-kicker"><span>✦</span> THE NASA COLLECTION <i className="cosmos-kicker-line" /></p>
            <h2>Beyond the <em>familiar.</em></h2>
            <p>From our home planet to distant galaxies. Explore NASA imagery, mission stories, and the data behind the discoveries.</p>
            <div className="cosmos-hero-actions">
              <a className="cosmos-primary" href="#earth-library">Explore the collection <span aria-hidden="true">↓</span></a>
              <a className="cosmos-quiet-link" href="#spherex">VISIT THE SPHEREx LAB ↗</a>
            </div>
            <div className="cosmos-hero-facts">
              <div><strong>NASA</strong><span>IMAGERY &amp; VIDEO</span></div>
              <div><strong>6</strong><span>INFRARED BANDS</span></div>
              <div><strong>∞</strong><span>OPEN ARCHIVES</span></div>
            </div>
          </div>
          <div className="cosmos-hero-visual" aria-hidden="true">
            <div className="cosmos-planet-halo" />
            <div className="cosmos-orbit cosmos-orbit-one" />
            <div className="cosmos-orbit cosmos-orbit-two" />
            <div className="cosmos-planet" style={{ backgroundImage: `url("${destination.featureImage || destination.image}")` }} />
            <div className="cosmos-planet-shine" />
            <span className="cosmos-hero-coordinate">DESTINATION / {destination.name.toUpperCase()}</span>
            <div className="cosmos-planet-tag"><span className="cosmos-status-dot" /><span>NOW EXPLORING<strong>{destination.name}</strong></span></div>
          </div>
        </header>

        <div className="cosmos-divider" aria-hidden="true">✦</div>

        <article className="cosmos-destination-card">
          <div className="cosmos-feature-image">
            <img src={destination.featureImage || destination.image} alt={destination.title || `${destination.name} NASA image`} />
            <span className="cosmos-image-label">{destination.featureType || destination.type}</span>
          </div>
          <div className="cosmos-feature-copy">
            <span className="cosmos-eyebrow">DESTINATION / {destination.name.toUpperCase()}</span>
            <h3>{destination.title || destination.name}</h3>
            <p>{description}</p>
            <div className="cosmos-feature-meta">
              <span>{destination.credit || 'NASA'}{destination.date ? ` · Archive date ${destination.date}` : ''}</span>
              <a href={destination.source} target="_blank" rel="noreferrer">Full NASA record ↗</a>
            </div>
          </div>
        </article>

        <div className="cosmos-mission-heading">
          <span className="cosmos-eyebrow">OBSERVATORY / MISSION CONTROL</span>
          <h2>Choose your <em>horizon.</em></h2>
          <p>Explore the NASA media library, a simulated infrared sky, and the mission archives.</p>
        </div>
        <div className="cosmos-tab-bar" role="tablist" aria-label="Observatory sections">
          {TABS.map(({ id, symbol, title, caption }, index) => (
            <button
              key={id}
              id={`tab-${id}`}
              type="button"
              role="tab"
              tabIndex={tab === id ? 0 : -1}
              aria-controls={`panel-${id}`}
              aria-selected={tab === id}
              className={tab === id ? 'is-active' : ''}
              onClick={() => setTab(id)}
              onKeyDown={event => {
                let next;
                if (event.key === 'ArrowRight') next = TABS[(index + 1) % TABS.length].id;
                if (event.key === 'ArrowLeft') next = TABS[(index + TABS.length - 1) % TABS.length].id;
                if (event.key === 'Home') next = TABS[0].id;
                if (event.key === 'End') next = TABS[TABS.length - 1].id;
                if (next) {
                  event.preventDefault();
                  setTab(next);
                  document.getElementById(`tab-${next}`)?.focus();
                }
              }}
            >
              <span className="cosmos-tab-symbol" aria-hidden="true">{symbol}</span>
              <span className="cosmos-tab-copy"><strong>{title}</strong><small>{caption}</small></span>
            </button>
          ))}
        </div>
        <div className="cosmos-tab-panel" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
          {tab === 'media'
            ? <Library key={`${destination.id}-${mediaRequest?.id || ''}`} initialQuery={mediaRequest?.query || destination.query || destination.name} />
            : tab === 'flipbook'
              ? (
                <>
                  <div className="cosmos-panel-intro">
                    <p>Explore a changing infrared sky. Blink between four model visits, isolate a detector band, or compare what changes.</p>
                    <span className="cosmos-mini-status">PROTOTYPE · SIMULATED DATA</span>
                  </div>
                  <Flipbook />
                </>
              )
              : <DataSources />}
        </div>

        <footer className="cosmos-footer">
          <span>NASA STAR EXPLORER / A SKY FULL OF POSSIBILITIES</span>
          <span>NASA MEDIA · SIMULATED SPHEREx DATA · OPEN SCIENCE</span>
        </footer>
      </div>
    </section>
  );
}