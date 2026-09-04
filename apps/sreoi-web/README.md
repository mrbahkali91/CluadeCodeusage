# @sreoi/web

React + Vite client for the Saudi Real Estate Opportunity Intelligence
platform. Talks to `@sreoi/api`; in development Vite proxies `/api` and `/auth`
so the session cookie is same-origin and needs no CORS credential dance.

```bash
pnpm --filter @sreoi/web dev     # http://127.0.0.1:5173
```

## Decisions worth knowing

**No `@vitejs/plugin-react`.** It pulls in `@babel/core`, which this
workspace's `trustPolicy: no-downgrade` refuses — an earlier published version
carried a provenance attestation and the current one does not. Weakening a
supply-chain policy to gain a build-time convenience is the wrong trade, and
Vite's own esbuild transform compiles the automatic JSX runtime without it. The
cost is React Fast Refresh: editing a component reloads the page instead of
preserving state.

**The map has no third-party basemap.** Tiles from a public provider would send
every viewport a user pans to — effectively, which districts an investor is
studying — to a third party. District polygons come from the platform's own
PostGIS geometry instead, which orients the reader and leaks nothing. Because
there is no glyph server, labels are DOM markers rather than symbol layers: a
symbol layer renders nothing at all without glyphs.

**`glyphs` is absent from the style object, not set to `undefined`.** MapLibre
validates the style and rejects an explicit `glyphs: undefined` with
"glyphs: string expected, undefined found", which aborts the style load and
leaves a blank canvas with only a console error to explain it.

**An unknown classification never renders as a recommendation.** `INSUFFICIENT_DATA`,
`null` and any future value the engine adds all render in the refused tone, so a
classification this client has not been taught about cannot arrive looking like
a buy signal.

**Arabic is a first-class locale, not a translation.** Direction is set on
`<html>` so the whole document flips, numbers render in Arabic-Indic digits, and
the vocabulary is real estate Arabic (تنازل, مزاد) rather than transliteration.
