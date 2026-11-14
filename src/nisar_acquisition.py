#!/usr/bin/env python3
"""NISAR GUNW acquisition layer: CMR discovery -> Harmony subsets -> validated manifest.

Install:
    python -m pip install requests harmony-py rasterio

Sign in with NASA Earthdata Login using ~/.netrc or EDL_USERNAME/EDL_PASSWORD.

Two example AOIs (WGS84; BBOX ORDER = WEST SOUTH EAST NORTH):
    # Ein Gedi, small (~10 x 8 km):  --bbox 35.35 31.43 35.45 31.50
    # Western Dead Sea shore:      --bbox 35.30 30.95 35.54 31.80

Examples (PowerShell / shell):
    python nisar_acquisition.py --bbox 35.35 31.43 35.45 31.50 --search-only
    python nisar_acquisition.py --bbox 35.35 31.43 35.45 31.50 --start 2026-10-01 --max-scenes 3
    python nisar_acquisition.py --bbox 35.30 30.95 35.54 31.80 --max-scenes 10 --with-radar-metadata
    python nisar_acquisition.py --bbox 35.35 31.43 35.45 31.50 --with-radar-metadata --height-m -400
    python nisar_acquisition.py --center 31.46 35.39 --size-km 8 --max-scenes 2

Import into the pipeline:
    from nisar_acquisition import acquire_gunw
    records = acquire_gunw((35.35, 31.43, 35.45, 31.50),
                           start_date="2026-10-01", output_dir="data/raw")

Notes:
* Optional --with-radar-metadata streams only HDF5 metadata and adds exact
  radar frequency and a georeferenced incidence_angle_deg.tif. The default
  --height-m -400 is a constant WGS84 ellipsoid height approximation, NOT
  terrain-following. A height-aware DEM interpolation is needed later.
* This module downloads phase/coherence/ionosphere/connected-components. It does
  not apply any corrections or derive ground displacement (that is Stage 2).
* Each scene is downloaded into its own directory to avoid filename collisions.
* A manifest is marked "complete" ONLY if all requested COGs are present and
  georeferenced to the same pixel grid; partial scenes remain non-complete.
* GUNW pair names contain both reference and secondary acquisitions. The date
  filter here uses the SECONDARY (later) acquisition date when parseable.
* CMR BBOX is an intersection search: scene footprints need not cover the
  entire requested area. Subsequent stages must respect actual raster coverage.
* A single GUNW pair is a relative measurement. Do not combine geometries,
  tracks, or interferometric pairs without an explicit time-series strategy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from contextlib import closing
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("nisar.acquisition")
CMR_BASE = "https://cmr.earthdata.nasa.gov/search"
COLLECTIONS = {
    "validated": "NISAR_L2_GUNW_V1",
    "provisional": "NISAR_L2_GUNW_PROVISIONAL_V1",
    "beta": "NISAR_L2_GUNW_BETA_V1",
}
RELEASE_ORDER = tuple(COLLECTIONS)
LAYERS = {
    "phase": "unwrappedPhase",
    "coherence": "coherenceMagnitude",
    "ionosphere": "ionospherePhaseScreen",
    "connected_components": "connectedComponents",
    "ionosphere_uncertainty": "ionospherePhaseScreenUncertainty",
}
DEFAULT_LAYERS = ("phase", "coherence", "ionosphere", "connected_components")
DEFAULT_BBOX = (35.35, 31.43, 35.45, 31.50)


@dataclass(frozen=True)
class Scene:
    collection_id: str
    short_name: str
    release: str
    granule_id: str
    granule_name: str
    cmr_time_start: str | None
    cmr_time_end: str | None
    reference_time: str | None
    secondary_time: str | None
    track: int | None
    frame: int | None
    direction: str | None
    pol_code: str | None
    cycle: int | None
    mode: str | None
    updated: str | None

    @property
    def pair_key(self) -> tuple[Any, ...]:
        # Do NOT merge different tracks, frames, geometry or polarizations.
        if self.track is None or self.reference_time is None or self.secondary_time is None:
            return (self.granule_id,)
        return (self.track, self.frame, self.direction, self.mode,
                self.pol_code, self.reference_time, self.secondary_time)


class AcquisitionError(RuntimeError):
    """An acquisition or validation step failed."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_bbox(bbox: Iterable[float]) -> tuple[float, float, float, float]:
    values = tuple(map(float, bbox))
    if len(values) != 4:
        raise ValueError("bbox requires four coordinates: west south east north")
    w, s, e, n = values
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Bounding-box coordinates must be finite")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError("Invalid bbox: expected W < E and S < N in WGS84 degrees")
    return w, s, e, n


def bbox_from_center(lat: float, lon: float, size_km: float) -> tuple[float, float, float, float]:
    if not math.isfinite(size_km) or size_km <= 0 or abs(lat) > 88:
        raise ValueError("size_km must be positive and latitude must be within +/-88 degrees")
    if not (-180 <= lon <= 180):
        raise ValueError("Invalid longitude")
    dlat = size_km / (2 * 111.32)
    dlon = size_km / (2 * 111.32 * math.cos(math.radians(lat)))
    return validate_bbox((lon - dlon, lat - dlat, lon + dlon, lat + dlat))


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}': use YYYY-MM-DD") from exc


def cmr_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=4, backoff_factor=1.0,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset(("GET",)))
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers["User-Agent"] = "DeadSeaNISARGUNW/1.0 (research acquisition)"
    return session


def cmr_get(session: requests.Session, resource: str, params: dict[str, Any]) -> dict:
    response = session.get(f"{CMR_BASE}/{resource}.json", params=params, timeout=(15, 60))
    response.raise_for_status()
    payload = response.json()
    if "feed" not in payload:
        raise AcquisitionError(f"Unexpected CMR response for {resource}: missing feed")
    return payload


def get_collection_id(session: requests.Session, short_name: str) -> str | None:
    entries = cmr_get(session, "collections", {"short_name": short_name, "page_size": 100})["feed"].get("entry", [])
    matching = [entry for entry in entries if entry.get("short_name") == short_name]
    if not matching:
        return None
    # The short_name includes V1; do not select a different collection by fuzzy matching.
    return str(matching[0]["id"])


def parse_gunw_filename(name: str) -> dict[str, Any]:
    """Parse documented paired-acquisition GUNW naming; tolerate unknown names."""
    base = Path(name).name
    if base.lower().endswith(".h5"):
        base = base[:-3]
    parts = base.split("_")
    if len(parts) < 20 or parts[0].upper() != "NISAR" or parts[3].upper() != "GUNW":
        return {}
    try:
        ref = datetime.strptime(parts[11], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        sec = datetime.strptime(parts[13], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return {
            "track": int(parts[5]), "frame": int(parts[7]),
            "direction": parts[6].upper(), "cycle": int(parts[4]),
            "mode": parts[9], "pol_code": parts[10].upper(),
            "reference_time": ref.isoformat(), "secondary_time": sec.isoformat(),
        }
    except (ValueError, IndexError):
        return {}


def scene_from_cmr(entry: dict[str, Any], release: str, short_name: str, collection_id: str) -> Scene:
    name = str(entry.get("producer_granule_id") or entry.get("title") or entry["id"])
    parsed = parse_gunw_filename(name)
    return Scene(
        collection_id=collection_id, short_name=short_name, release=release,
        granule_id=str(entry["id"]), granule_name=name,
        cmr_time_start=entry.get("time_start"), cmr_time_end=entry.get("time_end"),
        reference_time=parsed.get("reference_time"), secondary_time=parsed.get("secondary_time"),
        track=parsed.get("track"), frame=parsed.get("frame"),
        direction=parsed.get("direction"), pol_code=parsed.get("pol_code"),
        cycle=parsed.get("cycle"), mode=parsed.get("mode"),
        updated=entry.get("updated"),
    )


def find_scenes(
    session: requests.Session,
    bbox: tuple[float, float, float, float],
    start_date: str,
    end_date: str | None,
    release: str = "auto",
    search_cap: int = 600,
) -> list[Scene]:
    """Search CMR pages for *all* release tiers; prefer validated duplicates.

    The search cap applies per collection. Warn when the result set is truncated.
    Filtering the later acquisition after CMR search avoids confusing a pair's
    reference time with the date of its later acquisition.
    """
    bbox = validate_bbox(bbox)
    start = parse_day(start_date)
    end = parse_day(end_date) if end_date else None
    if end is not None and end < start:
        raise ValueError("end_date must not precede start_date")
    if release not in ("auto", *RELEASE_ORDER):
        raise ValueError(f"Unknown release: {release}")
    if search_cap < 1:
        raise ValueError("search_cap must be >= 1")

    names = RELEASE_ORDER if release == "auto" else (release,)
    found: list[Scene] = []
    interval = f"{start.isoformat()}T00:00:00Z,{end.isoformat()+'T23:59:59Z' if end else ''}"
    for tier in names:
        short_name = COLLECTIONS[tier]
        collection_id = get_collection_id(session, short_name)
        if not collection_id:
            LOG.warning("No CMR collection found for %s", short_name)
            continue
        fetched = 0
        page_num = 1
        while fetched < search_cap:
            page_size = min(200, search_cap - fetched)
            data = cmr_get(session, "granules", {
                "collection_concept_id": collection_id,
                "bounding_box": ",".join(str(n) for n in bbox),
                "temporal": interval,
                "page_size": page_size,
                "page_num": page_num,
                "sort_key[]": "-start_date",
            })
            entries = data["feed"].get("entry", [])
            if not entries:
                break
            fetched += len(entries)
            for entry in entries:
                scene = scene_from_cmr(entry, tier, short_name, collection_id)
                # CMR pair metadata can differ by catalog. Use parsed later time
                # when present; otherwise retain CMR's temporal search result.
                if scene.secondary_time:
                    observation_day = date.fromisoformat(scene.secondary_time[:10])
                    if observation_day < start or (end and observation_day > end):
                        continue
                found.append(scene)
            if len(entries) < page_size:
                break
            page_num += 1
        if fetched >= search_cap:
            LOG.warning("CMR results for %s may be truncated at --search-cap=%d", short_name, search_cap)
        LOG.info("CMR %s: %d queried records", tier, fetched)

    # Keep first of duplicates after sorting by release priority, newest processing.
    # Stable sort: within each release prefer the latest processing update,
    # then globally prefer validated > provisional > beta.
    found.sort(key=lambda s: s.updated or "", reverse=True)
    found.sort(key=lambda s: RELEASE_ORDER.index(s.release))
    by_pair: dict[tuple[Any, ...], Scene] = {}
    for scene in found:
        by_pair.setdefault(scene.pair_key, scene)
    result = list(by_pair.values())
    result.sort(key=lambda s: s.secondary_time or s.cmr_time_start or "", reverse=True)
    return result


def select_polarization(scene: Scene, requested: str = "auto") -> str | None:
    if requested not in ("auto", "HH", "VV"):
        raise ValueError("polarization must be auto, HH, or VV")
    # Code is 2-character frequency-A config, e.g. SH, SV, DH, DV, QP.
    code = (scene.pol_code or "")[:2]
    available = {"SH": ("HH",), "DH": ("HH",),
                 "SV": ("VV",), "DV": ("VV",),
                 "QP": ("HH", "VV")}.get(code)
    if available is None:
        if requested == "auto":
            raise AcquisitionError(
                f"Cannot infer polarization from {scene.granule_name}. "
                "Specify --polarization HH or --polarization VV."
            )
        return requested
    if requested == "auto":
        return available[0]
    return requested if requested in available else None


def variable_paths(layers: Iterable[str], polarization: str) -> dict[str, str]:
    if polarization not in ("HH", "VV"):
        raise ValueError("polarization must be HH or VV")
    chosen = tuple(dict.fromkeys(layers))
    unknown = set(chosen) - set(LAYERS)
    if unknown:
        raise ValueError(f"Unknown layers: {sorted(unknown)}")
    if not chosen:
        raise ValueError("No layers selected")
    base = f"/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/{polarization}"
    return {layer: f"{base}/{LAYERS[layer]}" for layer in chosen}


def classify_tif(filename: str) -> str | None:
    compact = re.sub(r"[^a-z0-9]", "", Path(filename).stem.lower())
    # Check longer tokens first to avoid confusing uncertainty with phase screen.
    for key in ("ionosphere_uncertainty", "ionosphere", "phase", "coherence", "connected_components"):
        if LAYERS[key].lower() in compact:
            return key
    return None


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOG.warning("Ignoring corrupted manifest %s", path)
        return None


def raster_metadata(paths: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Validate GeoTIFFs against one another; no full raster loaded into RAM."""
    try:
        import rasterio
    except ImportError as exc:
        raise AcquisitionError("Missing rasterio: python -m pip install rasterio") from exc

    result: dict[str, dict[str, Any]] = {}
    reference = None
    for layer, name in paths.items():
        path = Path(name)
        if not path.is_file() or path.stat().st_size == 0:
            raise AcquisitionError(f"Missing/empty {layer} raster: {path}")
        try:
            with rasterio.open(path) as ds:
                if ds.count != 1 or ds.crs is None or ds.width == 0 or ds.height == 0:
                    raise AcquisitionError(f"Invalid georeference or bands for {layer}: {path}")
                geo = (ds.crs, ds.transform, ds.width, ds.height)
                # COGs from the same 80 m group should have identical grids.
                if reference is None:
                    reference = geo
                elif geo != reference:
                    raise AcquisitionError(f"GeoTIFF grids differ: {layer}: {path}")
                result[layer] = {
                    "path": str(path.resolve()), "bytes": path.stat().st_size,
                    "crs": ds.crs.to_string(), "width": ds.width, "height": ds.height,
                    "transform": list(ds.transform)[:6], "bounds": list(ds.bounds),
                    "dtype": ds.dtypes[0], "nodata": ds.nodata,
                }
        except Exception as exc:
            if isinstance(exc, AcquisitionError):
                raise
            raise AcquisitionError(f"Cannot read {layer}: {path}: {exc}") from exc
    return result


def inspect_outputs(scene_dir: Path, layers: Iterable[str]) -> dict[str, dict[str, Any]]:
    requested = set(layers)
    found: dict[str, str] = {}
    for path in sorted(scene_dir.iterdir()):
        if path.suffix.lower() not in (".tif", ".tiff"):
            continue
        kind = classify_tif(path.name)
        if kind not in requested:
            continue
        if kind in found:
            raise AcquisitionError(f"Ambiguous duplicate layer '{kind}' in {scene_dir}")
        found[kind] = str(path)
    missing = requested - set(found)
    if missing:
        raise AcquisitionError(f"Missing required Harmony layers: {', '.join(sorted(missing))} in {scene_dir}")
    return raster_metadata(found)


def scene_folder(output_dir: Path, scene: Scene, bbox: tuple, layers: Iterable[str],
                 polarization: str, epsg: int | None) -> Path:
    params = {"granule_id": scene.granule_id, "bbox": bbox,
              "layers": sorted(layers), "polarization": polarization, "epsg": epsg}
    digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]
    identifier = re.sub(r"[^a-zA-Z0-9_-]", "_", scene.granule_id)[:56]
    return output_dir / f"{identifier}_{digest}"


def load_harmony():
    try:
        from harmony import BBox, Client, Collection, Request
    except ImportError as exc:
        raise AcquisitionError("Missing harmony-py: python -m pip install harmony-py") from exc
    return BBox, Client, Collection, Request


def harmony_subset(
    scene: Scene,
    bbox: tuple[float, float, float, float],
    layers: Iterable[str],
    polarization: str,
    epsg: int | None,
    directory: Path,
    existing_job_id: str | None,
    on_submit,
    client: Any | None = None,
) -> str:
    """Submit or resume one scene job. Return the Harmony job ID."""
    BBox, Client, Collection, Request = load_harmony()
    client = client if client is not None else Client()
    job_id = existing_job_id
    if not job_id:
        options: dict[str, Any] = {
            "collection": Collection(id=scene.collection_id),
            "granule_id": [scene.granule_id],
            "spatial": BBox(*bbox),
            "variables": list(variable_paths(layers, polarization).values()),
            "format": "image/tiff",
        }
        if epsg is not None:
            options["crs"] = f"EPSG:{epsg}"
        job_id = client.submit(Request(**options))
        on_submit(str(job_id))   # Persist before long-running wait/download.
    LOG.info("Waiting for Harmony job %s", job_id)
    client.wait_for_processing(job_id, show_progress=True)
    futures = client.download_all(job_id, directory=str(directory), overwrite=False)
    files = [str(f.result()) for f in futures]  # propagate download failures
    if not files and not list(directory.glob("*.tif*")):
        raise AcquisitionError(f"Harmony job {job_id} returned no GeoTIFFs")
    LOG.info("Harmony job %s downloaded %d outputs", job_id, len(files))
    return str(job_id)


def fetch_radar_metadata(
    scene: Scene,
    phase_tif: str | Path,
    destination: str | Path,
    *,
    height_m: float = -400.0,
) -> dict[str, Any]:
    """Stream only HDF5 metadata via Earthdata and make a phase-aligned angle TIFF.

    The incidence angle is interpolated at a CONSTANT WGS84 ELLIPSOID HEIGHT
    (`height_m`). It is not terrain-following: for accurate geometry use a DEM
    to interpolate the cube in 3D per pixel instead. Never assume that a Dead
    Sea water-level/orthometric elevation is an ellipsoid height.

    Dependencies (optional): earthaccess, h5py, numpy, rasterio.
    """
    try:
        import earthaccess
        import h5py
        import numpy as np
        import rasterio
        from affine import Affine
        from rasterio.warp import reproject, Resampling
    except ImportError as exc:
        raise AcquisitionError(
            "Metadata streaming requires: pip install earthaccess h5py numpy rasterio"
        ) from exc
    if not math.isfinite(height_m):
        raise AcquisitionError("--height-m must be finite")

    earthaccess.login()  # .netrc or interactive EDL authentication
    records = earthaccess.search_data(
        short_name=scene.short_name,
        granule_name=scene.granule_name,
        count=20,
    )
    if not records:
        raise AcquisitionError(f"Earthaccess could not locate {scene.granule_name}")
    expected = scene.granule_name.removesuffix('.h5')
    candidates = []
    for record in records:
        for url in record.data_links(access='external'):
            if url.split('?')[0].lower().endswith('.h5') and expected in url:
                candidates.append(url)
    if len(set(candidates)) != 1:
        raise AcquisitionError(
            f"Expected one original HDF5 URL for {expected}, got {len(set(candidates))}. "
            "Metadata was not guessed."
        )
    source_url = candidates[0]
    LOG.info("Reading small remote HDF5 metadata blocks for %s", expected)
    # Earthaccess/fsspec uses HTTP range reads rather than a full HDF5 download.
    remote = earthaccess.open(
        [source_url],
        provider="ASF",
        show_progress=False,
        open_kwargs={
            "block_size": 4 * 1024 * 1024,
            "cache_type": "blockcache",
        },
    )
    if len(remote) != 1:
        raise AcquisitionError(f"Expected one remotely opened HDF5, got {len(remote)}")
    base = '/science/LSAR/GUNW'
    center_paths = [
        f'{base}/grids/frequencyA/centerFrequency',
        f'{base}/metadata/sourceData/swaths/frequencyA/centerFrequency',
        f'{base}/metadata/sourceData/swaths/frequencyA/processedCenterFrequency',
        f'{base}/metadata/sourceData/swaths/frequencyA/acquiredCenterFrequency',
        f'{base}/metadata/processingInformation/parameters/reference/frequencyA/centerFrequency',
        f'{base}/metadata/processingInformation/parameters/common/frequencyA/centerFrequency',
    ]
    cube_base = f'{base}/metadata/radarGrid'
    with closing(remote[0]) as file_obj:
        with h5py.File(file_obj, 'r') as h5:
            frequency_hz, frequency_path = None, None
            for path in center_paths:
                if path in h5 and h5[path].shape == ():
                    value = float(h5[path][()])
                    if 1.1e9 < value < 1.4e9:
                        frequency_hz, frequency_path = value, path
                        break
            if frequency_hz is None:
                # GUNW metadata layouts differ between releases. Only inspect
                # frequency-A scalar datasets; never accidentally use B-band.
                matches = []
                def visit_frequency(name, obj):
                    if not isinstance(obj, h5py.Dataset) or obj.shape != ():
                        return
                    normalized = name.lower()
                    if ('frequencya' not in normalized or
                        not normalized.endswith(('centerfrequency', 'processedcenterfrequency',
                                                 'acquiredcenterfrequency'))):
                        return
                    try:
                        val = float(obj[()])
                        if 1.1e9 < val < 1.4e9:
                            matches.append((val, '/' + name))
                    except (ValueError, TypeError):
                        pass
                h5[f'{base}'].visititems(visit_frequency)
                distinct = {round(val, 1) for val, _ in matches}
                if len(distinct) == 1:
                    frequency_hz, frequency_path = matches[0]
                elif len(distinct) > 1:
                    raise AcquisitionError(f'Ambiguous frequency-A values: {matches}')
            if frequency_hz is None:
                raise AcquisitionError(
                    'Could not read frequency-A center frequency from HDF5. '
                    "Check this GUNW version's structure; do not use a generic 24-cm constant."
                )
            needed = ['incidenceAngle', 'heightAboveEllipsoid',
                      'xCoordinates', 'yCoordinates', 'projection']
            missing = [name for name in needed if f'{cube_base}/{name}' not in h5]
            if missing:
                raise AcquisitionError(f'Missing radarGrid metadata: {missing}')
            x = np.asarray(h5[f'{cube_base}/xCoordinates'][:], dtype=np.float64)
            y = np.asarray(h5[f'{cube_base}/yCoordinates'][:], dtype=np.float64)
            z = np.asarray(h5[f'{cube_base}/heightAboveEllipsoid'][:], dtype=np.float64)
            epsg = int(h5[f'{cube_base}/projection'][()])
            cube = h5[f'{cube_base}/incidenceAngle']
            if cube.shape != (len(z), len(y), len(x)) or len(z) < 1:
                raise AcquisitionError(f'Unexpected incidence cube shape: {cube.shape}')
            if not (np.all(np.diff(x) > 0) or np.all(np.diff(x) < 0)) or not (
                    np.all(np.diff(y) > 0) or np.all(np.diff(y) < 0)):
                raise AcquisitionError('Radar-grid coordinates are not monotonic')
            if not np.all(np.diff(z) > 0):
                raise AcquisitionError('Height grid is not increasing')
            if not (z[0] <= height_m <= z[-1]):
                raise AcquisitionError(
                    f'Requested ellipsoidal height {height_m} m outside '
                    f'metadata cube range [{z[0]}, {z[-1]}] m'
                )
            hi = min(int(np.searchsorted(z, height_m)), len(z) - 1)
            lo = max(0, hi - 1)
            if height_m == z[hi]:
                lo = hi
            lower = np.asarray(cube[lo, :, :], dtype=np.float32)
            if lo == hi:
                grid = lower
            else:
                upper = np.asarray(cube[hi, :, :], dtype=np.float32)
                fraction = float((height_m - z[lo]) / (z[hi] - z[lo]))
                grid = lower * (1 - fraction) + upper * fraction

    # Coordinate vectors represent pixel centers. Use full signed dx/dy to
    # preserve increasing/decreasing axes and let rasterio reproject correctly.
    if len(x) < 2 or len(y) < 2:
        raise AcquisitionError('Incidence radar grid needs at least 2 rows/columns')
    dx = float(x[1] - x[0])
    dy = float(y[1] - y[0])
    if not (np.allclose(np.diff(x), dx) and np.allclose(np.diff(y), dy)):
        raise AcquisitionError('Nonuniform radar-grid axes not supported')
    source_transform = Affine(dx, 0, x[0] - dx / 2,
                              0, dy, y[0] - dy / 2)
    with rasterio.open(phase_tif) as src:
        if not src.crs:
            raise AcquisitionError('Phase TIFF missing coordinate reference system')
        out_angle = np.full((src.height, src.width), np.nan, dtype=np.float32)
        reproject(
            source=grid, destination=out_angle,
            src_transform=source_transform, src_crs=f'EPSG:{epsg}',
            src_nodata=np.nan, dst_transform=src.transform, dst_crs=src.crs,
            dst_nodata=np.nan, resampling=Resampling.bilinear,
        )
        profile = src.profile.copy()
    profile.update(driver='GTiff', dtype='float32', count=1,
                   nodata=np.nan, compress='deflate')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(destination, 'w', **profile) as ds:
        ds.write(out_angle, 1)
        ds.set_band_description(1, 'Incidence angle at constant ellipsoid height, degrees')
        ds.update_tags(units='degrees', height_above_wgs84_ellipsoid_m=str(height_m),
                       height_assumption='constant; NOT terrain-following')
    if not np.isfinite(out_angle).any():
        destination.unlink(missing_ok=True)
        raise AcquisitionError('No overlap between radar metadata and requested phase TIFF')

    return {
        'center_frequency_hz': float(frequency_hz),
        'wavelength_m': 299792458.0 / float(frequency_hz),
        'frequency_source': frequency_path,
        'incidence_height_m': float(height_m),
        'incidence_approximation': 'constant_WGS84_ellipsoid_height_not_dem',
        'incidence_cube_path': f'{cube_base}/incidenceAngle',
        'remote_hdf5_url': source_url,
    }


def ensure_radar_metadata(
    record: dict[str, Any], scene: Scene, folder: Path, *,
    height_m: float, previous: dict[str, Any] | None = None,
) -> None:
    """Enrich a Harmony scene with frequency and a pixel-aligned incidence TIFF.

    Metadata failures never trigger a second Harmony download. The original
    COGs remain in place and can be reused when rerunning acquisition.
    """
    previous = previous or {}
    old = previous.get('radar_metadata', {})
    old_angle = previous.get('assets', {}).get('incidence_angle', {}).get('path')
    if (old.get('center_frequency_hz') and
        old.get('incidence_height_m') == float(height_m) and
        old_angle and Path(old_angle).is_file()):
        record['radar_metadata'] = old
        record['assets']['incidence_angle'] = previous['assets']['incidence_angle']
        record['metadata_status'] = 'complete_cached'
        return
    angle_file = folder / 'incidence_angle_deg.tif'
    md = fetch_radar_metadata(scene, record['assets']['phase']['path'],
                              angle_file, height_m=height_m)
    md_asset = raster_metadata({'incidence_angle': str(angle_file)})['incidence_angle']
    phase = record['assets']['phase']
    for attr in ('crs', 'width', 'height', 'transform'):
        if md_asset[attr] != phase[attr]:
            raise AcquisitionError(f'Incidence raster not aligned with phase: {attr}')
    record['radar_metadata'] = md
    record['assets']['incidence_angle'] = md_asset
    record['metadata_status'] = 'complete'



def acquire_one(
    scene: Scene,
    bbox: tuple[float, float, float, float],
    output_dir: Path,
    layers: Iterable[str],
    polarization: str,
    epsg: int | None,
    client: Any | None = None,
    retry_failed: bool = False,
    with_radar_metadata: bool = False,
    metadata_height_m: float = -400.0,
) -> dict[str, Any]:
    chosen = select_polarization(scene, polarization)
    if chosen is None:
        LOG.info("Skip scene: requested %s but scene has %s: %s", polarization,
                 scene.pol_code, scene.granule_name)
        return {**asdict(scene), "status": "skipped_polarization", "assets": {}}

    folder = scene_folder(output_dir, scene, bbox, layers, chosen, epsg)
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    previous = read_json(manifest_path)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "scene": asdict(scene),
        "requested": {"bbox_wsen": list(bbox), "layers": list(layers),
                      "polarization": chosen, "output_epsg": epsg,
                      "variables": variable_paths(layers, chosen)},
        "directory": str(folder.resolve()),
        "created_utc": previous.get("created_utc", now_utc()) if previous else now_utc(),
        "updated_utc": now_utc(),
        "status": "pending",
        "job_id": previous.get("job_id") if previous else None,
        "assets": {},
    }
    if previous and previous.get("status") in ("complete", "metadata_incomplete"):
        try:
            manifest["assets"] = inspect_outputs(folder, layers)
            manifest["status"] = "complete"
            manifest["cached"] = True
            manifest["job_id"] = previous.get("job_id")
            if with_radar_metadata:
                try:
                    ensure_radar_metadata(manifest, scene, folder,
                                          height_m=metadata_height_m, previous=previous)
                except Exception as exc:
                    manifest["status"] = "metadata_incomplete"
                    manifest["metadata_error"] = f"{type(exc).__name__}: {exc}"
                    LOG.error("Metadata extraction failed: %s", exc)
            manifest["updated_utc"] = now_utc()
            write_json(manifest_path, manifest)
            return manifest
        except AcquisitionError:
            LOG.warning("Invalid/incomplete cached scene, re-downloading: %s", scene.granule_id)

    if previous and previous.get("status") == "failed" and not retry_failed:
        manifest["status"] = "failed"
        manifest["error"] = "Previous attempt failed; pass --retry-failed to try again"
        return manifest

    # A failed Harmony job might be irrecoverable: submit a new one on retry.
    resume_job = manifest["job_id"] if previous and previous.get("status") in ("submitted", "downloading") else None
    def save_job(job_id: str) -> None:
        manifest["job_id"] = job_id
        manifest["status"] = "submitted"
        manifest["updated_utc"] = now_utc()
        write_json(manifest_path, manifest)

    try:
        if not resume_job:
            # Avoid accidentally validating old leftover rasters after resubmission.
            for old in folder.iterdir():
                if old.suffix.lower() in (".tif", ".tiff"):
                    old.unlink()
        manifest["status"] = "downloading" if resume_job else "pending"
        write_json(manifest_path, manifest)
        job_id = harmony_subset(scene, bbox, layers, chosen, epsg, folder,
                                resume_job, save_job, client=client)
        manifest["job_id"] = job_id
        manifest["assets"] = inspect_outputs(folder, layers)
        manifest["status"] = "complete"
        manifest["cached"] = False
        if with_radar_metadata:
            try:
                ensure_radar_metadata(manifest, scene, folder,
                                      height_m=metadata_height_m)
            except Exception as exc:
                manifest["status"] = "metadata_incomplete"
                manifest["metadata_error"] = f"{type(exc).__name__}: {exc}"
                LOG.error("Metadata extraction failed: %s", exc)
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        LOG.error("Acquisition failed for %s: %s", scene.granule_id, exc)
    finally:
        manifest["updated_utc"] = now_utc()
        write_json(manifest_path, manifest)
    return manifest


def acquire_gunw(
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    *,
    start_date: str = "2026-10-01",
    end_date: str | None = None,
    output_dir: str | Path = "data/raw",
    max_scenes: int = 5,
    release: str = "auto",
    polarization: str = "auto",
    layers: tuple[str, ...] = DEFAULT_LAYERS,
    epsg: int | None = None,
    search_only: bool = False,
    search_cap: int = 600,
    retry_failed: bool = False,
    with_radar_metadata: bool = False,
    metadata_height_m: float = -400.0,
    session: requests.Session | None = None,
    harmony_client: Any | None = None,
) -> list[dict[str, Any]]:
    """Acquire matching scenes; return scene records with status and asset metadata.

    max_scenes=0 means all discovered matches, subject to search_cap. Results
    are sorted newest *secondary* acquisition first; only same-pair duplicates
    across releases are removed. Each complete record has an `assets` mapping
    (phase/coherence/ionosphere/connected_components) of geo-validated COGs.
    """
    bbox = validate_bbox(bbox)
    layers = tuple(dict.fromkeys(layers))
    if not layers or set(layers) - set(LAYERS):
        raise ValueError(f"layers must be nonempty subset of {list(LAYERS)}")
    if with_radar_metadata and not math.isfinite(metadata_height_m):
        raise ValueError("metadata_height_m must be a finite WGS84 ellipsoid height")
    if max_scenes < 0:
        raise ValueError("max_scenes must be >= 0 (0 means all)")
    if polarization not in ("auto", "HH", "VV"):
        raise ValueError("polarization must be auto, HH, VV")
    if epsg is not None and epsg != 4326 and not (32601 <= epsg <= 32660 or 32701 <= epsg <= 32760):
        raise ValueError("Supported --epsg: 4326, UTM north 32601..32660, UTM south 32701..32760")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    owned_session = session is None
    session = session if session is not None else cmr_session()
    try:
        scenes = find_scenes(session, bbox, start_date, end_date, release, search_cap)
    finally:
        if owned_session:
            session.close()

    # A requested explicit polarization can be filtered up front (without
    # counting unprocessable scenes against --max-scenes).
    if polarization != "auto":
        scenes = [s for s in scenes if select_polarization(s, polarization) is not None]
    if max_scenes:
        scenes = scenes[:max_scenes]
    LOG.info("Selected %d distinct GUNW scene(s)", len(scenes))
    if search_only:
        return [{**asdict(s), "status": "discovered"} for s in scenes]

    records: list[dict[str, Any]] = []
    def save_index() -> None:
        # Preserve the query result even when CMR found no matching scenes.
        write_json(out / "index.json", {
            "schema_version": 1, "updated_utc": now_utc(),
            "request": {"bbox_wsen": list(bbox), "start_date": start_date,
                        "end_date": end_date, "release": release,
                        "polarization": polarization, "layers": list(layers),
                        "max_scenes": max_scenes},
            "scenes": records,
        })

    save_index()
    for i, scene in enumerate(scenes, 1):
        LOG.info("Processing scene %d/%d: %s", i, len(scenes), scene.granule_name)
        try:
            record = acquire_one(scene, bbox, out, layers, polarization, epsg,
                                 client=harmony_client, retry_failed=retry_failed,
                                 with_radar_metadata=with_radar_metadata,
                                 metadata_height_m=metadata_height_m)
        except AcquisitionError as exc:
            LOG.error("Acquisition setup failed for %s: %s", scene.granule_id, exc)
            record = {"scene": asdict(scene), "status": "failed", "error": str(exc), "assets": {}}
        records.append(record)
        save_index()
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch geo-subsetted NISAR GUNW scenes with NASA CMR and Harmony",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    region = parser.add_mutually_exclusive_group()
    region.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                        help="WGS84 bounding box: WEST SOUTH EAST NORTH")
    region.add_argument("--center", type=float, nargs=2, metavar=("LAT", "LON"),
                        help="Geographic center: LATITUDE LONGITUDE")
    parser.add_argument("--size-km", type=float, default=8, help="Square width with --center")
    parser.add_argument("--start", default="2026-10-01", help="Date of later acquisition (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD); default no end")
    parser.add_argument("--release", choices=["auto", *RELEASE_ORDER], default="auto",
                        help="Auto prefers validated over provisional over beta for duplicate pairs")
    parser.add_argument("--max-scenes", type=int, default=5,
                        help="Newest distinct scenes to fetch; 0=all discovered")
    parser.add_argument("--search-cap", type=int, default=600, help="Max CMR hits per collection")
    parser.add_argument("--polarization", choices=["auto", "HH", "VV"], default="auto")
    parser.add_argument("--layers", nargs="+", choices=list(LAYERS), default=list(DEFAULT_LAYERS))
    parser.add_argument("--epsg", type=int, help="Optional output CRS; default original projection")
    parser.add_argument("--out", type=Path, default=Path("data/raw"))
    parser.add_argument("--search-only", action="store_true", help="Only inspect results, no Harmony job")
    parser.add_argument("--retry-failed", action="store_true", help="Resubmit previously failed jobs")
    parser.add_argument("--with-radar-metadata", action="store_true",
                        help="Also stream exact frequency and derive a pixel-aligned incidence angle")
    parser.add_argument("--height-m", type=float, default=-400.0,
                        help="Assumed constant WGS84 ELLIPSOID height for incidence cube (not a DEM)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")
    try:
        bbox = (tuple(args.bbox) if args.bbox else
                bbox_from_center(args.center[0], args.center[1], args.size_km)
                if args.center else DEFAULT_BBOX)
        rows = acquire_gunw(
            bbox, start_date=args.start, end_date=args.end, output_dir=args.out,
            max_scenes=args.max_scenes, release=args.release,
            polarization=args.polarization, layers=tuple(args.layers), epsg=args.epsg,
            search_only=args.search_only, search_cap=args.search_cap,
            retry_failed=args.retry_failed,
            with_radar_metadata=args.with_radar_metadata,
            metadata_height_m=args.height_m,
        )
    except (AcquisitionError, ValueError, requests.RequestException) as exc:
        LOG.error("%s", exc)
        return 1
    if not rows:
        print("No matching GUNW scenes. Try a wider date range, larger AOI, or another release.")
        return 2
    for i, record in enumerate(rows, 1):
        print(f"{i:>2}. {record.get('status', '?'):<22} "
              f"{record.get('granule_name', record.get('scene', {}).get('granule_name', '?'))}")
        if record.get("assets"):
            for name, info in sorted(record["assets"].items()):
                print(f"       {name}: {info['path']}")
        if record.get("radar_metadata"):
            md = record["radar_metadata"]
            print(f"       frequency A: {md['center_frequency_hz']/1e6:g} MHz; "
                  f"incidence height: {md['incidence_height_m']:g} m ellipsoid")
        if record.get("metadata_error"):
            print(f"       METADATA ERROR: {record['metadata_error']}")
        if record.get("error"):
            print(f"       ERROR: {record['error']}")
    if args.search_only:
        return 0
    n_done = sum(row.get("status") == "complete" for row in rows)
    print(f"\nCompleted {n_done}/{len(rows)} scene(s); scene manifests and index.json in {args.out}")
    return 0 if n_done == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
