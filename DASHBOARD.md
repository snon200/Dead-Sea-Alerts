# Dead Sea risk dashboard

## Purpose

Help users explore possible ground subsidence around the Dead Sea and its relationship to nearby infrastructure. The dashboard supports the team's NASA Space Apps challenge entry; it is not an operational hazard warning system.

## Current implementation

The dashboard uses vanilla JavaScript and Leaflet. Use `vercel dev` from the repository root to serve the frontend and Python API together. A static server cannot execute the API. Leaflet, OpenStreetMap tiles, and infrastructure features require internet access.

The interface has three regions:

- **Risk zones:** a collapsible sidebar with an independently scrollable list, ordered from highest to lowest risk, of risk levels, displacement rates, areas, and coordinate ranges. Use the arrow at the panel edge to collapse or reopen it. Layer controls remain below the list and toggle infrastructure individually or all at once. With all layers selected, the list and map show all zones; otherwise they show zones intersecting any selected layer, and with no layers selected none are shown. Filtering uses successfully loaded OSM data, so zones awaiting data or with failed requests are omitted and a notice is shown. Selected-zone details remain available even if that zone is filtered out.
- **Map:** an OpenStreetMap basemap with color-coded, selectable risk polygons. At lower zoom levels, clickable labels identify the small zones; labels disappear when zoomed in to keep infrastructure visible. The initial view covers the Dead Sea study area; panning is constrained around it, while surrounding context can appear at lower zoom.
- **Zone details:** a collapsible panel with the selected zone's measurements, infrastructure context, displacement trend, and data provenance. Use the arrow at the panel edge to collapse or reopen it. Only categories with mapped features are listed; an empty-state message appears if none are found. Measurement inputs are collapsed by default and include a brief explanation; information buttons explain the individual metrics and inputs.
- On narrow screens, opening one panel closes the other so their controls remain accessible.

On load, the dashboard requests `GET /api/v1/zones` and displays loading, empty and error states. The API export includes only detections from the latest acquisition interval, with the full displacement history for each retained footprint; earlier detections remain in the pipeline outputs. If the request fails, reload the page to retry. API results support Polygon and MultiPolygon geometry, missing measurements and dated cumulative series; zone names currently use IDs. The basemap is separate from the overlays. Results are exploratory and are not an operational hazard assessment.

Measurements and coordinates are rounded to two decimal places for display. Coordinates marked with `~` are approximate; map geometry and feature locations retain their original precision.

For local development, `python scripts/dashboard_server.py` serves the dashboard and the real zones API on `127.0.0.1:8000`. Set `ZONE_DATA_SOURCE=demo` before running it to use the nine synthetic demonstration zones instead. In PowerShell:

```powershell
$env:ZONE_DATA_SOURCE = "demo"
python scripts/dashboard_server.py
```

Clear the variable (`Remove-Item Env:ZONE_DATA_SOURCE`) to return to the default API source. In a POSIX shell, use `ZONE_DATA_SOURCE=demo python scripts/dashboard_server.py`. The local server binds only to localhost; Vercel deployments always use the real API, regardless of this variable. Demo zone names, values, classifications, and dates are synthetic; infrastructure features still come from OpenStreetMap.

### Infrastructure data

The dashboard loads the precomputed `data/api/osm-cache.json` after the zones load. Each cache entry includes its exact zone geometry; a new zone, changed geometry, missing element or invalid cache is reported as unavailable instead of showing stale results. Valid zones remain visible, and Retry rechecks unsuccessful entries. Production does not fall back to per-visitor OSM API calls, avoiding request bursts and rate limits. The local demo uses live OSM requests because its synthetic zones are not in the production cache.

After changing `data/api/zones.json`, regenerate the cache from the repository root with `python scripts/build_osm_cache.py` and commit both files together. The builder makes a bounded Overpass query for the zone area, intersects supported features with each polygon, and fails if the response is incomplete. Until it is rebuilt, affected zones explicitly show infrastructure as unavailable. Regenerate periodically as well to refresh OSM changes; geometry validation does not detect changes in OpenStreetMap itself.

Overlays include roads, pumping stations, buildings, bridges, power lines and substations, pipelines and water facilities or reservoirs, and hotels and selected public facilities. Only features intersecting a zone are selected, but returned ways can extend outside the zone. Pipelines may carry substances other than water, or have no substance recorded. The selected-zone panel lists intersecting features with clickable coordinates that locate them on the map; intersections do not establish damage. Features can appear in more than one category, and separate OSM objects may represent the same site. Data may be incomplete or outdated, including where a feature is represented as a relation. Layer counts show unique OSM objects loaded across zones. Basemap features are part of the tiles and remain visible when overlays are hidden.

The local demo's live requests use the OSM editing API, which [is not intended for read-only applications](https://operations.osmfoundation.org/policies/api/); it is not a production data source.

### Scientific data

The [NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/availability-overview/) describes publicly available products and their maturity levels. An [ASF search at a Dead Sea point](https://api.daac.asf.alaska.edu/services/search/param?platform=NISAR&intersectsWith=POINT%2835.4%2031.4%29&output=geojson&maxResults=1) returned an L1 RSLC granule footprint; this is not a ready-to-display displacement or risk layer. Coverage, acquisition pairs, access and processing must be checked before interpreting results. No validated, usable Dead Sea sinkhole inventory has been integrated.

## Plan

1. Investigate reliable access to OpenStreetMap infrastructure data, considering request limits, caching, and a preprocessed regional dataset if appropriate.
2. Identify and obtain suitable NISAR observations for the study area through NASA Earthdata or ASF, and establish a reproducible processing pipeline.
3. Derive and validate displacement time series and regional subsidence zones, recording acquisition dates, units, uncertainty, and data provenance.
4. Develop and validate risk scenarios and infrastructure exposure calculations against relevant research, including [Nof et al. (2019)](https://doi.org/10.3390/rs11010089). Do not treat current results as research-validated thresholds.
5. Continue connecting the details panel to the inputs and calculations behind each result.

See the [product requirements document](./product_requirements_document_prd_project_hail_miriam%20%281%29.md) for the broader product vision and [README](./README.md) for the challenge and data resources. Scientific claims and proposed thresholds in the PRD require validation before operational use.
