# NASA Star Explorer

React + Tailwind CSS v4 + Three.js, built with Vite on Node.js.

## Run locally

Use Node.js 22.12 or later (an LTS release is recommended).

```
npm ci
npm run dev
```

## Production build

```
npm run build
npm start
```

`npm start` previews the production build using Node.js/Vite. Deploy the generated `dist/` directory to a static web host. No backend or database is needed for this landing page.

Layout, colors, responsive states, star effects, and transitions use Tailwind utilities in `src.jsx`. `style.css` contains Tailwind imports, theme tokens, and shared base styles. `vite.config.js` enables the official Tailwind Vite plugin.

The star field is illustrative, not a live astronomical chart. Star information links to NASA. NASA logo: https://www.nasa.gov/wp-content/themes/nasa/assets/images/nasa-logo.svg . Earth texture: Three.js example asset (NASA Blue Marble imagery). Google Fonts: DM Sans and Manrope.

For an existing Laravel/Vite app, copy the React entry, Tailwind stylesheet, and public assets, and add the Tailwind Vite plugin to the existing Vite configuration.

## SPHEREx prototype and NASA media

- `Explore.jsx`: React/Tailwind flipbook, NASA image/video search, media dialogs, and external data-source directory.
- `sky-engine.js`: deterministic simulated sky adapted from the supplied SPHEREx prototype. It does not download real SPHEREx observations. Dates, motion, spectra, and frames are simulated.
- `sky-engine.test.js`: coordinate validation and numerical stack/difference checks. Run `node --test sky-engine.test.js`.
- `nasa-featured.json`: curated real NASA media records retrieved from NASA's public API.

Photo/video searches call https://images-api.nasa.gov directly using its supported browser CORS access. Videos stream from NASA; large media are not bundled. Network access is needed for media and searches. Errors retain the existing collection and link to the original NASA record. Media metadata and credits are shown in the viewer.

Earthdata, Worldview, NASA Earth Science, and IRSA are external data destinations. The page does not ingest or claim to contain all NASA datasets. Some external data services require their own login.

## Destination travel

The landing-page selector switches among Earth, Sun, Mars, Jupiter, Saturn, Kepler-186f, a quasar concept, Orion Nebula, and Andromeda. `objects.json` preserves NASA record titles, descriptions, credits, dates, and source URLs. Images are bundled in `public/objects/`. Earth is a 3D textured globe; other destinations display NASA imagery. Kepler and quasar artwork is explicitly labeled as artist concepts. This is an illustrative travel interface, not a scale model or live navigation system.

The NASA media library automatically searches the selected destination. All datasets, observatory images, and science measurements remain at their original archives; the SPHEREx flipbook is still a simulation, not real time-series cutouts.

## Clickable sky

The destination bars are replaced by 19 named sky markers. Selecting a marker centers the object and opens its information panel. The sky contains all eight solar-system planets, Sun, Moon, three Kepler worlds, a quasar concept, Orion Nebula, Andromeda, Sirius, Vega, and Polaris. The 1,300 background stars and marker positions are decorative rather than observed coordinates. Stellar close-ups are explicitly illustrated glows; planet/nebula/galaxy images and concepts link to their NASA source records.
