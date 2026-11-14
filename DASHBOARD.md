# Dead Sea risk dashboard

## Purpose

Help users explore possible ground subsidence around the Dead Sea and its relationship to nearby infrastructure. The dashboard supports the team's NASA Space Apps challenge entry; it is not an operational hazard warning system.

## Current implementation

The dashboard is a single-page application in `index.html`, using vanilla JavaScript and Leaflet. Serve the repository over HTTP and open `index.html` in a browser, for example:

```sh
python -m http.server 8000
```

Open `http://localhost:8000/index.html`. Leaflet, OpenStreetMap tiles, and infrastructure features require internet access.

The interface has three regions:

- **Risk zones:** a collapsible sidebar with an independently scrollable list, ordered from highest to lowest risk, of risk levels, displacement rates, areas, and coordinate ranges. Layer controls remain below the list and toggle infrastructure individually or all at once. With all layers selected, the list and map show all zones; otherwise they show zones intersecting any selected layer, and with no layers selected none are shown. Filtering uses successfully loaded OSM data, so zones awaiting data or with failed requests are omitted and a notice is shown. Selected-zone details remain available even if that zone is filtered out.
- **Map:** an OpenStreetMap basemap with color-coded, selectable risk polygons. At lower zoom levels, clickable labels identify the small zones; labels disappear when zoomed in to keep infrastructure visible. The initial view covers the Dead Sea study area; panning is constrained around it, while surrounding context can appear at lower zoom.
- **Zone details:** a collapsible panel with the selected zone's measurements, infrastructure context, displacement trend, and data provenance. Only categories with mapped features are listed; an empty-state message appears if none are found. Measurement inputs are collapsed by default and include a brief explanation; information buttons explain the individual metrics and inputs.

The nine zones, polygons, classifications, time series, and measurement values are illustrative fixtures embedded in `index.html`. Each polygon covers approximately 0.1–0.2 km²; displayed areas are calculated from the coordinates. They are **not NISAR observations, validated calculations, or actionable risk assessments**. Some zones were placed around mapped features solely to test the interface; their risk levels do not describe the actual sites. The basemap itself is separate from the overlays.

### Infrastructure data

The dashboard requests OpenStreetMap map data for each risk polygon's bounding box once per page load. Overlays include roads, pumping stations, buildings, bridges, power lines and substations, pipelines and water facilities or reservoirs, and hotels and selected public facilities. Only features intersecting a zone are selected, but returned ways can extend outside the zone. Pipelines may carry substances other than water, or have no substance recorded. The selected-zone panel lists intersecting features with clickable coordinates that locate them on the map; intersections do not establish damage. Features can appear in more than one category, and separate OSM objects may represent the same site. One of the mapped pumping-station objects has an OSM description identifying it as abandoned. Data may be incomplete or outdated, including where the map API does not return a crossing way or a feature is represented as a relation. Layer counts show unique OSM objects loaded across zones, including intersecting linear features. Basemap features are part of the tiles and remain visible when overlays are hidden. Successful results are cached in memory until the page is reloaded. If a request fails, Retry requests only unsuccessful zones; successful zones, the basemap, and illustrative zones remain available.

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
