# NISAR Dead Sea monitoring — `src/` operating guide

**Purpose:** one-place command reference for the NISAR GUNW acquisition, cumulative displacement processing, exploratory anomaly detection, visualization, and website API export scripts.

**Environment:** Windows 11 / PowerShell. Run every command from the **repository root** (the directory that contains `src/`, `data/`, and `tests/`). These instructions cover the cumulative-stack versions of the scripts developed for this project, not older pairwise-processing scripts.

> **Scientific status:** this is a research screening workflow, not an operational sinkhole warning system. The physical LOS sign, stability of the reference area, atmospheric residuals, and anomaly thresholds still require independent validation.

## 1. At a glance

| Script (`src/`) | Role | Main output |
| --- | --- | --- |
| `nisar_acquisition.py` | **Stage 1:** search NASA CMR and request Harmony GeoTIFF subsets of NISAR GUNW interferograms | `data/raw/index.json`, scene manifests and GeoTIFFs |
| `nisar_processing.py` | **Stage 2:** convert a connected GUNW interferogram network to **cumulative LOS displacement** | `data/processed/<RUN>/` |
| `view_nisar_stack.py` | View Stage 2 cumulative displacement and quality maps, including distance scale bars | PNG in the selected Stage 2 run |
| `subsidence_detection.py` | **Stage 3:** detect and track candidate localized deformation regions | `data/detection/<RUN>/` |
| `view_detection.py` | Inspect candidate masks, rates, acceleration, quality and polygons | PNG in the selected Stage 3 run |
| `zone_service.py` | Offline adapter: convert matching Stage 2/3 results into the frontend Zones API format | `data/api/zones.json` |

The `tests/` scripts and `api/v1/zones.py` are outside `src/`, but are included in the testing/deployment section below.

### File layout

```text
project-hail-miriam/
├── src/
│   ├── nisar_acquisition.py
│   ├── nisar_processing.py
│   ├── view_nisar_stack.py
│   ├── subsidence_detection.py
│   ├── view_detection.py
│   └── zone_service.py
├── data/
│   ├── raw/
│   │   ├── index.json
│   │   └── <scene>/manifest.json + GeoTIFFs
│   ├── processed/
│   │   └── <RUN>/displacement.npy, coherence.npy, metadata.json
│   ├── detection/
│   │   └── <RUN>/velocity.npy, acceleration.npy, candidate_mask.npy,
│   │              anomalies.geojson, metadata.json
│   └── api/
│       └── zones.json
├── api/v1/zones.py           # HTTP endpoint; calls zone_service.get_zones()
└── tests/
    ├── test_zones_api.py
    └── test_zone_service.py
```

`<RUN>` is the exact name of a Stage 2 output directory, typically `run_YYYYMMDD_HHMMSS_ffffff` (UTC). **Stage 3 reuses this name** so its data can be matched to the right Stage 2 run.

## 2. Quick start — full pipeline

The following is the recommended lightweight **LOS-only, no remote HDF5** workflow for the Ein Gedi test area. The reference box below is a **candidate** reference area, not validated stable ground.

### Step A — Download connected interferograms (Stage 1)

```powershell
python src/nisar_acquisition.py --bbox 35.35 31.43 35.45 31.50 --start 2026-06-01 --timeseries --max-scenes 0 --epsg 32636
```

- The box is **west south east north**, in longitude/latitude degrees.
- `--timeseries` selects a connected sequence of acquisition pairs from a compatible track/frame family.
- `--max-scenes 0` means all selected pairs. For a small trial, use `--max-scenes 2`.
- Harmony processes scenes on NASA servers and may take some time. Do not confuse an unchanged progress percentage with a local Python crash.
- **Do not use `--with-radar-metadata` for this lightweight workflow**; that optional mode accesses the much larger HDF5 original for incidence, frequency and atmospheric metadata.

### Step B — Generate cumulative displacement (Stage 2)

```powershell
python src/nisar_processing.py --input data/raw/index.json --ref-bbox 35.3770 31.4490 35.3880 31.4555 --component los --no-troposphere
```

Stage 2 creates a **new timestamped folder** in `data/processed/`. Find its name with:

```powershell
Get-ChildItem data/processed -Directory | Sort-Object LastWriteTime -Descending | Select-Object Name, LastWriteTime
```

To select the newest folder in the current PowerShell session:

```powershell
$RUN = (Get-ChildItem data/processed -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1).Name
Write-Host "Selected processing run: $RUN"
```

**Important:** keep using that same `$RUN` for the remaining commands. If you close the terminal, assign it again or explicitly set `$RUN = "run_..."`.

### Step C — View Stage 2 results

```powershell
python src/view_nisar_stack.py --input "data/processed/$RUN" --list
python src/view_nisar_stack.py --input "data/processed/$RUN" --scene 3 --vlim 20 --show
```

### Step D — Detect candidate deformation zones (Stage 3)

```powershell
python src/subsidence_detection.py --input "data/processed/$RUN"
```

Results go to `data/detection/$RUN/` automatically.

### Step E — View the detections

```powershell
python src/view_detection.py --input "data/detection/$RUN" --list
python src/view_detection.py --input "data/detection/$RUN" --show
```

The detection viewer defaults to the latest acquisition date. To inspect the same date as the Stage 2 example:

```powershell
python src/view_detection.py --input "data/detection/$RUN" --scene 3 --show
```

### Step F — Export results for the website

```powershell
python src/zone_service.py --run "$RUN" --data-root data --out data/api/zones.json
```

This generates the static JSON used by `GET /api/v1/zones`. Re-run this command after Stage 3 results change; the JSON file is **not updated automatically**.
The export includes only detections last observed on the latest acquisition date; older detections remain in the Stage 3 outputs.

---

## 3. Stage 1 — `nisar_acquisition.py`

**Inputs:** NASA Earthdata / CMR discovery, NASA Harmony processing, a study area and date range.

**Outputs:** geocoded GeoTIFFs for each scene and `data/raw/index.json`, including per-scene `manifest.json` files. The default layer list is unwrapped phase, coherence, ionospheric phase screen, and connected-component labels.

### Commands

Search for available data without submitting Harmony processing:

```powershell
python src/nisar_acquisition.py --bbox 35.35 31.43 35.45 31.50 --start 2026-06-01 --timeseries --search-only
```

Fetch a short test set:

```powershell
python src/nisar_acquisition.py --bbox 35.35 31.43 35.45 31.50 --start 2026-06-01 --timeseries --max-scenes 2 --epsg 32636
```

Search a wider western Dead Sea region (the selected track/frame still needs to intersect the area):

```powershell
python src/nisar_acquisition.py --bbox 35.30 30.95 35.54 31.80 --start 2026-06-01 --timeseries --max-scenes 0 --epsg 32636
```

### Useful options

| Argument | Meaning |
| --- | --- |
| `--bbox W S E N` | Bounding box in WGS84 degrees |
| `--center LAT LON --size-km 8` | Alternative to a bounding box |
| `--start YYYY-MM-DD` | Earliest **secondary/later** acquisition date to search |
| `--end YYYY-MM-DD` | Latest date to search |
| `--timeseries` | Restrict selection to a connected, compatible acquisition-pair sequence; **required for cumulative Stage 2** |
| `--max-scenes N` | Maximum selected interferograms; `0` means all |
| `--series-track N`, `--series-frame N` | Select a particular orbit track/frame |
| `--search-only` | Inspect availability; do not request raster processing |
| `--retry-failed` | Retry cached failed Harmony jobs |
| `--epsg 32636` | Request geocoded output in UTM zone 36N |
| `--release auto` | Select among validated, provisional and beta collections |
| `--polarization HH` | Restrict polarization |
| `--layers ...` | Override requested layer names (advanced) |
| `--out data/raw` | Destination for raw scene folders and index |
| `--with-radar-metadata` | Optional slow remote HDF5 access; not needed for current lightweight workflow |
| `--verbose` | More diagnostic logs |

**Prerequisites:** an Earthdata Login credential available to Harmony (for example via `.netrc` or supported Earthdata environment variables) and network access to NASA services.

**Common issue:** a Harmony processing job can remain at a low percentage for a long time. Stage 1 is waiting for server-side data preparation; check the NASA job status rather than immediately re-submitting the same work.

## 4. Stage 2 — `nisar_processing.py`

**Input:** the Stage 1 `data/raw/index.json` and associated completed interferogram rasters.

**Output:** one **new** run directory containing:

- `displacement.npy`: shape `(T, H, W)`, cumulative displacement in **mm**, relative to the first acquisition date; `NaN` means unavailable.
- `coherence.npy`: shape `(T, H, W)`, mean supporting-pair coherence **quality proxy**, not the original single-pair coherence.
- `metadata.json`: acquisition dates, original pairs, grid CRS/transform, reference box, correction policy, and provenance.

`T` is the number of **unique acquisition dates**, not the number of interferogram pairs. Cumulative displacement is obtained by weighted network inversion, avoiding double-counting pairs that overlap in time. The first map is zero only where data are connected to the first date; it can contain `NaN` elsewhere.

### Commands

Standard lightweight run:

```powershell
python src/nisar_processing.py --input data/raw/index.json --ref-bbox 35.3770 31.4490 35.3880 31.4555 --component los --no-troposphere
```

Use a descriptive, explicitly unique run name:

```powershell
python src/nisar_processing.py --input data/raw/index.json --ref-bbox 35.3770 31.4490 35.3880 31.4555 --component los --no-troposphere --run-name ein_gedi_los_v2
```

Restrict the processed raster to a smaller geographic area:

```powershell
python src/nisar_processing.py --input data/raw/index.json --bbox 35.35 31.43 35.45 31.50 --ref-bbox 35.3770 31.4490 35.3880 31.4555 --component los --no-troposphere
```

### Useful options

| Argument | Meaning |
| --- | --- |
| `--input` | Stage 1 index JSON (default `data/raw/index.json`) |
| `--out` | **Parent** of run folders (default `data/processed`) |
| `--run-name` | Name of a **new** run folder; defaults to a unique UTC timestamp |
| `--ref-bbox W S E N` | **Required.** Candidate stable land area used to remove reference phase |
| `--bbox W S E N` | Optional analysis crop; otherwise inherit Stage 1 area |
| `--component los` | Recommended: line-of-sight displacement |
| `--component vertical` | Approximate vertical estimate; requires independently verified geometry and negligible horizontal motion assumption |
| `--coh-min 0.3` | Displacement-quality threshold |
| `--resolution-m 80` | Grid spacing in metres |
| `--epsg 32636` | Target UTM projected CRS |
| `--no-troposphere` | **Use when Stage 1 did not obtain radar/tropospheric metadata** |
| `--no-ionosphere` | Disable ionospheric correction for diagnostic experiments |
| `--frequency-mhz N` | Override automatically inferred radar frequency if independently known |
| `--phase-sign -1` | Current assumed LOS sign convention; **not independently validated** |
| `--min-ref-pixels 10` | Minimum number of valid reference pixels |
| `--verbose` | More diagnostic logs |

**Preservation:** Stage 2 refuses to create an already existing output run folder (`exist_ok=False`), avoiding accidental replacement. Choose another `--run-name` when experimenting.

**Reference-area warning:** `35.3770 31.4490 35.3880 31.4555` is an unverified candidate near Ein Gedi. A moving reference biases every pixel's estimated displacement.

## 5. Stage 2 viewer — `view_nisar_stack.py`

Shows a **cumulative displacement** panel and a per-date coherence-quality panel. Both include geographic scale bars; images are saved as PNG files.

```powershell
python src/view_nisar_stack.py --list
python src/view_nisar_stack.py --input "data/processed/$RUN" --list
python src/view_nisar_stack.py --input "data/processed/$RUN" --scene 3 --show
python src/view_nisar_stack.py --input "data/processed/$RUN" --scene 3 --bbox 35.35 31.43 35.45 31.50 --vlim 20 --display-coh-min 0.5 --show
python src/view_nisar_stack.py --input "data/processed/$RUN" --scene 3 --out results/stage2_scene3.png
```

- `--scene` is **1-based** (scene 1 = first acquisition); its default is **1**, **not the latest date**.
- With no `--input`, the viewer selects the newest run under `data/processed/`.
- `--vlim 20` sets a symmetric displacement color scale of ±20 mm.
- `--display-coh-min 0.5` affects **display only**, not the underlying saved measurements.
- The default output filename is `quicklook_date_NNN.png` in the selected processing run.

**Interpretation:** the displacement map at date `t` shows movement relative to the **first** acquisition, not movement since the previous one. The change over the last interval is `displacement[t] - displacement[t-1]`. In LOS coordinates, the assumed positive direction is toward the satellite.

## 6. Stage 3 — `subsidence_detection.py`

**Input:** one cumulative Stage 2 run directory.

**Output:** `data/detection/<SAME RUN NAME>/` with:

| File | Meaning |
| --- | --- |
| `velocity.npy` | `(T-1, H, W)` LOS displacement-rate finite differences, **mm/day** |
| `acceleration.npy` | `(T-2, H, W)` changes of interval velocity, **mm/day²** |
| `candidate_mask.npy` | `(T, H, W)` binary candidate regions per acquisition date; first slice is zero |
| `anomalies.geojson` | WGS84 Polygon/MultiPolygon features, latest geometry for each tracked region |
| `metadata.json` | Thresholds, interval screening statistics, source run and warnings |

**Processing:** NaN-aware local Gaussian smoothing; broader spatial background estimate; local residual and robust-noise thresholds; connected components; temporal region matching; review-priority labels. The implementation finds significant localized **deformation** in either LOS direction; don't assume every polygon is confirmed subsidence.

### Commands

Defaults:

```powershell
python src/subsidence_detection.py --input "data/processed/$RUN"
```

Example of a more sensitive exploratory screening setting:

```powershell
python src/subsidence_detection.py --input "data/processed/$RUN" --min-mm 5 --min-rate 0.2 --min-snr 2.5 --min-pixels 3
```

These settings change false-positive/false-negative tradeoffs; **they are not validated safety thresholds**.

### Useful options and defaults

| Argument | Default | Meaning |
| --- | --- | --- |
| `--input` | required | Stage 2 **run folder**, not the `data/processed/` parent |
| `--out` | `data/detection` | Parent of the matching detection run folder |
| `--coh-min` | `0.3` | Minimum per-date coherence-quality support |
| `--local-sigma-m` | `100` | Local Gaussian smoothing spatial scale |
| `--background-sigma-m` | `900` | Background spatial scale |
| `--min-mm` | `8` | Minimum localized change magnitude in mm |
| `--min-rate` | `0.3` | Minimum localized rate in mm/day |
| `--min-snr` | `3` | Minimum robust noise multiple |
| `--min-pixels` | `4` | Minimum contiguous candidate pixels |
| `--noise-floor-mm` | `2` | Minimum assumed noise scale in mm |
| `--min-filter-support` | `0.35` | Required valid-data support for filtering |
| `--accel-threshold` | `0.02` | Acceleration review threshold in mm/day² |

**Important overwrite behavior:** Stage 3 uses the same folder name as Stage 2 and **will overwrite** `velocity.npy`, `acceleration.npy`, `candidate_mask.npy`, `anomalies.geojson`, and `metadata.json` when rerun for that same input run. If you need to compare parameter sets, create a distinct Stage 2 run name first or make an explicit backup of its detection directory.

## 7. Stage 3 viewer — `view_detection.py`

Shows four aligned panels: cumulative displacement, velocity, acceleration and coherence-quality proxy. Highlights detected candidate pixels and last-observed tracked polygons, with scale bars.

```powershell
python src/view_detection.py --list
python src/view_detection.py --input "data/detection/$RUN" --list
python src/view_detection.py --input "data/detection/$RUN" --show
python src/view_detection.py --input "data/detection/$RUN" --scene 3 --show
python src/view_detection.py --input "data/detection/$RUN" --date 2026-10-19 --show
python src/view_detection.py --input "data/detection/$RUN" --scene 3 --bbox 35.35 31.43 35.45 31.50 --vlim 20 --rate-limit 1 --accel-limit 0.05 --show
```

- Default is the **latest acquisition date**. With no `--input`, the viewer finds the most recently saved detection run under `data/detection/`.
- Use **either** `--scene N` (1-based) **or** `--date YYYY-MM-DD`; they are mutually exclusive.
- `--display-coh-min 0.5` optionally applies additional display-only masking.
- `--out file.png` writes to a custom location; otherwise outputs `detection_quicklook_NNN.png` in the detection run.
- If you moved the processing run, use `--processed "path/to/processed/run"` to tell the viewer where to find its Stage 2 arrays.
- The polygon collection retains only the **latest** geometry for each tracked anomaly. Older dates show their own candidate masks, but historical polygon boundaries cannot be reconstructed from `anomalies.geojson` alone.

**Which interval does each panel use?** On the selected date, *displacement* is cumulative since the first acquisition. *Velocity* is calculated from the selected and immediately preceding acquisition; *acceleration* compares the two most recent velocity intervals. The first date has no velocity; the first two dates have no acceleration.

## 8. API export — `zone_service.py`

This script **does not make an HTTP request and does not start NISAR processing**. It builds a static API payload from **matching completed Stage 2 and Stage 3 runs**:

```powershell
python src/zone_service.py --run "$RUN" --data-root data --out data/api/zones.json
```

Optional minimum time-consistent pixels for each polygon:

```powershell
python src/zone_service.py --run "$RUN" --min-common-pixels 4
```

The exporter:

1. Reads Stage 3 WGS84 polygons.
2. Uses each polygon's latest geometry as a **fixed footprint** across Stage 2 dates, requiring common valid pixels for the full history.
3. Computes mean cumulative LOS history, latest LOS rate (mm/year), and acceleration (mm/year²), when valid timestamps and compatible data exist.
4. Looks up the **original latest interferogram coherence** under `data/raw/`, if that raster is still available. The Stage 2 coherence proxy is **not** substituted for that API field.
5. Sets unavailable values to JSON `null`. Keeps `risk_level: null`: the detector's `watch`/`review`/`urgent_review` categories have **not** been validated as hazard risk levels.

**Output:** `data/api/zones.json` (this **single file is replaced** each time you export). `GET /api/v1/zones` is served by `api/v1/zones.py` and calls `get_zones()` to read the prebuilt data; it does **not** run Stage 2/3 on a web request.

To choose a non-default cache file in PowerShell when running locally:

```powershell
$env:NISAR_ZONES_FILE = "data/api/zones.json"
```

To deploy on Vercel, include the generated JSON in the deployment. If the repository `.gitignore` excludes it and you intend to commit this output:

```powershell
git add -f data/api/zones.json
```

The frontend must separately call `fetch('/api/v1/zones')`; the adapter does not itself change the dashboard's fixture-data behavior. See `API.md` and `openapi.json` for the documented API contract.

## 9. Setup, checks and troubleshooting

### Install Python dependencies

Use the project's environment or requirements file if provided. For the six scripts above, the principal Python packages are:

```powershell
python -m pip install numpy scipy matplotlib rasterio pyproj affine requests harmony-py
```

Only if you choose Stage 1's optional `--with-radar-metadata`:

```powershell
python -m pip install earthaccess h5py
```

Verify the local command-line interfaces (these commands need no downloaded NISAR scenes):

```powershell
python src/nisar_acquisition.py --help
python src/nisar_processing.py --help
python src/view_nisar_stack.py --help
python src/subsidence_detection.py --help
python src/view_detection.py --help
python src/zone_service.py --help
```

Run the Python tests:

```powershell
python -m unittest discover -s tests -v
```

That test command also requires the repository's HTTP handler (`api/v1/zones.py`) and the authoritative `openapi.json` used by the API tests. Full Vercel routing/packaging must still be checked in the target environment.

### Common problems

| Symptom | What to check |
| --- | --- |
| Stage 1 Harmony stuck at a small percentage | Inspect the specific Harmony job status; processing is server-side. Avoid duplicate submissions unless it failed. |
| Stage 1 found scenes but Stage 2 rejects them | Stage 2 needs completed rasters from a **compatible connected `--timeseries`** selection. Check `data/raw/index.json` and per-scene manifests. |
| Stage 2 says reference pixels are insufficient | The selected reference box may lack coherent land pixels; check its location and Stage 1 coverage. Do **not** substitute an arbitrary moving reference. |
| Stage 2 complains about tropospheric inputs | Rerun with `--no-troposphere` if Stage 1 did not fetch optional radar metadata. |
| Viewer displays mostly NaN / blank areas | Check raster coverage, quality threshold, connected-component validity, reference availability, and `--bbox`. |
| Stage 2 viewer shows 0 mm unexpectedly | It defaults to **scene 1**, the reference date. Use `--list` then select a later `--scene`. |
| Stage 3 produces no polygons | It may legitimately find no qualifying regions; inspect `metadata.json`, masks and thresholds. Do not equate no candidates with proven ground stability. |
| No API zones, but Stage 3 has polygons | Ensure `--run` matches both folders, original dates are usable, compatible LOS metadata exist, and valid pixels remain inside polygon footprints. |
| Website returns 503 | Ensure `data/api/zones.json` was generated, included in deployment, and readable at the configured path. |
| Website still shows illustrative data | The frontend must actually call `GET /api/v1/zones`; generation of `zones.json` alone does not connect the UI. |

### Important data conventions

- **Bounding boxes:** `WEST SOUTH EAST NORTH` = `[longitude_min, latitude_min, longitude_max, latitude_max]` in WGS84.
- **Spatial coordinates:** GeoJSON polygon coordinates use `[longitude, latitude]`; raster calculations use projected EPSG:32636 metres for Ein Gedi.
- **Time:** Stage 2 holds cumulative displacement at unique acquisition **dates**. Stage 3 rates are per interval. The API uses original available UTC acquisition timestamps and does not invent midnight timestamps.
- **Displacement:** LOS millimetres, referenced to both the first acquisition and the selected land reference area. The current physical sign is an **assumption**, not validated independently.
- **Quality:** Stage 2 `coherence.npy` is a mean supporting-pair quality **proxy**, not the coherence of the latest original interferogram.
- **Detection:** Stage 3 polygons are exploratory **deformation anomaly zones**, not precise sinkhole boundaries or verified risk polygons. An 80 m raster cannot resolve an individual small sinkhole.

---

**Last reviewed:** 2026-11-14. **Primary command reference:** the `--help` output of your installed script versions. If a script is updated, check its CLI flags before reusing examples in this guide.
