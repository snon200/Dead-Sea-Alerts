# Zones API: backend integration guide

This guide is for connecting the NISAR processing pipeline to the dashboard. The
[OpenAPI specification](./openapi.json) is the authoritative response contract;
open it in Swagger Editor to inspect the full schema and example.

## Current status

The static dashboard and Python function share a Vercel domain. The dashboard
requests the API on load. The endpoint reads a precomputed zones export; if the
export is unavailable it returns HTTP 503. The example in the specification
contains fictional values, not results from the pipeline.
The export includes only detections last observed on the latest acquisition
date; each retained zone still includes its full displacement history.

```text
Processed NISAR data -> zones.json -> GET /api/v1/zones -> dashboard
fixed study polygon + OSM -> precomputed OSM files -> GET /api/v1/osm -> dashboard
```

`GET /api/v1/osm` returns `{"schema_version": "1.0", "infrastructure": {...}, "places": {...}, "pagination": {...}}`,
containing one page of the precomputed infrastructure and the area-name dataset. It
returns 503 if either file is missing and 500 if the data cannot be read or is
malformed. No OSM network request or cache regeneration occurs in the handler.
Responses are limited to 3 MiB. Start without parameters, then request each
`pagination.next_offset` with `?offset=<next_offset>&revision=<revision>` from
the first page until `next_offset` is `null`. Combine the pages' element maps.
`pagination.total` is the number of elements across all pages. A changed
dataset returns 409 and requires restarting at the first page; invalid
pagination parameters return 400.
Infrastructure schema `3.0` includes a GeoJSON coverage polygon and regional
elements keyed by `type/id`, rather than per-zone entries. The browser computes
intersections against the current zones; intersections may be incomplete outside
the polygon. OSM generation is manual and independent of zone exports. Refresh
both files periodically and after changing the polygon; see the
[pipeline guide](./NISAR_PIPELINE_GUIDE.md).

Relevant files:

- [`api/v1/zones.py`](./api/v1/zones.py): Vercel HTTP handler. It calls
  `get_zones()`, wraps the result as `{"schema_version": "1.0", "zones": [...]}`,
  and sends JSON.
- [`api/v1/osm.py`](./api/v1/osm.py): Vercel HTTP handler for both precomputed
  OSM datasets.
- [`src/zone_service.py`](./src/zone_service.py): typed `Zone` contract and
  `get_zones()` function to implement.
- [`src/nisar_processing.py`](./src/nisar_processing.py): Stage 2 creates a
  cumulative displacement stack and metadata.
- [`src/subsidence_detection.py`](./src/subsidence_detection.py): Stage 3
  identifies and tracks exploratory deformation anomalies.
- [`tests/test_zones_api.py`](./tests/test_zones_api.py): current HTTP tests.

## Request and response

The frontend will request the same-origin path `GET /api/v1/zones`. No request
body, authentication, or query parameters are currently required. A successful
response has this shape (all values below are illustrative):

```json
{
  "schema_version": "1.0",
  "zones": [{
    "id": "zone-001",
    "geometry": {
      "type": "Polygon",
      "coordinates": [[[35.38, 31.45], [35.39, 31.45], [35.39, 31.46], [35.38, 31.46], [35.38, 31.45]]]
    },
    "risk_level": "elevated",
    "summary": {
      "los_velocity_mm_per_year": -60.88,
      "cumulative_shift_mm": -4.0,
      "acceleration_mm_per_year2": 0.0,
      "latest_pair_mean_coherence": 0.72,
      "latest_pair_reference_time": "2026-07-03T00:00:00Z",
      "latest_pair_secondary_time": "2026-07-15T00:00:00Z"
    },
    "cumulative_series": [
      { "time": "2026-06-21T00:00:00Z", "displacement_mm": 0.0 },
      { "time": "2026-07-03T00:00:00Z", "displacement_mm": -2.0 },
      { "time": "2026-07-15T00:00:00Z", "displacement_mm": -4.0 }
    ]
  }]
}
```

Each zone has:

| Field | Meaning |
| --- | --- |
| `id` | Non-empty identifier; keep stable where possible. |
| `geometry` | GeoJSON `Polygon` or `MultiPolygon` in WGS84. Coordinates are `[longitude, latitude]`; rings are closed. |
| `risk_level` | `high`, `elevated`, `watch`, `low`, or `null` if classification is unavailable. |
| `summary.los_velocity_mm_per_year` | LOS velocity in mm/year. |
| `summary.cumulative_shift_mm` | Cumulative LOS movement in mm relative to the series start. |
| `summary.acceleration_mm_per_year2` | Change in LOS velocity in mm/year². |
| `summary.latest_pair_mean_coherence` | Mean coherence for the latest acquisition pair, between 0 and 1. |
| `summary.latest_pair_reference_time`, `summary.latest_pair_secondary_time` | Earlier and later acquisition timestamps of that pair, in RFC 3339 date-time format. |
| `cumulative_series` | Time-ordered `{time, displacement_mm}` points relative to the first point, or `null` when unavailable. |

The API defines positive LOS movement as **toward** the satellite; negative is
away. All summary keys are required, but their values may be `null`. The frontend
will provide zone names and calculate coordinate ranges and areas; neither is an
API field. `schema_version` describes the response format, not a dataset.
`risk_level` is the backend base level; the dashboard may display an experimental
one-level adjustment based on OSM infrastructure intersections. The API response
itself is unchanged. See [DASHBOARD.md](./DASHBOARD.md) for coverage limitations.

Responses:

- **200:** calculations succeeded. `zones: []` means no zones were found.
- **503:** the precomputed zones export is unavailable. The error code
  is `calculations_unavailable`.
- **500:** calculation or JSON serialization failed; the server logs the
  details and returns `internal_error` without exposing them.

Both errors use `{"error": {"code": "...", "message": "..."}}`. The handler
rejects non-list results and non-finite JSON numbers; it does not validate the
full OpenAPI schema at runtime.

## Work for Almog

1. Implement `get_zones() -> list[Zone]` in
   [`src/zone_service.py`](./src/zone_service.py). Return **the list only**;
   the route supplies the outer object. Keep calculation and data-loading logic
   separate from HTTP handling. Raise `ZoneCalculationUnavailable` when the
   service is not ready, rather than returning an empty list. Surface unexpected
   processing errors so the endpoint can log them and return 500.
2. Select the input run explicitly and ensure the needed processed data is
   packaged with the deployment. Stage 2 creates
   `data/processed/run_.../{displacement.npy,coherence.npy,metadata.json}`;
   Stage 3 writes `anomalies.geojson` and metadata under `data/detection/`.
   The repository's [`.gitignore`](./.gitignore) currently ignores
   `data/detection/`. Stage 3 `main_process` writes files, so do not assume it
   can write to the deployed project directory on each request. Decide which
   processing happens locally and which calculations run per request, and
   verify bundled paths in a preview deployment.
3. Adapt each usable anomaly to the `Zone` contract. The detector already
   emits WGS84 Polygon or MultiPolygon geometry and an ID, but its ID includes
   the run name, so it is not guaranteed stable across runs. Its
   `priority` values (`urgent_review`, `review`, `watch`) are review categories,
   **not** the API's risk levels. Agree on and document a classification method
   before mapping them; otherwise return `null`.
4. Compute the summary and cumulative history using a clearly defined zone
   footprint and valid pixels across dates. Stage 2 uses network inversion to
   produce cumulative displacement, while Stage 3's exported feature contains
   the latest geometry and summary, not the API's full cumulative series.
   Missing or disconnected observations must not become invented values. Return
   `null` for unavailable fields or series, and convert NumPy values to ordinary
   JSON-compatible Python numbers; NaN and infinity are not valid JSON.
5. Only populate **LOS** fields from compatible LOS results with the stated
   sign and reference. Stage 3 rates are in mm/day and acceleration in
   mm/day²: using a 365.25-day year, multiply by `365.25` and `365.25²`,
   respectively, to convert compatible values. The detector's `mean_coherence`
   is based on per-date quality proxies, not necessarily the latest pair's
   mean coherence; don't present it as that field without the corresponding
   pair data. The current cumulative stack stores dates, so preserve accurate
   observation timestamps from source metadata rather than inventing times.
6. Confirm runtime dependencies and file inclusion on Vercel. In particular,
   Stage 3 imports `scipy`, which is not listed directly in
   [`requirements.txt`](./requirements.txt). Add any dependencies actually used
   by the deployed adapter, and verify Python package and data bundle sizes.
7. Add tests for the real adapter with representative data, missing values,
   empty results, Polygon/MultiPolygon geometries and failure cases. Update the
   existing test that expects the unimplemented 503 once the service is wired.
   Validate actual successful JSON against [OpenAPI](./openapi.json), not just
   the example. Test the API and packaging in a Vercel preview before release.

The sign, stable reference, quality and risk method require independent
validation. This is exploratory data, **not an operational safety warning**.

## Local checks

From the repository root, run the current route tests:

```sh
python -m unittest discover -s tests -v
```

To run the Vercel function locally, use `vercel dev` from the repository root
and request the URL it prints, for example:

```sh
curl http://localhost:3000/api/v1/zones
```

On Windows PowerShell, use `curl.exe` instead of the `curl` alias. A plain
`python -m http.server` serves files only and does not execute the function.
On the current Windows setup, `vercel dev` encountered a symlink-permission
error, so Vercel routing and packaging still need verification in a suitable
environment or preview deployment.

The frontend calls the relative path with `fetch('/api/v1/zones')` and displays
loading, empty and error states. Reload the page to retry a failed request.
Zone names use nearby places when the place dataset covers their search radius,
and otherwise use IDs.
Run the frontend contract tests with
`node --test tests/zone-api.test.js`.

The localhost-only `scripts/dashboard_server.py` can return labeled synthetic
demo zones when started with `ZONE_DATA_SOURCE=demo`. Its default is the real
API. This switch is implemented only in the local server, not the deployed route.
