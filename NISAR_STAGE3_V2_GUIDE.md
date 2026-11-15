# Stage 3 v2 — subsidence screening

## Files

- `src/subsidence_detection_v2.py` — produces candidate deformation polygons and provisional screening categories.
- `src/zone_service.py` — updated offline API export adapter. The existing `api/v1/zones.py` HTTP handler needs **no changes**.
- `tests/test_subsidence_detection_v2.py` — detector tests.
- `tests/test_zone_risk_forwarding.py` — checks API risk export.

## Data flow (important)

`data/raw/dead_sea_cropped_dem.npz` is an **uncorrected, pairwise acquisition product**. It is **not cumulative displacement**. Run processing first:

```powershell
python src/nisar_processing_v2.py
```

It creates `data/processed/run_v2_<timestamp>/displacement.npy`, `coherence.npy` and `metadata.json`. Stage 3 uses **these corrected, cumulative** files produced from the new NPZ acquisition format; it deliberately does not treat raw phase as displacement.

At the top of `src/subsidence_detection_v2.py`, set:

```python
PROCESSED = Path("data/processed")  # newest compatible processing run
# Or: PROCESSED = Path("data/processed/run_v2_20261115_...")
OUTPUT_ROOT = Path("data/detection")
```

Run:

```powershell
python src/subsidence_detection_v2.py
```

Stage 3 saves outputs in `data/detection/<SAME_STAGE2_RUN_NAME>/`: `anomalies.geojson`, `velocity.npy`, `acceleration.npy`, `candidate_mask.npy`, `metadata.json`. Rerunning detection for the *same* processing run overwrites those Stage 3 files; Stage 2 remains unchanged.

To update the website, replace `src/zone_service.py` with the updated version, then export for the matching processing folder:

```powershell
$RUN = "run_v2_REPLACE_WITH_YOUR_RUN_NAME"
python src/zone_service.py --run "$RUN" --data-root data --out data/api/zones.json
```

The existing API route will read `data/api/zones.json` at runtime. Deploy that file. The website's `risk_level` field is populated by Stage 3's `risk_level` rather than by the old `priority` review tags.

## Detection steps

1. Read cumulative displacement `(T,H,W)` [mm] and same-grid per-date coherence quality proxy from Stage 2.
2. Calculate interval LOS velocity `(T-1,H,W)` in mm/day, and acceleration `(T-2,H,W)` in mm/day², using the actual day intervals.
3. For each interval, apply coherence masking and NaN-aware Gaussian smoothing; subtract a broad Gaussian background to isolate localized motion. Use robust median/MAD thresholds, restrict to negative residual (motion *away* from satellite), and remove small connected regions.
4. Additionally screen cumulative displacement since the first acquisition to find slower, persistent deformation patches that might not exceed the per-interval threshold.
5. Track overlapping geographic regions between acquisition dates; polygonize into WGS84 GeoJSON. `candidate_mask.npy` stores masks for every date but the GeoJSON stores each track's last geometry.
6. For each polygon, use a fixed valid pixel footprint at the **latest** acquisition to estimate the latest interval's signed LOS rate (mm/year). Subsidence-screening magnitude is `max(0, -LOS_velocity_mm_year)` under the **provisional** positive-toward-satellite convention.
7. Check three consecutive recent intervals. `accelerating_last_3_intervals=True` only if all three interval rates are measurable, the first has a positive subsidence rate, and each of the two successive rates increases by at least `ACCEL_MIN_STEP_MM_YEAR` (default 5 mm/year). This is a simple heuristic, not statistical proof of acceleration.
8. Assign a user-defined *screening* category to each polygon; store date, rate, acceleration flag and category in the GeoJSON, then export categories to `data/api/zones.json`.

## User-defined screening categories

| Category (`risk_level`) | Recent LOS-away rate | Acceleration condition |
|---|---:|---|
| `low` | < 10 mm/year | Any |
| `watch` | 10 to < 40 mm/year | Any |
| `watch` | 40 to 100 mm/year (inclusive) | No confirmed acceleration, or insufficient data |
| `elevated` | 40 to 100 mm/year (inclusive) | Acceleration across three latest intervals |
| `high` | > 100 mm/year | Any |

`high` is the existing API's highest category; not `highest`, which the API schema does not recognize. If there is no reliable rate measurement, the category is `null`.

These are **user-selected engineering thresholds**, not risk boundaries calibrated by Nof et al. (2019). Nof et al. substantiate localized subsidence and temporal acceleration as useful sinkhole precursor evidence, not a universal 10/40/100 mm/year operational warning scale. The satellite measures LOS, not strictly vertical motion; reference and sign conventions are unverified. Treat all categories as *experimental research screening*, not real-world safety warnings. An area with no polygon is **not** certified low risk.

**Noise limitation:** 10 mm/year is only ~0.33 mm in a 12-day interval, much smaller than typical single-interferogram noise. Short-term low/watch classifications cannot be expected to exhaustively detect faint new subsidence. Multi-interval cumulative detection improves sensitivity but does not remove all errors.

## Main settings

```python
COHERENCE_MIN = 0.35
LOCAL_SIGMA_M = 100.0
BACKGROUND_SIGMA_M = 900.0
MINIMUM_MOVEMENT_MM = 3.0
MINIMUM_SNR = 2.5
MINIMUM_PIXELS = 4
DETECT_CUMULATIVE_ANOMALIES = True
ACCEL_MIN_STEP_MM_YEAR = 5.0
```

The spatial thresholds decide **where candidates exist**. The rate boundaries decide **how candidate polygons are labeled**. The two are deliberately independent.

## Test

```powershell
python -m pip install numpy scipy rasterio affine
python -m unittest discover -s tests -p "test_*v2.py" -v
python -m unittest discover -s tests -p "test_zone_risk_forwarding.py" -v
```

Use `python src/view_detection.py --show` to inspect the matching latest detection run; the existing viewer colors by `priority` (`watch/review/urgent_review`), while the definitive numerical screening bin is recorded as `properties.risk_level` in `anomalies.geojson` and exported through the API.
