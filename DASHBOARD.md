# Dead Sea risk dashboard

## Purpose

Help users explore possible ground subsidence around the Dead Sea and its relationship to nearby infrastructure. The dashboard supports the team's NASA Space Apps challenge entry; it is not an operational hazard warning system.

## Current implementation

The dashboard uses vanilla JavaScript and Leaflet. Use `vercel dev` from the repository root to serve the frontend and Python API together. A static server can be used to explore the illustrative demo, but cannot execute the API:

```sh
python -m http.server 8000
```

Open `http://localhost:8000/index.html` and select **Show illustrative demo** after the API request fails. Leaflet, OpenStreetMap tiles, and infrastructure features require internet access.

The interface has three regions:

- **Risk zones:** a collapsible sidebar with an independently scrollable list, ordered from highest to lowest risk, of risk levels, displacement rates, areas, and coordinate ranges. Use the arrow at the panel edge to collapse or reopen it. Layer controls remain below the list and toggle infrastructure individually or all at once. With all layers selected, the list and map show all zones; otherwise they show zones intersecting any selected layer, and with no layers selected none are shown. Filtering uses successfully loaded OSM data, so zones awaiting data or with failed requests are omitted and a notice is shown. Selected-zone details remain available even if that zone is filtered out.
- **Map:** an OpenStreetMap basemap with color-coded, selectable risk polygons. At lower zoom levels, clickable labels identify the small zones; labels disappear when zoomed in to keep infrastructure visible. The initial view covers the Dead Sea study area; panning is constrained around it, while surrounding context can appear at lower zoom.
- **Zone details:** a collapsible panel with the selected zone's measurements, infrastructure context, displacement trend, and data provenance. Use the arrow at the panel edge to collapse or reopen it. Only categories with mapped features are listed; an empty-state message appears if none are found. Measurement inputs are collapsed by default and include a brief explanation; information buttons explain the individual metrics and inputs.
- On narrow screens, opening one panel closes the other so their controls remain accessible.

On load, the dashboard requests `GET /api/v1/zones`. It displays loading, empty and error states and offers retry; it does not silently substitute fixtures. The API calculations are not connected yet, so it currently returns 503. Users may explicitly choose the labeled illustrative demo. Its nine zones, polygons, classifications, time series, and measurements are embedded fixtures, **not NISAR observations, validated calculations, or actionable risk assessments**. Some zones were placed around mapped features solely to test the interface; their risk levels do not describe the actual sites. API results support Polygon and MultiPolygon geometry, missing measurements and dated cumulative series; zone names currently use IDs. The basemap is separate from the overlays.

### Infrastructure data

The dashboard requests OpenStreetMap map data for each risk polygon's bounding box after zones load. Overlays include roads, pumping stations, buildings, bridges, power lines and substations, pipelines and water facilities or reservoirs, and hotels and selected public facilities. Only features intersecting a zone are selected, but returned ways can extend outside the zone. Pipelines may carry substances other than water, or have no substance recorded. The selected-zone panel lists intersecting features with clickable coordinates that locate them on the map; intersections do not establish damage. Features can appear in more than one category, and separate OSM objects may represent the same site. One of the mapped pumping-station objects has an OSM description identifying it as abandoned. Data may be incomplete or outdated, including where the map API does not return a crossing way or a feature is represented as a relation. Layer counts show unique OSM objects loaded across zones, including intersecting linear features. Basemap features are part of the tiles and remain visible when overlays are hidden. Successful results are cached in memory until the page is reloaded or the zone source changes. If a request fails, Retry requests only unsuccessful zones; successful zones and the basemap remain available.

The [OpenStreetMap API policy](https://operations.osmfoundation.org/policies/api/) states that its editing API is not intended for read-only applications. This limited request is not a suitable long-term or production data source; use an appropriate read-only provider or regional extract before wider deployment. The previously used Overpass endpoint could not be reached reliably in the current development environment.

### Scientific data

The [NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/availability-overview/) describes publicly available products and their maturity levels. An [ASF search at a Dead Sea point](https://api.daac.asf.alaska.edu/services/search/param?platform=NISAR&intersectsWith=POINT%2835.4%2031.4%29&output=geojson&maxResults=1) returned an L1 RSLC granule footprint; this is not a ready-to-display displacement or risk layer. Coverage, acquisition pairs, access and processing must be checked before interpreting results. No validated, usable Dead Sea sinkhole inventory has been integrated.

## Plan

1. Investigate reliable access to OpenStreetMap infrastructure data, considering request limits, caching, and a preprocessed regional dataset if appropriate.
2. Identify and obtain suitable NISAR observations for the study area through NASA Earthdata or ASF, and establish a reproducible processing pipeline.
3. Derive and validate displacement time series and regional subsidence zones, recording acquisition dates, units, uncertainty, and data provenance.
4. Develop and validate risk scenarios and infrastructure exposure calculations against relevant research, including [Nof et al. (2019)](https://doi.org/10.3390/rs11010089). Do not treat the current fixture values or classifications as research-validated thresholds.
5. Replace the illustrative zones and measurements with processed data and connect the details panel to the inputs and calculations behind each result.

See the [product requirements document](./product_requirements_document_prd_project_hail_miriam%20%281%29.md) for the broader product vision and [README](./README.md) for the challenge and data resources. Scientific claims and proposed thresholds in the PRD require validation before operational use.
