# Dead Sea risk dashboard

## Purpose

Help users explore possible ground subsidence around the Dead Sea and its relationship to nearby infrastructure. The dashboard supports the team's NASA Space Apps challenge entry; it is not an operational hazard warning system.

## Current implementation

The dashboard uses vanilla JavaScript and Leaflet. Use `vercel dev` from the repository root to serve the frontend and Python API together. A static server cannot execute the API. Leaflet, OpenStreetMap tiles, and infrastructure features require internet access.

The interface has three regions:

- **Risk zones:** a collapsible sidebar divided equally between independently scrollable risk-zone and map-layer sections. The zone list is ordered from highest to lowest risk and shows risk levels, displacement rates, areas, and coordinate ranges. Use the arrow at the panel edge to collapse or reopen it. Layer controls toggle infrastructure individually or all at once. With all layers selected, the list and map show all zones; otherwise they show zones intersecting any selected layer, and with no layers selected none are shown. Filtering uses available OSM data; zones awaiting data, with failed requests, or without known matches are omitted. A notice warns when the regional coverage is incomplete. Selected-zone details remain available even if that zone is filtered out.
- **Map:** an OpenStreetMap basemap with color-coded, selectable risk polygons and clickable zone dots. Zone names appear beside the dots at zoom level 11 and above; selecting a dot highlights its sidebar entry and zooms to it. The initial view covers the Dead Sea study area; panning is constrained around it, while surrounding context can appear at lower zoom.
- **Zone details:** the panel and its tab are hidden until a zone is selected. Selecting a zone opens its measurements, infrastructure context, displacement trend, and data provenance. Once selected, moving the mouse near the right edge also opens the panel, and moving out closes it. The panel-edge pin is visible only while the panel is open; use it to keep the panel open, then use it again to close the panel. On touch devices, select a zone to reopen the panel. Only categories with mapped features are listed; an empty-state message appears if none are found. Measurement inputs are collapsed by default and include a brief explanation; information buttons explain the individual metrics and inputs.
- On narrow screens, opening one panel closes the other so their controls remain accessible.

**AI interfaces:** Ask AI opens a floating card below its button and shows scripted responses selected by keywords, with the loaded zone count and selected zone when available; questions stay in the browser and are not sent to an AI service. The selected-zone AI explanation uses a fixed template populated with the displayed risk, its adjustment, and available displacement history. Both simulate loading; neither is AI-generated analysis or safety guidance.

On load, the dashboard requests `GET /api/v1/zones` and displays a loading indicator in the risk-zone sidebar, followed by the data or an empty or error state. The map-layer list is hidden while infrastructure loads, and loading is also indicated in selected-zone details. The API export includes only detections from the latest acquisition interval, with the full displacement history for each retained footprint; earlier detections remain in the pipeline outputs. If the request fails, reload the page to retry. API results support Polygon and MultiPolygon geometry, missing measurements and dated cumulative series. The frontend names each zone after the nearest named OpenStreetMap place within 10 km of its bounding-box center. Names are approximate and do not imply that a zone is inside the named place; numbered labels distinguish zones near the same place. Zones without a nearby place, or outside the dataset's coverage, retain their IDs. The basemap is separate from the overlays. Results are exploratory and are not an operational hazard assessment.

Zone names show “Loading risk zone...” while place names load; if the place-name data is unavailable, zone IDs are shown instead.

Measurements and coordinates are rounded to two decimal places for display. Coordinates marked with `~` are approximate. Zone outlines are smoothed for display; measurements, filtering and infrastructure intersections use the original geometry. Feature locations retain their original precision.

The dashboard derives its displayed risk from the API's base risk: a mapped infrastructure intersection raises it one level, while confirmed absence within the regional dataset lowers it one level. The scale is low → watch → elevated → high and is capped at both ends. This uses all supported infrastructure categories regardless of layer visibility; the separate known-hazards layer is not infrastructure. A known intersection can raise risk even with partial coverage, but incomplete or unavailable OSM data cannot establish absence, so the base is retained. An unavailable API risk remains unavailable. The selected-zone panel explains the base and adjustment. These are experimental heuristics, not validated safety guidance.

For local development, `python scripts/dashboard_server.py` serves the dashboard and the real zones API on `127.0.0.1:8000`. Set `ZONE_DATA_SOURCE=demo` before running it to use the nine synthetic demonstration zones instead. In PowerShell:

```powershell
$env:ZONE_DATA_SOURCE = "demo"
python scripts/dashboard_server.py
```

Clear the variable (`Remove-Item Env:ZONE_DATA_SOURCE`) to return to the default API source. In a POSIX shell, use `ZONE_DATA_SOURCE=demo python scripts/dashboard_server.py`. The local server binds only to localhost; Vercel deployments always use the real API, regardless of this variable. Demo zone names, values, classifications, and dates are synthetic; infrastructure features still come from OpenStreetMap.

### Infrastructure data

The dashboard requests `GET /api/v1/osm`, which serves the precomputed `data/api/osm-cache.json` and `data/api/osm-places.json` in bounded pages. Infrastructure is collected for the fixed polygon in `data/api/osm-study-area.geojson`, independent of zone IDs or geometries. The browser intersects the current zones with the regional features; changing zones does not invalidate the OSM data. Regional overlays also load if zones are empty or unavailable; counts are unavailable if the zones request fails. A zone partly or entirely outside the polygon can have missing matches: its details and the layer counts say results are partial, and filtering may omit it. Retry rechecks the API. Production never makes per-visitor Overpass requests.

Regenerate both OSM datasets periodically, or after changing the study polygon, and deploy them together only after both builders succeed. A zone-only export does not require OSM regeneration. The place-name dataset covers the polygon's bounding box plus a 10 km search margin; zones outside that coverage retain their IDs. Neither dataset is updated automatically. See the [pipeline guide](./NISAR_PIPELINE_GUIDE.md) for the manual build and deployment sequence.

Area names use the separate `data/api/osm-places.json` regional dataset returned by the OSM API. If it does not fully cover a zone's 10 km search radius, the dashboard displays the zone ID instead. The initial data is from the OpenStreetMap Geofabrik Israel/Palestine and Jordan extracts; attribution is shown on the map.

Overlays include roads, pumping stations, buildings, bridges, power lines and substations, pipelines and water facilities or reservoirs, and hotels and selected public facilities. All supported nodes and ways intersecting the study polygon are displayed, even outside zones; ways are not clipped and can extend outside the polygon. Pipelines may carry substances other than water, or have no substance recorded. The selected-zone panel lists intersecting features with clickable coordinates that locate them on the map; intersections do not establish damage. Features can appear in more than one category, and separate OSM objects may represent the same site. Data may be incomplete or outdated, including where a feature is represented as a relation. Layer counts deduplicate OSM objects intersecting the current zones, not all features drawn. Basemap features are part of the tiles and remain visible when overlays are hidden.

The local synthetic demo uses live Overpass requests because its zones are not in the production cache. These requests depend on a third-party service and may be slow or rate-limited.

### Scientific data

The [NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/availability-overview/) describes publicly available products and their maturity levels. An [ASF search at a Dead Sea point](https://api.daac.asf.alaska.edu/services/search/param?platform=NISAR&intersectsWith=POINT%2835.4%2031.4%29&output=geojson&maxResults=1) returned an L1 RSLC granule footprint; this is not a ready-to-display displacement or risk layer. Coverage, acquisition pairs, access and processing must be checked before interpreting results. No validated, usable Dead Sea sinkhole inventory has been integrated.

## Plan

1. Investigate reliable access to OpenStreetMap infrastructure data, considering request limits, caching, and a preprocessed regional dataset if appropriate.
2. Identify and obtain suitable NISAR observations for the study area through NASA Earthdata or ASF, and establish a reproducible processing pipeline.
3. Derive and validate displacement time series and regional subsidence zones, recording acquisition dates, units, uncertainty, and data provenance.
4. Develop and validate risk scenarios and infrastructure exposure calculations against relevant research, including [Nof et al. (2019)](https://doi.org/10.3390/rs11010089). Do not treat current results as research-validated thresholds.
5. Continue connecting the details panel to the inputs and calculations behind each result.

See the [product requirements document](./product_requirements_document_prd_project_hail_miriam%20%281%29.md) for the broader product vision and [README](./README.md) for the challenge and data resources. Scientific claims and proposed thresholds in the PRD require validation before operational use.
