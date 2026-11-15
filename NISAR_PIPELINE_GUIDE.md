# Dead Sea Alerts NISAR pipeline

Run these commands from the repository root in an activated Python environment. The pipeline produces exploratory line-of-sight (LOS) deformation products relative to a candidate reference area and the first acquisition date. Use them for research screening. Independent validation is needed for the reference area, phase sign, atmospheric corrections, and interpretation.

## Setup

```powershell
python -m pip install numpy scipy rasterio affine matplotlib pyproj
```

For direct acquisition, also install `requests earthaccess h5py`. For fresh Harmony downloads, install `requests harmony-py`. Earthdata Login is required for remote NASA data. Obtain the WGS84 study polygon at `data/geography/dead_sea.geojson` and the raw input bundle for the selected route. A fresh clone needs these local data files. Keep Earthdata credentials and GDAL cookie files local.

## 1. Prepare a pairwise acquisition stack

**Local Harmony route.** Place completed scene directories under `data/raw/`, with their `manifest.json` and phase, coherence, ionosphere, and connected-component GeoTIFFs. If acquisition is needed, use `python src/nisar_acquisition.py --help` to configure a compatible AOI, date window, and connected time series. Check that the manifest family matches the track, frame and polarization in `nisar_acquisition_v2_fast.py`.

```powershell
python src/nisar_acquisition_v2_fast.py  # data/raw/dead_sea_fast.npz
python src/nisar_prepare_dem_crop.py    # data/raw/dead_sea_cropped_dem.npz
```

The crop step aligns a NASA DEM or a configured local DEM. It preserves existing correction screens at their original evaluation heights. Retain the original input manifests and DEM provenance.

**Direct route.** In `src/nisar_acquisition_v2.py`, check `GEOJSON_FILE`, dates, track, frame, and `MAX_PAIRS`. Set `MAX_PAIRS = None` for the full selected connected series. Run `python src/nisar_acquisition_v2.py`, then set `INPUT = Path("data/raw/dead_sea.npz")` in the processor. The direct script reads GUNW HDF5 and evaluates atmospheric corrections using the DEM. Its cache is under `data/cache/nisar_v2/`.

## 2. Generate and inspect cumulative displacement

In `src/nisar_processing_v2.py`, check `INPUT` (defaults to `data/raw/dead_sea_cropped_dem.npz`), the stable-land `REF_BBOX` in west/south/east/north order, the phase sign, and correction switches. Wet/dry troposphere and solid-Earth tides default to off. Verify correction availability before enabling them.

```powershell
python src/nisar_processing_v2.py
$RUN = 'run_v2_REPLACE_WITH_PRINTED_RUN_ID'
python src/view_nisar_stack.py --input "data/processed/$RUN" --list
python src/view_nisar_stack.py --input "data/processed/$RUN" --scene 3 --show
```

Stage 2 writes cumulative LOS `displacement.npy` in millimeters, a per-date `coherence.npy` support proxy, `metadata.json`, `ramp_fits.csv`, and an optional comparison plot. The first date is the zero reference. Compare the diagnostic panels because ramp subtraction can remove broad deformation. The optional temporal spike filter interpolates isolated interior-date outliers. Review `displacement_unfiltered.npy`, `temporal_replaced_mask.npy`, and `temporal_filter_report.csv` before interpreting the filtered result.

## 3. Detect and export zones

Set `PROCESSED` in `src/subsidence_detection_v2.py` to the exact `data/processed/<run-id>` directory. The parent-folder setting selects by modification time. The detector writes `anomalies.geojson`, velocity, acceleration, a candidate mask, and metadata under `data/detection/<same-run-id>/`. Rerunning detection for the same ID replaces its outputs.

```powershell
python src/subsidence_detection_v2.py
python src/view_detection.py --input "data/detection/$RUN" --processed "data/processed/$RUN" --show
python src/zone_service.py --run "$RUN" --data-root data --out "data/detection/$RUN/zones-preview.json"
```

Review the preview against `data/api/zones.json` before publishing it. The exporter calculates screening categories from fixed-footprint cumulative LOS histories. Stage 3 categories remain available in the anomaly GeoJSON for review. True latest-pair coherence requires a matching raw pair raster and may be null. The dashboard uses the published JSON through the [zones API](./API.md). OSM cache maintenance is covered in the [dashboard guide](./DASHBOARD.md).

## Legacy runs and checks

For historical Harmony-index runs, use `nisar_processing.py --input data/raw/index.json --ref-bbox W S E N --component los --no-troposphere`, with the original run's reference coordinates and correction settings. Then run `subsidence_detection.py --input data/processed/<run-id>`. Both viewers accept explicit run folders. Use `--help` for legacy options and keep each processed run with its matching detection folder.

```powershell
python -m unittest discover -s tests -p 'test_subsidence_detection_v2.py' -v
```
