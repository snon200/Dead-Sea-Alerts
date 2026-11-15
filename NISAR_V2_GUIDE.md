# NISAR v2 — Acquisition and cumulative-displacement processing

A compact research workflow for the Ein Gedi test area. Both scripts use **editable Python constants**, not command-line arguments. Run the commands from the **repository root** (the directory containing `src/` and `data/`).

> **Scope:** Experimental NISAR GUNW line-of-sight (LOS) processing. The output is **relative surface displacement**, not an elevation map, and is **not a verified sinkhole warning**. Stable-reference selection and LOS sign require independent validation.

## 1. Files and data flow

| Script | Input | Output | Purpose |
|---|---|---|---|
| `src/nisar_acquisition_direct.py` (acquisition v2) | GeoJSON polygon, acquisition dates, NASA GUNW + DEM | `data/raw/ein_gedi_direct.npz` | Fetch raw, aligned interferogram pairs and all correction layers |
| `src/nisar_processing_v2.py` | Acquisition `.npz`, stable reference rectangle | `data/processed/run_v2_.../` | Correct phase, mask unreliable pixels, remove planar ramps and reconstruct **cumulative LOS displacement** |

The acquisition `.npz` is **pair-indexed**: every `phase[i]` measures one reference/secondary acquisition pair. Processing converts the pair network into an **acquisition-date-indexed** cumulative stack: `displacement[k]` measures movement relative to the first acquisition date.

### Folder layout

```text
project-root/
├── src/
│   ├── nisar_acquisition_direct.py
│   └── nisar_processing_v2.py
├── data/
│   ├── ein_gedi.geojson
│   ├── cache/nisar_direct/             # Acquisition cache; reused on reruns
│   ├── raw/
│   │   └── ein_gedi_direct.npz         # Raw, uncorrected pairwise layers
│   └── processed/
│       └── run_v2_YYYYMMDD_HHMMSS_ffffff/
│           ├── displacement.npy       # Cumulative LOS [dates, rows, cols] (mm)
│           ├── coherence.npy          # Per-date support-quality proxy [dates, rows, cols]
│           ├── metadata.json          # Dates, grid, pair IDs, processing settings
│           ├── ramp_fits.csv          # Per-interferogram fitted ramp slopes
│           └── correction_comparison.png
└── NISAR_V2_GUIDE.md
```

## 2. Install dependencies (one time)

Use the project's activated Python environment in PowerShell:

```powershell
python -m pip install earthaccess requests h5py numpy scipy rasterio matplotlib
```

NASA Earthdata Login credentials are required for the remote GUNW HDF5 reads. `nisar_acquisition_direct.py` uses `earthaccess.login(persist=True)` and may open an authentication prompt. It reads remote *byte ranges*; it does **not** normally download the full multi-gigabyte granules, although remote requests can still be slow.

## 3. Acquisition v2 — `nisar_acquisition_direct.py`

### 3.1 Choose the geographic region

Supply a WGS84 GeoJSON Polygon, MultiPolygon, Feature or FeatureCollection. The example `data/ein_gedi.geojson` covers approximately longitude 35.35–35.45°E, latitude 31.43–31.50°N. Edit its vertices in a GIS/editor if you want a nonrectangular study area.

### 3.2 Edit the CONFIG block

```python
GEOJSON_FILE = Path("data/ein_gedi.geojson")
START_DATE = "2026-06-01"
END_DATE = "2026-11-15"  # inclusive
TRACK = 100
FRAME = 17
POLARIZATION = "HH"
RELEASE = "auto"
EPSG = 32636
PIXEL_M = 80.0
MAX_PAIRS = None
OUTPUT = Path("data/raw/ein_gedi_direct.npz")
```

`MAX_PAIRS = 1` is useful for an initial access test; set it back to `None` for the complete connected series. **Both dates** of each interferogram must fall inside the requested date range. The script chooses one consistent track/frame/direction/mode/polarization/release/processing family, rather than combining incompatible viewing geometries.

### 3.3 Run

```powershell
python src/nisar_acquisition_direct.py
```

The first run can take time because remote HDF5 chunks and DEM tiles have to be fetched. Subsequent runs reuse cached per-pair data and the aligned DEM where possible. Running acquisition again with the same `OUTPUT` **replaces the previous `.npz`**. Change `OUTPUT` if you want to keep multiple acquired datasets.

### 3.4 What the acquired `.npz` contains

| Key | Shape | Units / meaning |
|---|---|---|
| `phase` | `(P, H, W)` | Original unwrapped phase, radians |
| `coherence` | `(P, H, W)` | Interferometric coherence, 0–1 |
| `connected_components` | `(P, H, W)` | Unwrapping component labels |
| `ionosphere` | `(P, H, W)` | Ionospheric phase screen, radians |
| `tropo_wet` | `(P, H, W)` | Wet tropospheric phase screen, radians |
| `tropo_dry` | `(P, H, W)` | Hydrostatic/dry tropospheric phase screen, radians |
| `solid_earth_tides` | `(P, H, W)` | Solid-Earth-tide phase correction, radians |
| `elevation` | `(H, W)` | NASA DEM WGS84 **ellipsoidal** elevation, metres |
| `x`, `y` | `(W,)`, `(H,)` | Projected UTM cell-center coordinates, metres |
| `polygon_mask` | `(H, W)` | Pixels inside the requested GeoJSON polygon |
| `reference_times`, `secondary_times` | `(P,)` | UTC time for each GUNW pair |
| `granule_ids` | `(P,)` | Source granule identifiers |
| `metadata_json` | scalar string | Grid, source and methodology information |

All correction layers have the same ground-cell indices as `phase`. Acquisition interpolates the **3D** radar-grid corrections horizontally and vertically to each pixel's DEM ellipsoidal height, rather than applying them at a single assumed height. It **does not subtract anything from phase**.

```python
import numpy as np
with np.load("data/raw/ein_gedi_direct.npz", allow_pickle=False) as a:
    print(a["phase"].shape)
    print(a["elevation"].shape)
    print(a["reference_times"])
```

## 4. Processing v2 — `nisar_processing_v2.py`

### 4.1 Edit the settings

```python
INPUT = Path("data/raw/ein_gedi_direct.npz")
OUTPUT_ROOT = Path("data/processed")

REF_BBOX = (35.3770, 31.4490, 35.3880, 31.4555)  # W,S,E,N
COHERENCE_MIN = 0.35
MIN_REFERENCE_PIXELS = 8

APPLY_IONOSPHERE = True
APPLY_WET_TROPO = True
APPLY_DRY_TROPO = True
APPLY_SOLID_EARTH_TIDES = True
APPLY_PLANAR_RAMP = True

RAMP_COHERENCE_MIN = 0.65
RAMP_MIN_PIXELS = 40
RAMP_EXCLUDE_BBOXES = []  # Insert known deforming/incoherent rectangles
RAMP_FIT_BBOX = None      # None: whole acquisition polygon

PHASE_SIGN = -1           # PROVISIONAL LOS convention; verify
CENTER_FREQUENCY_MHZ = None  # Infer frequency A from granule mode

MAKE_DIAGNOSTIC_FIGURE = True
SHOW_FIGURE = False
DISPLAY_LIMIT_MM = 60.0
```

**Replace `REF_BBOX` with a land region you have inspected.** The shipped box is only an *unvalidated candidate*. Prefer coherent terrain away from shoreline changes, mapped sinkholes, steep unstable slopes and construction. Google Maps can help identify land cover, but cannot demonstrate geodetic stability. Check the reference in more than one acquisition.

### 4.2 Run

```powershell
python src/nisar_processing_v2.py
```

Every run produces a new `data/processed/run_v2_...` directory; earlier processed results are **not overwritten**. The input `.npz` remains unchanged.

### 4.3 All processing steps, in order

1. **Load and validate grid:** Read the common `(P,H,W)` raw arrays, acquisition pair timestamps, GeoJSON mask and UTM georeferencing. Check consistent shapes and one Frequency-A acquisition mode. Derive wavelength from NISAR center frequency (for mode `2000`: 1229 MHz).
2. **Quality mask:** Keep only pixels inside the geographic polygon with finite unwrapped phase, coherence between `COHERENCE_MIN` and 1, a valid connected-component label, and finite values for **every enabled correction**. No missing correction is silently replaced with zero.
3. **Find a reference-connected phase component:** For each interferogram, locate valid pixels within `REF_BBOX`, select the connected-component label containing the most reference pixels, and exclude other disconnected unwrapping components. This avoids comparing arbitrary phase offsets between unrelated components.
4. **Subtract atmospheric and geophysical phase screens:** Each correction is retained separately in the raw file; processing subtracts enabled screens in **radians**:

   \[\phi_{\mathrm{corr}}=\phi_{\mathrm{raw}}-\phi_{\mathrm{iono}}-\phi_{\mathrm{wet}}-\phi_{\mathrm{dry}}-\phi_{\mathrm{tide}}.\]

   `solid_earth_tides` is a geophysical correction, not an atmospheric one. The script assumes each screen represents an additive phase contribution to remove; **the sign of each correction and the LOS sign should be verified against the specific product convention**. Switch terms on/off to inspect sensitivity.
5. **Reference each pair:** Subtract the median corrected phase inside the chosen reference patch, using only coherent pixels within the selected unwrapping component. This sets the **relative pair displacement** at the reference to approximately zero. No absolute geodetic movement can be inferred without an external stable reference.
6. **Convert phase to LOS displacement:**

   \[\Delta d_{\mathrm{LOS,mm}}=s\frac{1000c}{4\pi f_A}\,(\phi_{\mathrm{corr}}-\mathrm{median}(\phi_{\mathrm{corr,ref}})),\quad s=\texttt{PHASE\_SIGN}.\]

   With `PHASE_SIGN = -1`, the **assumed** positive convention is motion toward the satellite and negative means away. The algorithm does **not** project LOS to vertical displacement; no incidence-angle assumption is needed.
7. **Fit and remove a planar ramp (per pair):** Fit `a + b (east in km) + c (north in km)` using high-coherence pixels (`RAMP_COHERENCE_MIN`). Repeated robust weighting reduces isolated outliers. Subtract only the spatial **slopes**, then re-anchor the result to the reference median. The fitted `b` and `c` are recorded in `ramp_fits.csv` in **mm/km**. Exclude candidate deforming regions using `RAMP_EXCLUDE_BBOXES`; otherwise real broad subsidence can bias the plane. If there are not enough stable pixels, the script raises an error instead of silently skipping correction.
8. **Invert the interferogram network:** Each pair measures `D(t_secondary) - D(t_reference)`. Perform **coherence-weighted least squares per ground cell** with the first acquisition date fixed to zero. This correctly handles overlapping pairs without double-counting. Where the available valid pairs do not connect a pixel to the first date, output `NaN` for the disconnected dates. **There is no automatic unwrapping-error repair.**
9. **Save cumulative displacement:** Write `displacement.npy` (mm) and `coherence.npy` (per-date quality proxy), metadata, ramp fits, and optional four-panel diagnostic plot.

### 4.4 What does “cumulative” mean?

| Acquisition date | Pair movement | Output cumulative displacement |
|---|---:|---:|
| June 21 | First acquisition | 0 mm |
| July 3 | −5 mm since June 21 | −5 mm |
| July 15 | −9 mm since July 3 | −14 mm |

If a direct June 21 → July 15 pair is also available, the network inversion uses that observation too. The first map is zero only where future observations are connected; it is **NaN** elsewhere.

### 4.5 Output arrays and viewer

```python
import numpy as np
from pathlib import Path
run = Path("data/processed/run_v2_REPLACE_WITH_ACTUAL_RUN_NAME")
D = np.load(run / "displacement.npy")  # (number_of_dates, H, W), mm
Q = np.load(run / "coherence.npy")     # same shape, per-date quality proxy
latest_cumulative = D[-1]             # since FIRST date
latest_interval = D[-1] - D[-2]       # between LAST TWO dates, if valid
```

The existing viewer is compatible with the saved file layout:

```powershell
python src/view_nisar_stack.py --input data/processed/run_v2_REPLACE_WITH_ACTUAL_RUN_NAME --list
python src/view_nisar_stack.py --input data/processed/run_v2_REPLACE_WITH_ACTUAL_RUN_NAME --scene 3 --vlim 40 --show
```

**Viewer detail:** `view_nisar_stack.py` defaults to the **first** acquisition (`--scene 1`), which is the zero reference map. Choose `--scene N` for a later date. `coherence.npy` is the mean coherence of supporting **interferogram pairs**, **not** an observed same-date coherence map.

Stage 3 should also accept this run because the `.npy`/`metadata.json` format is compatible with its cumulative Stage 2 input; confirm the scientific validity of the corrected map before detection.

## 5. Understand the diagnostic figure

`correction_comparison.png` compares the **last acquisition date** on a consistent ±`DISPLAY_LIMIT_MM` color scale:

| Panel | What to inspect |
|---|---|
| No corrections | Raw reference-relative displacement (same coherence/component mask) |
| Iono + wet/dry + tide | Effect of subtracting all enabled correction screens |
| + planar ramp | Final corrected cumulative displacement |
| Effect of ramp | Difference between final map and atmosphere-corrected map |

UTM easting/northing axes are in **kilometres**, with correct geographic axis order. If the ramp effect is much smaller than the atmosphere effect, that is a useful result, **not an error**. Large localized jumps can be unwrapping artifacts; mask or inspect them rather than letting them influence the ramp-fit background.

## 6. Common problems

| Symptom | What to check |
|---|---|
| `Acquisition v2 data missing` | Confirm `INPUT` points to the `.npz` from acquisition v2 |
| No valid reference pixels | Move `REF_BBOX` inside coherent stable land; inspect whether its component is connected |
| Insufficient ramp-fit pixels | Use a larger stable land area, reduce `RAMP_COHERENCE_MIN` cautiously, or set `APPLY_PLANAR_RAMP=False` for comparison |
| A pixel becomes `NaN` in some dates | A pair failed coherence/component/correction masking or the pixel-level date graph is disconnected |
| Huge jumps in dates | Investigate raw pair coherence, connected-component boundaries and possible ±2π phase-unwrapping errors; the inversion cannot repair them |
| Apparent shared motion in all zones | Check reference stability, atmospheric residuals, and broad deformation before treating it as local subsidence |
| Acquisition is slow | Initial HDF5 remote byte-range reads and 3D correction cubes can be slow; subsequent runs use the cache. Test `MAX_PAIRS=1` first |
| New `.npz` data don't appear in old Stage 2 | Old Stage 2 expects GeoTIFF manifests. Use **processing v2** for the direct-acquisition `.npz` |

## 7. Scientific caveats and references

- **All displacement is relative to the first acquisition date and to the selected spatial reference patch**. It is **LOS**, not vertical settlement or terrain elevation.
- The example reference region is **unvalidated**, as is the `PHASE_SIGN = -1` assumption for provisional products.
- The supplied atmospheric screens may retain residual errors and have finite model accuracy. DEM-height interpolation improves terrain treatment but cannot eliminate spatial weather-model errors.
- **Planar ramp subtraction can erase real regional deformation.** Fit against a separately evaluated background and retain the raw/corrected comparison.
- The connected GUNW date graph may contain no redundant interferometric loops. With a simple chain, a bad pair contaminates subsequent cumulative estimates; coherence weighting alone cannot fix this.
- NASA's *provisional* products are subject to recalibration. Avoid mixing validated and provisional acquisition families without checking consistency.

Official references:

- [NISAR GUNW product guide](https://nisar-docs.asf.alaska.edu/gunw/) — 80 m geocoded unwrapped phase and separately supplied correction layers.
- [NISAR metadata/correction cubes](https://nisar-docs.asf.alaska.edu/metadata/) — wet/dry atmosphere, solid-Earth tide and ellipsoidal height grids.
- [NISAR acquisition modes and center frequencies](https://nisar-docs.asf.alaska.edu/nisar-intro/) — frequency A for mode conversion.
- [NISAR data maturity](https://nisar-docs.asf.alaska.edu/products-overview/) — provisional/validated collection cautions.
