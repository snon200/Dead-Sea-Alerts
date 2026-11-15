"""Prepare the cached Dead Sea acquisition-v2 stack for a smaller experiment.

No NISAR download or HDF5 access. Uses the NASA modified Copernicus DEM
v1.2 (cloud-optimized GeoTIFFs behind its VRT) only for the cropped region.

From the repository root:
    python src/nisar_prepare_dem_crop.py

Install: pip install numpy rasterio
Your existing Earthdata ~/.netrc login may be needed to read NASA's DEM.

Output is another acquisition-v2-compatible NPZ. The original is untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom

# ==================== EDIT ONLY THESE SETTINGS ========================
INPUT = Path("data/raw/dead_sea_fast.npz")
POLYGON = Path("data/geography/dead_sea.geojson")  # Your WGS84 lon/lat polygon
OUTPUT = Path("data/raw/dead_sea_cropped_dem.npz")

# NASA's modified Copernicus DEM (WGS84 ellipsoidal height, meters).
DEM_SOURCE = (
    "https://nisar.asf.earthdatacloud.nasa.gov/"
    "NISAR/DEM/v1.2/EPSG4326/EPSG4326.vrt"
)
# You may instead set DEM_SOURCE to a local georeferenced DEM GeoTIFF.
DEM_CACHE_DIR = Path("data/raw/dem_cache")

# For duplicate date intervals, retain the observation with the most good
# phase/coherence pixels in the final polygon. This does not alter values.
MIN_COHERENCE_FOR_SELECTION = 0.35
# =====================================================================

LOG = logging.getLogger("nisar.prepare")


def read_polygons(file: Path) -> list[dict]:
    data = json.loads(file.read_text(encoding="utf-8"))
    typ = data.get("type")
    if typ in ("Polygon", "MultiPolygon"):
        geometries = [data]
    elif typ == "Feature":
        geometries = [data.get("geometry")]
    elif typ == "FeatureCollection":
        geometries = [feature.get("geometry") for feature in data["features"]]
    else:
        raise ValueError("GeoJSON must be a Polygon, MultiPolygon, Feature or FeatureCollection")
    geometries = [g for g in geometries if g and g.get("type") in ("Polygon", "MultiPolygon")]
    if not geometries:
        raise ValueError("GeoJSON contains no Polygon or MultiPolygon geometry")
    return geometries


def intersect_and_crop(geom: list[dict], old_mask: np.ndarray, transform: Affine, crs: str):
    """Return overlap mask, tight row/column slices, and shifted affine."""
    projected = [transform_geom("EPSG:4326", crs, g) for g in geom]
    polygon_pixels = geometry_mask(
        projected, out_shape=old_mask.shape, transform=transform, invert=True
    )
    overlap = old_mask & polygon_pixels
    rows, cols = np.nonzero(overlap)
    if rows.size == 0:
        raise ValueError(
            "No overlap between the selected GeoJSON and the existing NPZ polygon "
            "(or no pixel centers in the intersection). Check longitude/latitude and region."
        )
    r0, r1 = int(rows.min()), int(rows.max()) + 1
    c0, c1 = int(cols.min()), int(cols.max()) + 1
    cropped_mask = overlap[r0:r1, c0:c1]
    cropped_transform = transform * Affine.translation(c0, r0)
    return cropped_mask, (slice(r0, r1), slice(c0, c1)), cropped_transform


def choose_unique_pairs(source, mask: np.ndarray, region, n: int):
    """Deduplicate identical acquisition *date* intervals by usable coverage."""
    phase = source["phase"]
    coh = source["coherence"]
    comps = source["connected_components"]
    refs = list(map(str, source["reference_times"]))
    secs = list(map(str, source["secondary_times"]))
    if any(a.shape != phase.shape for a in (coh, comps)) or phase.ndim != 3 or phase.shape[0] != n:
        raise ValueError("Expected phase/coherence/components of shape (pairs, rows, cols)")
    if len(refs) != n or len(secs) != n:
        raise ValueError("Timestamp arrays have the wrong length")
    rr, cc = region
    winners = {}
    dropped = []
    for i, (ref, sec) in enumerate(zip(refs, secs)):
        key = (ref[:10], sec[:10])
        if key[0] >= key[1]:
            raise ValueError(f"Bad interferogram date interval {key}")
        p, c, labels = phase[i, rr, cc], coh[i, rr, cc], comps[i, rr, cc]
        good = (
            mask & np.isfinite(p) & np.isfinite(c)
            & (c >= MIN_COHERENCE_FOR_SELECTION) & (c <= 1)
            & np.isfinite(labels) & (labels > 0) & (labels < 65535)
        )
        count = int(good.sum())
        avg = float(np.mean(c[good])) if count else -1.0
        score = (count, avg)
        if key not in winners or score > winners[key][1]:
            winners[key] = (i, score)
    if not any(score[0] > 0 for _, score in winners.values()):
        raise ValueError("No coherent interferogram pixels inside GeoJSON intersection")
    indices = sorted((v[0] for v in winners.values()), key=lambda i: (refs[i], secs[i]))
    dropped = [i for i in range(n) if i not in set(indices)]
    LOG.info("Unique intervals: %d kept, %d duplicate entries discarded", len(indices), len(dropped))
    if len(indices) == 0:
        raise ValueError("No interferogram pairs available")
    return indices, dropped


def nasa_dem(tf: Affine, crs: str, shape: tuple[int, int]):
    """Fetch/read only the DEM area covered by the cropped raster grid."""
    DEM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    signature = json.dumps(
        {"source": DEM_SOURCE, "transform": list(tf)[:6], "shape": shape, "crs": crs},
        sort_keys=True,
    )
    cache_name = "dem_" + hashlib.sha256(signature.encode()).hexdigest()[:18] + ".npy"
    cache = DEM_CACHE_DIR / cache_name
    if cache.exists():
        dem = np.load(cache, allow_pickle=False)
        if dem.shape == shape:
            LOG.info("Using cached DEM: %s", cache)
            return dem.astype(np.float32, copy=False)

    LOG.info("Loading NASA DEM for cropped grid: %d x %d cells", shape[1], shape[0])
    dem_source = str(DEM_SOURCE)
    source = (
        "/vsicurl/" + dem_source
        if dem_source.startswith(("https://", "http://")) else dem_source
    )
    cookies = DEM_CACHE_DIR / "gdal_cookies.txt"
    # NASA's documented /vsicurl access route. Reads remote COG windows via a VRT;
    # this is separate from the slow remote HDF5 reads that failed earlier.
    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="TRUE",
        GDAL_HTTP_NETRC="YES",
        GDAL_HTTP_COOKIEFILE=str(cookies.resolve()),
        GDAL_HTTP_COOKIEJAR=str(cookies.resolve()),
        GDAL_HTTP_TIMEOUT="45",
        GDAL_HTTP_MAX_RETRY="2",
        GDAL_HTTP_RETRY_DELAY="2",
    ):
        with rasterio.open(source) as dataset:
            with WarpedVRT(
                dataset, crs=crs, transform=tf,
                width=shape[1], height=shape[0],
                nodata=np.nan, resampling=Resampling.bilinear,
            ) as regridded:
                dem = np.asarray(regridded.read(1, masked=True).filled(np.nan), dtype=np.float32)
    if dem.shape != shape:
        raise RuntimeError("NASA DEM returned a raster with unexpected dimensions")
    if not np.isfinite(dem).any():
        raise RuntimeError("No finite NASA DEM heights in the requested raster region")
    np.save(cache, dem)
    LOG.info("Saved DEM cache: %s", cache)
    return dem


def prepare():
    if INPUT.resolve() == OUTPUT.resolve():
        raise ValueError("OUTPUT must be different from INPUT to preserve the raw data")
    if not INPUT.is_file():
        raise FileNotFoundError(f"Cannot find {INPUT}")
    if not POLYGON.is_file():
        raise FileNotFoundError(f"Cannot find {POLYGON}")
    geom = read_polygons(POLYGON)

    with np.load(INPUT, allow_pickle=False) as src:
        meta = json.loads(str(src["metadata_json"].item()))
        grid = meta["grid"]
        tf = Affine(*map(float, grid["affine"]))
        crs = f"EPSG:{int(grid['epsg'])}"
        old_mask = src["polygon_mask"].astype(bool)
        original_h, original_w = old_mask.shape
        n = int(src["phase"].shape[0])
        if src["phase"].shape[1:] != old_mask.shape:
            raise ValueError("NPZ polygon_mask and phase grid differ")
        if src["x"].shape != (original_w,) or src["y"].shape != (original_h,):
            raise ValueError("Original x/y dimensions do not match the raster grid")
        expected_x = tf.c + tf.a * (np.arange(original_w) + 0.5)
        expected_y = tf.f + tf.e * (np.arange(original_h) + 0.5)
        if not (np.allclose(src["x"], expected_x, rtol=0, atol=1e-3)
                and np.allclose(src["y"], expected_y, rtol=0, atol=1e-3)):
            raise ValueError("Original NPZ x/y disagree with its affine transform")

        mask, (rr, cc), new_tf = intersect_and_crop(geom, old_mask, tf, crs)
        h, w = mask.shape
        LOG.info(
            "Cropping %d x %d → %d x %d cells; intersection has %d selected cells",
            original_w, original_h, w, h, int(mask.sum()),
        )
        keep, dropped = choose_unique_pairs(src, mask, (rr, cc), n)

        # Read elevation first; if NASA access fails, no incomplete output is written.
        dem = nasa_dem(new_tf, crs, mask.shape)
        dem = np.where(mask, dem, np.nan).astype(np.float32)
        n_dem_valid = int(np.isfinite(dem[mask]).sum())
        LOG.info("DEM: %d / %d polygon cells have valid ellipsoidal heights", n_dem_valid, int(mask.sum()))
        if n_dem_valid == 0:
            raise ValueError("DEM contains no valid heights inside the intersection")

        result = {}
        for key in src.files:
            if key in ("metadata_json", "elevation", "polygon_mask", "x", "y"):
                continue
            array = src[key]
            if array.ndim >= 3 and array.shape[0] == n and array.shape[-2:] == old_mask.shape:
                # All time-dependent rasters are pair-indexed and spatially aligned.
                array = array[keep, rr, cc].copy()
                if np.issubdtype(array.dtype, np.floating):
                    array[..., ~mask] = np.nan
                elif np.issubdtype(array.dtype, np.integer):
                    array[..., ~mask] = 0  # invalid component labels, if stored as int
                else:
                    raise TypeError(f"Cannot mask raster {key} with dtype {array.dtype}")
            elif array.ndim == 2 and array.shape == old_mask.shape:
                array = array[rr, cc].copy()
                if np.issubdtype(array.dtype, np.floating):
                    array[~mask] = np.nan
                elif np.issubdtype(array.dtype, np.integer):
                    array[~mask] = 0
            elif array.ndim == 1 and len(array) == n:
                array = array[keep].copy()
            result[key] = array
        # After the loop, replace the spatial/metadata arrays, preserving indexing.
        result.update(
            elevation=dem,
            polygon_mask=mask,
            x=np.asarray(src["x"][cc], dtype=np.float64),
            y=np.asarray(src["y"][rr], dtype=np.float64),
        )
        # Update the provenance; keep wet/dry layers untouched (original fixed height).
        old_bbox = grid.get("polygon_bbox_lonlat")
        new_grid = dict(grid)
        new_grid.update(
            shape=[h, w],
            affine=[float(v) for v in tuple(new_tf)[:6]],
            original_polygon_bbox_lonlat=old_bbox,
        )
        new_grid["polygon_bbox_lonlat"] = [
            float(v) for v in rasterio.warp.transform_bounds(crs, "EPSG:4326", *rasterio.transform.array_bounds(h, w, new_tf))
        ]
        meta["grid"] = new_grid
        if len(meta.get("scenes", [])) == n:
            meta["scenes"] = [meta["scenes"][i] for i in keep]
        dem_is_nasa = str(DEM_SOURCE) == (
            "https://nisar.asf.earthdatacloud.nasa.gov/"
            "NISAR/DEM/v1.2/EPSG4326/EPSG4326.vrt"
        )
        method = dict(meta.get("method", {}))
        method.update(
            elevation_available=True,
            elevation_source=(
                "NASA modified Copernicus DEM v1.2, WGS84 ellipsoid"
                if dem_is_nasa else f"User-supplied DEM: {DEM_SOURCE} (verify vertical datum)"
            ),
            dem_resampling="bilinear to common 80 m grid",
            corrections_applied=False,
            corrections_evaluated_at_dem=False,
            atmosphere_warning=(
                "Existing wet/dry GeoTIFFs were extracted independently, possibly at "
                "one fixed height; adding the DEM DOES NOT re-evaluate those screens."
            ),
        )
        meta["method"] = method
        meta["source_npz"] = str(INPUT)
        meta["geojson_intersection"] = str(POLYGON)
        meta["prepared_utc"] = datetime.now(timezone.utc).isoformat()
        meta["duplicate_intervals_removed"] = len(dropped)
        meta["deduplication"] = {
            "method": "Keep best count of valid coherent pixels per acquisition-date interval",
            "kept_source_indices": [int(i) for i in keep],
            "dropped_source_indices": [int(i) for i in dropped],
            "coherence_threshold": MIN_COHERENCE_FOR_SELECTION,
        }
        layers = ("phase", "coherence", "ionosphere", "tropo_wet", "tropo_dry", "solid_earth_tides", "connected_components")
        meta["available_layers"] = {
            name: bool(np.isfinite(result[name]).any())
            for name in layers if name in result
        }
        meta["available_layers"]["elevation"] = True
        result["metadata_json"] = np.asarray(json.dumps(meta, allow_nan=False))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_name(OUTPUT.stem + ".partial.npz")
    np.savez_compressed(temp, **result)
    temp.replace(OUTPUT)
    LOG.info("Saved %s", OUTPUT)
    LOG.info("Final phase shape: %s | elevation: %s", result["phase"].shape, result["elevation"].shape)
    LOG.warning(
        "DEM is now aligned, but existing wet/dry corrections have NOT been recomputed "
        "at the DEM heights. Leave wet/dry disabled in processing-v2 until validated."
    )
    return OUTPUT


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    prepare()
