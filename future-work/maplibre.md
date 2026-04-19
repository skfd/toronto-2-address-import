# Switch map rendering from Leaflet to MapLibre

Status: **proposed, not implemented**. Captured 2026-04-19.

## Current state

Review UI uses Leaflet 1.9.4 loaded from unpkg:

- `t2/web/templates/base.html` — CDN `<link>`/`<script>` tags, map container CSS.
- `t2/web/templates/_review_detail.html` — per-candidate map (`L.map(...)`).
- `t2/web/templates/_ranges_detail.html` — per-range map.
- `t2/web/templates/run.html` — run overview map (tooltip styling).

All three templates construct a Leaflet map directly against a raster tile
layer and drop markers/tooltips for the candidate, OSM nodes, and range
endpoints.

## Proposal

Replace Leaflet with MapLibre GL JS. Motivation to flesh out before building:

- Vector tiles for crisper rendering at zoom 18+ where most review happens.
- Better control over styling (e.g. highlighting matched vs. unmatched OSM
  nodes via paint expressions rather than per-marker HTML).
- Single rendering stack if we ever add heatmap / clustering views.

## Open questions

- Tile source: self-host, or use a hosted vector tile provider (cost,
  attribution, offline dev)?
- Marker/tooltip parity: Leaflet's `L.marker` + `bindTooltip` is terse;
  MapLibre needs symbol layers or HTML `Marker` objects — confirm the
  review UX survives the translation.
- Bundle size and CDN availability vs. current zero-build setup.
