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

- **Risk zones:** a collapsible list with risk levels, displacement rates, areas, and coordinate ranges. Layer controls toggle roads, pumping stations, and residential buildings.
- **Map:** an OpenStreetMap basemap with color-coded, selectable risk polygons. The initial view covers the Dead Sea study area; panning is constrained around it, while surrounding context can appear at lower zoom.
- **Zone details:** a collapsible panel with the selected zone's measurements, infrastructure context, displacement trend, and data provenance.

The four zones, polygons, classifications, time series, and measurement values are illustrative fixtures embedded in `index.html`. They are **not NISAR observations, validated calculations, or actionable risk assessments**. The basemap itself is separate from the infrastructure overlay.

### Infrastructure data

The dashboard queries OpenStreetMap features through the Overpass API. Roads and pumping stations are requested for the study area once per page load; residential building footprints are requested on demand at higher zoom. Successful results are cached in memory until the page is reloaded. If a request fails, use the Retry button; the basemap and illustrative zones remain available.

The public Overpass endpoint has timed out in the current development environment. A curl check resolved its hostname but could not establish an HTTPS connection; the cause remains unconfirmed. Live infrastructure retrieval has therefore not been verified in this environment.

## Plan

1. Investigate reliable access to OpenStreetMap infrastructure data, considering request limits, caching, and a preprocessed regional dataset if appropriate.
2. Identify and obtain suitable NISAR observations for the study area through NASA Earthdata or ASF, and establish a reproducible processing pipeline.
3. Derive and validate displacement time series and regional subsidence zones, recording acquisition dates, units, uncertainty, and data provenance.
4. Develop and validate risk scenarios and infrastructure exposure calculations against relevant research, including [Nof et al. (2019)](https://doi.org/10.3390/rs11010089). Do not treat the current fixture values or classifications as research-validated thresholds.
5. Replace the illustrative zones and measurements with processed data and connect the details panel to the inputs and calculations behind each result.

See the [product requirements document](./product_requirements_document_prd_project_hail_miriam%20%281%29.md) for the broader product vision and [README](./README.md) for the challenge and data resources. Scientific claims and proposed thresholds in the PRD require validation before operational use.
