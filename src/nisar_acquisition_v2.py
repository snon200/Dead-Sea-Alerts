"""Acquire co-registered NISAR GUNW layers directly from NASA (no Harmony jobs).

Edit CONFIG, then run from repository root:
    python src/nisar_acquisition_direct.py

Dependencies: pip install earthaccess requests h5py numpy scipy rasterio

NASA sources:
  GUNW: Earthdata CMR search + earthaccess HTTP-range reads of remote HDF5.
  DEM:  NASA modified Copernicus DEM, WGS84 *ellipsoidal* heights, remote VRT.

The output contains RAW pairwise phase layers (NOT corrected/cumulative), plus
wet/dry/tide correction tables evaluated at each pixel's DEM height. The grids
are co-registered, but numerical phase corrections are NOT applied here.

NASA documentation:
  https://nisar-docs.asf.alaska.edu/gunw/
  https://nisar-docs.asf.alaska.edu/metadata/
  https://nisar-docs.asf.alaska.edu/gdal/
  https://nisar-docs.asf.alaska.edu/nisar-dem/
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import logging
import os

import h5py
import numpy as np
import requests
from rasterio import Affine, Env, open as rio_open
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform_bounds, transform_geom
from scipy.interpolate import RegularGridInterpolator

# ========================== EDIT THESE SETTINGS ==========================
GEOJSON_FILE = Path(r"data\geography\dead_sea.geojson")
START_DATE = "2026-06-01"       # both reference and secondary acquisitions in window
END_DATE = "2026-11-15"         # inclusive
TRACK = 100                     # None = automatically choose a connected series
FRAME = 17                      # None = automatically choose a connected series
POLARIZATION = "HH"            # "HH" or "VV"
RELEASE = "auto"               # "auto", "validated", "provisional", "beta"
OUTPUT = Path("data/raw/dead_sea.npz")
CACHE_DIR = Path("data/cache/nisar_v2")
EPSG = 32636
PIXEL_M = 80.0
MAX_PAIRS = 2                # None = whole chosen series; set 1 for a first test

# NASA's official modified Copernicus DEM; may also be a local GeoTIFF path.
DEM_SOURCE = (
    "https://nisar.asf.earthdatacloud.nasa.gov/"
    "NISAR/DEM/v1.2/EPSG4326/EPSG4326.vrt"
)
# ========================================================================

COLLECTIONS = {
    "validated": "NISAR_L2_GUNW_V1",
    "provisional": "NISAR_L2_GUNW_PROVISIONAL_V1",
    "beta": "NISAR_L2_GUNW_BETA_V1",
}
DATASETS_2D = {
    "phase": "unwrappedPhase",
    "coherence": "coherenceMagnitude",
    "ionosphere": "ionospherePhaseScreen",
    "connected_components": "connectedComponents",
}
DATASETS_3D = {
    "tropo_wet": "wetTroposphericPhaseScreen",
    "tropo_dry": "hydrostaticTroposphericPhaseScreen",
    "solid_earth_tides": "slantRangeSolidEarthTidesPhase",
}
BASE = "/science/LSAR/GUNW"
CMR = "https://cmr.earthdata.nasa.gov/search"
LOG = logging.getLogger("nisar.direct")


def load_polygon(path: Path):
    """Load a GeoJSON Polygon/MultiPolygon/Feature/FeatureCollection in EPSG:4326."""
    obj = json.loads(path.read_text(encoding="utf-8"))
    typ = obj.get("type")
    if typ in ("Polygon", "MultiPolygon"):
        geometries = [obj]
    elif typ == "Feature":
        geometries = [obj["geometry"]]
    elif typ == "FeatureCollection":
        geometries = [f["geometry"] for f in obj["features"]]
    else:
        raise ValueError("Expected GeoJSON Polygon, MultiPolygon, Feature, or FeatureCollection")
    geometries = [g for g in geometries if g and g.get("type") in ("Polygon", "MultiPolygon")]
    if not geometries:
        raise ValueError("GeoJSON contains no polygon geometry")
    def points(g):
        rings = g["coordinates"] if g["type"] == "Polygon" else [r for poly in g["coordinates"] for r in poly]
        return [pt for ring in rings for pt in ring]
    pts = [pt for g in geometries for pt in points(g)]
    west, south = min(p[0] for p in pts), min(p[1] for p in pts)
    east, north = max(p[0] for p in pts), max(p[1] for p in pts)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Invalid polygon longitude/latitude bounds")
    return geometries, (west, south, east, north)


def common_grid(geometries, bbox, epsg=EPSG, pixel=PIXEL_M):
    target = f"EPSG:{epsg}"
    l, b, r, t = transform_bounds("EPSG:4326", target, *bbox, densify_pts=21)
    left = np.floor(l / pixel) * pixel
    top = np.ceil(t / pixel) * pixel
    width = int(np.ceil((r - left) / pixel))
    height = int(np.ceil((top - b) / pixel))
    transform = Affine(pixel, 0, left, 0, -pixel, top)
    poly_utm = [transform_geom("EPSG:4326", target, g) for g in geometries]
    inside = geometry_mask(poly_utm, (height, width), transform, invert=True)
    if not inside.any():
        raise ValueError("Polygon contains no output pixel centers at selected resolution")
    x = left + pixel * (np.arange(width) + 0.5)
    y = top - pixel * (np.arange(height) + 0.5)
    return transform, x, y, inside


def _parse_name(filename):
    parts = Path(filename).name.removesuffix(".h5").split("_")
    if len(parts) < 20 or parts[0] != "NISAR" or parts[3] != "GUNW":
        return None
    try:
        a = datetime.strptime(parts[11], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        b = datetime.strptime(parts[13], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return {
            "name": filename, "reference_time": a.isoformat(), "secondary_time": b.isoformat(),
            "track": int(parts[5]), "frame": int(parts[7]), "direction": parts[6],
            "mode": parts[9], "polarization_code": parts[10], "crid": parts[15],
        }
    except (ValueError, IndexError):
        return None


def _search_granules(bbox):
    """Search NASA CMR, preferring higher maturity for duplicate acquisitions."""
    start = date.fromisoformat(START_DATE)
    end = date.fromisoformat(END_DATE)
    if end < start:
        raise ValueError("END_DATE precedes START_DATE")
    names = list(COLLECTIONS) if RELEASE == "auto" else [RELEASE]
    if any(n not in COLLECTIONS for n in names):
        raise ValueError("Unknown RELEASE")
    session = requests.Session()
    session.headers["User-Agent"] = "NISAR-scientific-direct-acquisition/1.0"
    out = []
    # CMR's temporal field can be based on the earlier date of the pair.
    window = f"{(start - timedelta(days=60)).isoformat()}T00:00:00Z,{end.isoformat()}T23:59:59Z"
    for release in names:
        c = session.get(CMR + "/collections.json",
                        params={"short_name": COLLECTIONS[release], "page_size": 100}, timeout=60)
        c.raise_for_status()
        collections = c.json().get("feed", {}).get("entry", [])
        exact = [v for v in collections if v.get("short_name") == COLLECTIONS[release]]
        if not exact:
            LOG.info("No collection for %s", release)
            continue
        collection_id = exact[0]["id"]
        page = 1
        while True:
            r = session.get(CMR + "/granules.json", params={
                "collection_concept_id": collection_id, "bounding_box": ",".join(map(str, bbox)),
                "temporal": window, "page_num": page, "page_size": 200,
            }, timeout=90)
            r.raise_for_status()
            entries = r.json().get("feed", {}).get("entry", [])
            for item in entries:
                record = _parse_name(item.get("producer_granule_id") or item.get("title", ""))
                if record is None:
                    continue
                if TRACK is not None and record["track"] != TRACK:
                    continue
                if FRAME is not None and record["frame"] != FRAME:
                    continue
                pol_codes = {"SH": "HH", "DH": "HH", "SV": "VV", "DV": "VV", "QP": "BOTH"}
                present = pol_codes.get(record["polarization_code"][:2], "UNKNOWN")
                if present not in (POLARIZATION, "BOTH"):
                    continue
                # Both observations, not only secondary time, must be within chosen dates.
                if not (start <= date.fromisoformat(record["reference_time"][:10]) <= end and
                        start <= date.fromisoformat(record["secondary_time"][:10]) <= end):
                    continue
                links = [v.get("href", "") for v in item.get("links", [])
                         if not v.get("inherited", False)]
                urls = [u for u in links if u.lower().split("?")[0].endswith(".h5")
                        and u.startswith("https://")]
                stem = Path(record["name"]).stem.lower()
                exact = [u for u in urls if stem in u.lower()]
                if not exact:
                    LOG.warning("No matching HTTPS HDF5 link for %s", record["name"])
                    continue
                record.update(release=release, url=exact[0], updated=item.get("updated", ""))
                out.append(record)
            if len(entries) < 200:
                break
            page += 1
        LOG.info("CMR %s: %d eligible granules so far", release, len(out))
    # Deduplicate exact acquisitions (higher release maturity wins).
    out.sort(key=lambda v: v["updated"], reverse=True)
    out.sort(key=lambda v: names.index(v["release"]))
    unique = {}
    for s in out:
        k = (s["track"], s["frame"], s["direction"], s["mode"],
             s["reference_time"], s["secondary_time"], POLARIZATION)
        unique.setdefault(k, s)
    return list(unique.values())


def _select_series(scenes):
    """Select longest connected component within one track/frame/mode/polarization."""
    families = defaultdict(list)
    for s in scenes:
        k = (s["track"], s["frame"], s["direction"], s["mode"],
             POLARIZATION, s["release"], s["crid"])
        families[k].append(s)
    candidates = []
    for key, family in families.items():
        nodes = defaultdict(set)
        for index, s in enumerate(family):
            a, b = s["reference_time"], s["secondary_time"]
            nodes[a].add(index)
            nodes[b].add(index)
        remaining = set(nodes)
        while remaining:
            root = next(iter(remaining))
            group = {root}
            stack = [root]
            remaining.remove(root)
            edges = set()
            while stack:
                for idx in nodes[stack.pop()]:
                    edges.add(idx)
                    for endpoint in (family[idx]["reference_time"], family[idx]["secondary_time"]):
                        if endpoint not in group:
                            group.add(endpoint)
                            stack.append(endpoint)
                            remaining.discard(endpoint)
            pairs = sorted((family[i] for i in edges), key=lambda s: s["secondary_time"])
            candidates.append((len(pairs), len(group), pairs[-1]["secondary_time"], key, pairs))
    if not candidates:
        raise RuntimeError("No GUNW interferograms in requested polygon/date/track/frame")
    _, _, _, family_key, selected = max(candidates, key=lambda c: c[:3])
    if MAX_PAIRS is not None:
        if MAX_PAIRS < 1:
            raise ValueError("MAX_PAIRS must be positive or None")
        # Select an earliest chronological connected prefix for a small smoke test.
        chosen = [selected[0]]
        connected = {chosen[0]["reference_time"], chosen[0]["secondary_time"]}
        unused = selected[1:]
        while unused and len(chosen) < MAX_PAIRS:
            possible = next((s for s in unused if
                             s["reference_time"] in connected or s["secondary_time"] in connected), None)
            if possible is None:
                break
            chosen.append(possible)
            connected.update((possible["reference_time"], possible["secondary_time"]))
            unused.remove(possible)
        selected = sorted(chosen, key=lambda s: s["secondary_time"])
    LOG.info("Selected family %s: %d connected pairs", family_key, len(selected))
    return selected


def _h5_scalar(h5, path):
    if path not in h5:
        raise KeyError(f"Required NISAR dataset absent: {path}")
    return h5[path][()]


def _find_axes(h5, group):
    """Find 2D source grid coordinate axes, using the same coordinate group."""
    options = [group, group.rsplit("/", 1)[0], f"{BASE}/grids/frequencyA"]
    for root in dict.fromkeys(options):
        if all(f"{root}/{key}" in h5 for key in ("xCoordinates", "yCoordinates", "projection")):
            x = np.asarray(h5[f"{root}/xCoordinates"][:], dtype=float)
            y = np.asarray(h5[f"{root}/yCoordinates"][:], dtype=float)
            proj = int(h5[f"{root}/projection"][()])
            return x, y, f"EPSG:{proj}"
    raise KeyError(f"Cannot locate xCoordinates/yCoordinates/projection near {group}")


def _range_slice(axis, lo, hi, margin=2):
    """ROI with interpolation support, works with increasing or decreasing axes."""
    arr = np.asarray(axis, dtype=float)
    if arr.ndim != 1 or len(arr) < 2:
        raise ValueError("Expected 1D coordinate axis with at least two coordinates")
    step = np.diff(arr)
    if not (np.all(step > 0) or np.all(step < 0)):
        raise ValueError("Nonmonotonic geographic coordinate axis")
    ix = np.flatnonzero((arr >= lo) & (arr <= hi))
    if not ix.size:
        # Need overlap even when AOI is smaller than spacing of coarse metadata cube.
        mid = int(np.argmin(abs(arr - (lo + hi) / 2)))
        ix = np.array([mid])
    start = max(0, int(ix.min()) - margin)
    stop = min(len(arr), int(ix.max()) + margin + 1)
    return slice(start, stop)


def _nodata_to_nan(data, dataset):
    a = np.asarray(data, dtype=np.float32)
    nodata = dataset.attrs.get("_FillValue", dataset.attrs.get("missing_value", None))
    if nodata is not None:
        try:
            a[a == np.float32(np.asarray(nodata).flat[0])] = np.nan
        except (TypeError, ValueError):
            pass
    a[~np.isfinite(a)] = np.nan
    return a


def _read_2d(h5, group, variable, dst_transform, shape, dst_crs):
    path = group + "/" + variable
    if path not in h5:
        raise KeyError(f"Required NISAR science layer missing: {path}")
    ds = h5[path]
    x, y, src_crs = _find_axes(h5, group)
    if ds.shape != (len(y), len(x)):
        raise ValueError(f"Unexpected dimensions for {path}: {ds.shape}")
    # Window only the geographic patch; never read the full satellite raster.
    l, b, r, t = transform_bounds(dst_crs, src_crs,
                                   *(_bounds(dst_transform, shape)), densify_pts=12)
    sx, sy = _range_slice(x, l, r), _range_slice(y, b, t)
    if sx.stop - sx.start < 2 or sy.stop - sy.start < 2:
        raise ValueError(f"Not enough source pixels for {path}")
    sub = _nodata_to_nan(ds[sy, sx], ds)
    xsub, ysub = x[sx], y[sy]
    dx, dy = float(np.median(np.diff(xsub))), float(np.median(np.diff(ysub)))
    if not (np.allclose(np.diff(xsub), dx) and np.allclose(np.diff(ysub), dy)):
        raise ValueError(f"Nonuniform raster grid: {path}")
    transform = Affine(dx, 0, xsub[0] - dx / 2, 0, dy, ysub[0] - dy / 2)
    dest = np.full(shape, np.nan, dtype=np.float32)
    # Nearest avoids mixing phase values across different unwrapping components.
    reproject(sub, dest, src_transform=transform, src_crs=src_crs,
              src_nodata=np.nan, dst_transform=dst_transform, dst_crs=dst_crs,
              dst_nodata=np.nan, resampling=Resampling.nearest)
    return dest


def _bounds(transform, shape):
    h, w = shape
    x0, y0 = transform * (0, 0)
    x1, y1 = transform * (w, h)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _read_cube(h5, variable, xout, yout, dem, inside, dst_crs):
    """Select small sections of NISAR 3D lookup tables and interpolate (z,y,x)."""
    root = f"{BASE}/metadata/radarGrid"
    path = f"{root}/{variable}"
    if path not in h5:
        raise KeyError(f"Required NISAR correction cube is absent: {path}")
    ds = h5[path]
    units = ds.attrs.get("units", "")
    if isinstance(units, bytes):
        units = units.decode("utf-8")
    if str(units).lower().strip() not in ("", "rad", "radian", "radians"):
        raise ValueError(f"Unexpected units for {path}: {units!r}; expected radians")
    x = np.asarray(h5[f"{root}/xCoordinates"][:], dtype=float)
    y = np.asarray(h5[f"{root}/yCoordinates"][:], dtype=float)
    z = np.asarray(h5[f"{root}/heightAboveEllipsoid"][:], dtype=float)
    epsg = int(_h5_scalar(h5, f"{root}/projection"))
    if ds.shape != (len(z), len(y), len(x)):
        raise ValueError(f"Unexpected 3D cube shape for {path}: {ds.shape}")
    if not np.all(np.diff(z) > 0):
        raise ValueError("Expected increasing radarGrid heightAboveEllipsoid")
    rows, cols = np.nonzero(inside & np.isfinite(dem))
    if not len(rows):
        raise RuntimeError("DEM has no finite values inside polygon")
    xquery, yquery = np.meshgrid(xout, yout)
    target_crs = f"EPSG:{epsg}"
    if target_crs != dst_crs:
        from rasterio.warp import transform
        xx, yy = transform(dst_crs, target_crs, xquery[rows, cols], yquery[rows, cols])
        qx = np.asarray(xx)
        qy = np.asarray(yy)
    else:
        qx, qy = xquery[rows, cols], yquery[rows, cols]
    zquery = dem[rows, cols].astype(float)
    # Check rather than extrapolating the atmospheric physics to unsupported heights.
    if (np.min(zquery) < z[0]) or (np.max(zquery) > z[-1]):
        raise ValueError(f"DEM elevation [{zquery.min():.1f},{zquery.max():.1f}] m "
                         f"outside radarGrid vertical range [{z[0]:.1f},{z[-1]:.1f}] m")
    sx = _range_slice(x, float(qx.min()), float(qx.max()))
    sy = _range_slice(y, float(qy.min()), float(qy.max()))
    sz = _range_slice(z, float(zquery.min()), float(zquery.max()), margin=1)
    if min(sx.stop - sx.start, sy.stop - sy.start, sz.stop - sz.start) < 2:
        raise ValueError(f"Not enough interpolation support in {path}")
    cube = _nodata_to_nan(ds[sz, sy, sx], ds)
    zz, yy, xx = z[sz], y[sy], x[sx]
    # RGI expects axes monotone; reversing descending x/y is safe.
    if yy[0] > yy[-1]:
        yy, cube = yy[::-1], cube[:, ::-1, :]
    if xx[0] > xx[-1]:
        xx, cube = xx[::-1], cube[:, :, ::-1]
    interp = RegularGridInterpolator((zz, yy, xx), cube, method="linear",
                                     bounds_error=False, fill_value=np.nan)
    values = interp(np.column_stack((zquery, qy, qx)))
    out = np.full(dem.shape, np.nan, dtype=np.float32)
    out[rows, cols] = values.astype(np.float32)
    return out


def _load_dem(dst_transform, shape, inside):
    """Warp a small remote window of NASA's mission DEM; cache the aligned array."""
    spec = {"source": DEM_SOURCE, "epsg": EPSG, "shape": shape,
            "transform": tuple(dst_transform)[:6]}
    key = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
    cache_file = CACHE_DIR / f"dem_{key}.npy"
    if cache_file.exists():
        dem = np.load(cache_file)
        if dem.shape == shape:
            LOG.info("Using cached NASA DEM %s", cache_file)
            return np.where(inside, dem, np.nan)
    LOG.info("Reading NASA DEM subset from %s", DEM_SOURCE)
    # This is an official NASA VRT over COG tiles; reads are spatially windowed.
    source = "/vsicurl/" + DEM_SOURCE if DEM_SOURCE.startswith("https://") else DEM_SOURCE
    with Env(GDAL_DISABLE_READDIR_ON_OPEN="TRUE", GDAL_HTTP_NETRC="YES",
             GDAL_HTTP_COOKIEFILE=str(CACHE_DIR / "gdal_cookies.txt"),
             GDAL_HTTP_COOKIEJAR=str(CACHE_DIR / "gdal_cookies.txt"),
             GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
             CPL_VSIL_CURL_CHUNK_SIZE="2097152"):
        with rio_open(source) as ds:
            with WarpedVRT(ds, crs=f"EPSG:{EPSG}", transform=dst_transform,
                           width=shape[1], height=shape[0],
                           nodata=np.nan, resampling=Resampling.bilinear) as vrt:
                dem = vrt.read(1, masked=True).filled(np.nan).astype(np.float32)
    if not np.isfinite(dem[inside]).any():
        raise RuntimeError("NASA DEM returned no elevation values for the polygon")
    np.save(cache_file, dem)
    return np.where(inside, dem, np.nan)


def _read_one_scene(scene, earthaccess, dst_transform, x, y, dem, inside):
    cache_key = hashlib.sha256(json.dumps({
        "url": scene["url"], "epsg": EPSG, "resolution": PIXEL_M,
        "x": [float(x[0]), float(x[-1]), len(x)],
        "y": [float(y[0]), float(y[-1]), len(y)],
        "dem_source": DEM_SOURCE,
    }, sort_keys=True).encode()).hexdigest()[:20]
    cached = CACHE_DIR / f"pair_{cache_key}.npz"
    if cached.is_file():
        LOG.info("Pair cache hit: %s", scene["name"])
        with np.load(cached, allow_pickle=False) as f:
            return {name: f[name] for name in (*DATASETS_2D, *DATASETS_3D)}
    LOG.info("Reading remote granule %s", scene["name"])
    # Earthaccess uses seekable HTTP range requests, not a whole HDF5 download.
    remote = earthaccess.open([scene["url"]], provider="ASF", show_progress=False,
                              open_kwargs={"block_size": 2 * 1024 * 1024,
                                           "cache_type": "blockcache"})
    if len(remote) != 1:
        raise RuntimeError(f"Could not remotely open {scene['name']}")
    out = {}
    with closing(remote[0]) as remote_file:
        with h5py.File(remote_file, "r") as h5:
            group = f"{BASE}/grids/frequencyA/unwrappedInterferogram/{POLARIZATION}"
            for name, variable in DATASETS_2D.items():
                out[name] = _read_2d(h5, group, variable, dst_transform,
                                     dem.shape, f"EPSG:{EPSG}")
                out[name][~inside] = np.nan
            for name, variable in DATASETS_3D.items():
                out[name] = _read_cube(h5, variable, x, y, dem, inside, f"EPSG:{EPSG}")
    # Missing corrections must not silently turn into a partially corrected product.
    for key in ("phase", "coherence", "ionosphere", *DATASETS_3D):
        valid_count = int(np.isfinite(out[key][inside]).sum())
        if valid_count == 0:
            raise RuntimeError(f"No valid {key} pixels in polygon for {scene['name']}")
        if valid_count < int(inside.sum() * 0.8):
            LOG.warning("Only %d/%d valid %s pixels in %s", valid_count,
                        int(inside.sum()), key, scene["name"])
    temp = cached.with_name(cached.stem + ".partial.npz")
    np.savez_compressed(temp, **out)
    os.replace(temp, cached)
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    polygon, bbox = load_polygon(GEOJSON_FILE)
    tf, x, y, inside = common_grid(polygon, bbox)
    LOG.info("Common grid: %d columns x %d rows, EPSG:%d, %.1f m", len(x), len(y), EPSG, PIXEL_M)
    scenes = _select_series(_search_granules(bbox))
    LOG.info("Selected %d interferograms, from %s to %s", len(scenes),
             scenes[0]["reference_time"][:10], scenes[-1]["secondary_time"][:10])

    import earthaccess
    earthaccess.login(persist=True)  # prompts or uses cached Earthdata credentials
    dem = _load_dem(tf, inside.shape, inside)
    LOG.info("DEM elevation inside polygon: %.1f to %.1f m ellipsoid",
             np.nanmin(dem), np.nanmax(dem))

    per_pair = []
    for i, scene in enumerate(scenes, 1):
        LOG.info("Scene %d/%d", i, len(scenes))
        data = _read_one_scene(scene, earthaccess, tf, x, y, dem, inside)
        LOG.info("  Valid phase pixels: %d/%d", np.isfinite(data["phase"]).sum(), inside.sum())
        per_pair.append(data)
    arrays = {name: np.stack([p[name] for p in per_pair]).astype(np.float32)
              for name in (*DATASETS_2D, *DATASETS_3D)}
    # Preserve CC labels but NaN outside source/ROI; no inference of missing labels.
    arrays.update(elevation=dem.astype(np.float32),
                  x=x.astype(np.float64), y=y.astype(np.float64),
                  polygon_mask=inside,
                  reference_times=np.asarray([s["reference_time"] for s in scenes]),
                  secondary_times=np.asarray([s["secondary_time"] for s in scenes]),
                  granule_ids=np.asarray([s["name"] for s in scenes]))
    metadata = {
        "description": "Raw aligned GUNW interferogram pairs + corrections at per-pixel DEM height",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": "NASA ASF Earthdata; NASA modified Copernicus DEM v1.2",
        "polygon_geojson_path": str(GEOJSON_FILE),
        "grid": {"epsg": EPSG, "pixel_size_m": PIXEL_M,
                 "shape": list(inside.shape), "affine": list(tuple(tf)[:6]),
                 "axis_order": ["pair", "row_north_to_south", "col_west_to_east"],
                 "polygon_bbox_lonlat": list(bbox)},
        "units": {"phase": "rad", "coherence": "1", "ionosphere": "rad",
                  "tropo_wet": "rad", "tropo_dry": "rad",
                  "solid_earth_tides": "rad", "elevation": "m WGS84 ellipsoid",
                  "connected_components": "label"},
        "method": {"science_layers": "remote HDF5 ROI nearest-neighbor regridding",
                   "correction_cubes": "3D trilinear (ellipsoidal elevation, northing, easting)",
                   "dem": "NASA modified Copernicus DEM remote COG/VRT bilinear resampling",
                   "corrections_applied": False,
                   "time_axis": "interferogram pair, NOT cumulative acquisitions"},
        "scenes": scenes,
    }
    arrays["metadata_json"] = np.asarray(json.dumps(metadata, allow_nan=False))
    temp = OUTPUT.with_name(OUTPUT.stem + ".partial.npz")
    np.savez_compressed(temp, **arrays)
    os.replace(temp, OUTPUT)
    LOG.info("Saved %s (shape of 3D layers: %s)", OUTPUT, arrays["phase"].shape)
    LOG.info("Example: d=np.load('%s'); d['tropo_wet'][0]", OUTPUT)


if __name__ == "__main__":
    main()
