"""Read-only Zones API adapter and OFFLINE Stage 2/3 export builder.

Production HTTP requests ONLY read a prebuilt JSON file. No NumPy, SciPy,
GeoTIFF, or NISAR processing runs on Vercel requests.

Build from a matching pair of completed Stage 2 and Stage 3 runs:
  python src/zone_service.py --run run_YYYYMMDD_HHMMSS_ffffff \
      --data-root data --out data/api/zones.json

Deploy the generated zones.json with the API and optionally set:
  NISAR_ZONES_FILE=path/to/zones.json

The HTTP handler already calls get_zones(), which returns just list[Zone].
All detections are exploratory. We deliberately do NOT map the Stage 3
review priority to the API's risk levels unless the v2 detector explicitly assigns a
user-defined experimental screening category. Those categories are not
validated hazard levels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict


class Polygon(TypedDict):
    type: Literal["Polygon"]
    coordinates: list[list[list[float]]]


class MultiPolygon(TypedDict):
    type: Literal["MultiPolygon"]
    coordinates: list[list[list[list[float]]]]


class ZoneSummary(TypedDict):
    los_velocity_mm_per_year: float | None
    cumulative_shift_mm: float | None
    acceleration_mm_per_year2: float | None
    latest_pair_mean_coherence: float | None
    latest_pair_reference_time: str | None
    latest_pair_secondary_time: str | None


class CumulativePoint(TypedDict):
    time: str
    displacement_mm: float


class Zone(TypedDict):
    id: str
    geometry: Polygon | MultiPolygon
    risk_level: Literal["high", "elevated", "watch", "low"] | None
    summary: ZoneSummary
    cumulative_series: list[CumulativePoint] | None


class ZoneCalculationUnavailable(Exception):
    """No verified, precomputed zones payload is available to the HTTP API."""


YEAR_DAYS = 365.25


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _finite_number(value: object) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_zone(zone: object) -> None:
    """Minimal contract validation; ensures output is JSON-safe without OpenAPI deps."""
    if not isinstance(zone, dict) or not isinstance(zone.get("id"), str) or not zone["id"]:
        raise ValueError("Zone has no valid ID")
    _validate_geometry(zone.get("geometry"))
    if zone.get("risk_level") not in (None, "high", "elevated", "watch", "low"):
        raise ValueError("Invalid risk level")
    summary = zone.get("summary")
    required = (
        "los_velocity_mm_per_year", "cumulative_shift_mm", "acceleration_mm_per_year2",
        "latest_pair_mean_coherence", "latest_pair_reference_time", "latest_pair_secondary_time",
    )
    if not isinstance(summary, dict) or any(k not in summary for k in required):
        raise ValueError("Missing zone summary field")
    for k in required[:4]:
        if summary[k] is not None and not _finite_number(summary[k]):
            raise ValueError(f"Nonfinite or nonnumeric summary field: {k}")
    q = summary["latest_pair_mean_coherence"]
    if q is not None and not 0 <= q <= 1:
        raise ValueError("Coherence must be between 0 and 1")
    for k in required[4:]:
        if summary[k] is not None and not isinstance(summary[k], str):
            raise ValueError(f"Invalid timestamp: {k}")
    series = zone.get("cumulative_series")
    if series is not None:
        if not isinstance(series, list) or not series:
            raise ValueError("Cumulative series must be a nonempty list or null")
        for item in series:
            if (not isinstance(item, dict) or not isinstance(item.get("time"), str)
                    or not _finite_number(item.get("displacement_mm"))):
                raise ValueError("Invalid cumulative point")


def get_zones() -> list[Zone]:
    """Return precomputed Zone records (NOT the HTTP response envelope).

    The cache must be generated explicitly by the offline builder and bundled
    with the deployment. Missing data -> 503, malformed data -> 500 via route.
    """
    file_path = Path(os.environ.get("NISAR_ZONES_FILE", "data/api/zones.json"))
    if not file_path.is_absolute():
        file_path = _project_root() / file_path
    if not file_path.is_file():
        raise ZoneCalculationUnavailable(
            f"Precomputed zones not deployed: {file_path}. "
            "Run the offline zone_service builder and include the output file."
        )
    payload = _read_json(file_path)
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
        raise ValueError("Unsupported or malformed zones cache")
    zones = payload.get("zones")
    if not isinstance(zones, list):
        raise ValueError("Zones cache lacks a zones list")
    for item in zones:
        _validate_zone(item)
    # Avoid invalid JSON even if a future builder accidentally writes NaN.
    json.dumps(zones, allow_nan=False)
    return zones


def _validate_geometry(geom: object) -> None:
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("Expected WGS84 GeoJSON Polygon or MultiPolygon")
    polys = [geom.get("coordinates")] if geom["type"] == "Polygon" else geom.get("coordinates")
    if not isinstance(polys, list) or not polys:
        raise ValueError("Polygon coordinates missing")
    for rings in polys:
        if not isinstance(rings, list) or not rings:
            raise ValueError("Missing polygon ring")
        for ring in rings:
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError("Polygon rings must contain 4+ points and be closed")
            for pt in ring:
                if (not isinstance(pt, list) or len(pt) != 2 or
                        not all(_finite_number(n) for n in pt) or
                        not (-180 <= pt[0] <= 180 and -90 <= pt[1] <= 90)):
                    raise ValueError("Invalid GeoJSON longitude/latitude")


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Acquisition time is missing UTC offset: {value}")
    return dt.astimezone(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _observation_times(meta: dict) -> list[datetime] | None:
    """Map Stage 2 DATE slices to actual timestamps found in Stage 2 pair records.

    NISAR repeat acquisitions of a day can differ by a few seconds. Use an
    actually recorded timestamp, never midnight guessed from YYYY-MM-DD.
    An ambiguous date (> 120 s spread) makes the history unavailable.
    """
    days = meta["dates"]
    by_day: dict[str, list[datetime]] = {d: [] for d in days}
    for pair in meta.get("pairs", []):
        for key in ("reference_time_utc", "secondary_time_utc"):
            value = pair.get(key)
            if value is not None:
                dt = _parse_utc(value)
                if dt.date().isoformat() in by_day:
                    by_day[dt.date().isoformat()].append(dt)
    dates = []
    for day in days:
        occurrences = sorted(by_day[day])
        if not occurrences or (occurrences[-1] - occurrences[0]).total_seconds() > 120:
            return None
        dates.append(occurrences[0])
    if any(b <= a for a, b in zip(dates, dates[1:])):
        return None
    return dates


def _zone_id(geometry: dict, acquisition_family: dict, stable_id: str | None) -> str:
    if isinstance(stable_id, str) and stable_id.strip():
        return stable_id.strip()
    # Independent of run name, stable when geometry and orbit family unchanged.
    # This is NOT a durable identity if polygon boundaries migrate in later runs.
    def round_coordinates(obj):
        if isinstance(obj, list):
            return [round_coordinates(x) for x in obj]
        if isinstance(obj, (float, int)):
            return round(float(obj), 5)
        return obj
    key = {
        "family": {k: acquisition_family.get(k) for k in ("track", "frame", "direction", "polarization")},
        "geom": {"type": geometry["type"], "coordinates": round_coordinates(geometry["coordinates"])},
    }
    signature = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return "zone-" + hashlib.sha256(signature.encode()).hexdigest()[:16]


def _precise_zone_id(geometry: dict, acquisition_family: dict) -> str:
    """Geometry-derived fallback when distinct polygons collide at 5 decimals.

    Preserve the original five-decimal ID for normal cases for backwards
    compatibility, but hash exact serialized coordinates for colliding shapes.
    The fallback is still independent of the processing run and detector ID.
    """
    key = {
        "family": {k: acquisition_family.get(k) for k in ("track", "frame", "direction", "polarization")},
        "geometry": geometry,
    }
    signature = json.dumps(key, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "zone-" + hashlib.sha256(signature.encode()).hexdigest()[:32]


def _raw_coherence_rasters(raw_root: Path, granule_id: str) -> list[Path]:
    """Resolve original Harmony pair coherence; do NOT use Stage 2 date proxy."""
    candidates: list[Path] = []
    if not raw_root.is_dir() or not granule_id:
        return candidates
    for manifest_file in raw_root.glob("*/manifest.json"):
        meta = _read_json(manifest_file)
        if meta.get("scene", {}).get("granule_id") != granule_id:
            continue
        record = meta.get("assets", {}).get("coherence", {})
        name = record.get("path") if isinstance(record, dict) else None
        if not name:
            continue
        value = Path(name)
        for p in (value, manifest_file.parent / value.name, raw_root.parent.parent / value):
            if p.is_file() and p not in candidates:
                candidates.append(p.resolve())
                break
    return candidates


def _latest_pair_quality(meta: dict, raw_root: Path, shape: tuple[int,int]):
    """Return (true_pair_coherence_grid_or_None, latest_pair_record_or_None)."""
    import numpy as np
    import rasterio
    from affine import Affine
    from rasterio.warp import reproject, Resampling

    pairs = meta.get("pairs") or []
    latest = max(pairs, key=lambda p: _parse_utc(p["secondary_time_utc"])) if pairs else None
    if latest is None:
        return None, None
    paths = _raw_coherence_rasters(raw_root, latest.get("granule_id", ""))
    if not paths:
        return None, latest
    dst = np.full(shape, np.nan, dtype=np.float32)
    geo = meta["grid"]
    for path in paths:
        with rasterio.open(path) as src:
            if src.crs is None or src.count != 1:
                raise ValueError(f"Ungrounded coherence raster: {path}")
            source = src.read(1, masked=True).astype(np.float32).filled(np.nan)
            projected = np.full(shape, np.nan, dtype=np.float32)
            reproject(
                source, projected,
                src_transform=src.transform, src_crs=src.crs, src_nodata=np.nan,
                dst_transform=Affine(*geo["transform_affine"]), dst_crs=geo["crs"],
                dst_nodata=np.nan, resampling=Resampling.nearest,
            )
            incoming = np.isfinite(projected) & (projected >= 0) & (projected <= 1)
            dst[incoming & ~np.isfinite(dst)] = projected[incoming & ~np.isfinite(dst)]
    return dst if np.isfinite(dst).any() else None, latest


def build_zones(*, run_name: str, data_root: str | Path = "data", min_common_pixels: int = 1) -> list[Zone]:
    """Offline adapter: export latest-date Stage 3 polygons and Stage 2 data.

    Pixel time histories use the intersection of finite pixels across ALL
    acquisition dates within the LAST polygon geometry. This fixed footprint
    prevents artificial time trends from changing observed pixel populations.
    If no common footprint exists, history/LOS metrics are null, never invented.
    """
    import numpy as np
    from affine import Affine
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    if (not run_name or Path(run_name).name != run_name or run_name in (".", "..")
            or "/" in run_name or "\\" in run_name):
        raise ValueError("run_name must be a single safe folder name")
    if min_common_pixels < 1:
        raise ValueError("min_common_pixels must be >= 1")
    root = Path(data_root)
    processed = root / "processed" / run_name
    detected = root / "detection" / run_name
    for required in (
        processed / "metadata.json", processed / "displacement.npy",
        processed / "coherence.npy", detected / "metadata.json",
        detected / "anomalies.geojson",
    ):
        if not required.is_file():
            raise FileNotFoundError(f"Missing required completed-pipeline output: {required}")

    meta = _read_json(processed / "metadata.json")
    det_meta = _read_json(detected / "metadata.json")
    geojson = _read_json(detected / "anomalies.geojson")
    if meta.get("stage") != "geophysical_processing_cumulative_stack":
        raise ValueError("Processing input is not a cumulative displacement stack")
    if det_meta.get("stage") != "subsidence_detection":
        raise ValueError("Detection metadata has an unexpected stage")
    if Path(det_meta.get("source_processing_directory", "")).name != run_name:
        raise ValueError("Stage 3 metadata references a different processing run")
    if geojson.get("type") != "FeatureCollection" or not isinstance(geojson.get("features"), list):
        raise ValueError("Stage 3 polygons must be a GeoJSON FeatureCollection")
    if geojson.get("meta", {}).get("source_run") != run_name:
        raise ValueError("Stage 3 GeoJSON source run does not match selection")

    displacement = np.load(processed / "displacement.npy", mmap_mode="r", allow_pickle=False)
    quality_proxy = np.load(processed / "coherence.npy", mmap_mode="r", allow_pickle=False)
    if (displacement.ndim != 3 or quality_proxy.shape != displacement.shape
            or tuple(meta.get("array_shape", [])) != displacement.shape):
        raise ValueError("Stage 2 arrays are not aligned or have unexpected shape")
    dates = meta.get("dates", [])
    if not isinstance(dates, list) or not dates or len(dates) != displacement.shape[0]:
        raise ValueError("Stage 2 date axis does not match data")
    try:
        parsed_dates = [datetime.strptime(day, "%Y-%m-%d").date() for day in dates]
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Stage 2 acquisition dates") from exc
    if any(day.isoformat() != original for day, original in zip(parsed_dates, dates)) or any(
        later <= earlier for earlier, later in zip(parsed_dates, parsed_dates[1:])
    ):
        raise ValueError("Stage 2 acquisition dates must be ordered ISO dates")
    grid = meta["grid"]
    if (not grid.get("crs") or len(grid.get("transform_affine", [])) != 6
            or grid.get("width") != displacement.shape[2]
            or grid.get("height") != displacement.shape[1]):
        raise ValueError("Stage 2 map grid metadata inconsistent with arrays")

    # LOS values only if the input declares the contract's orientation and
    # an explicit land reference. The provisional phase sign still needs
    # independent scientific validation: risk fields are EXPERIMENTAL bins.
    disp_info = meta.get("displacement", {})
    compatible_los = (
        disp_info.get("component") == "los"
        and disp_info.get("positive_los_direction") == "toward"
        and disp_info.get("phase_sign") == -1
        and disp_info.get("reference_bbox_wsen") is not None
    )
    actual_times = _observation_times(meta)
    latest_pair_grid, latest_pair = _latest_pair_quality(
        meta, root / "raw", displacement.shape[1:]
    )
    latest_ref = _rfc3339(_parse_utc(latest_pair["reference_time_utc"])) if latest_pair else None
    latest_sec = _rfc3339(_parse_utc(latest_pair["secondary_time_utc"])) if latest_pair else None

    transform = Affine(*grid["transform_affine"])
    zones: list[Zone] = []
    used_geometry_by_id: dict[str, str] = {}
    duplicate_features = 0
    # When duplicate geographic footprints exist, retain the most recently
    # observed track so the exported screening category is not stale.
    features = sorted(geojson["features"],
                      key=lambda f: str((f.get("properties") or {}).get("last_observed", "")),
                      reverse=True)
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("Invalid GeoJSON feature")
        geometry = feature.get("geometry")
        _validate_geometry(geometry)
        props = feature.get("properties") or {}
        if props.get("source_run", run_name) != run_name:
            raise ValueError("Feature belongs to a different processing run")
        if props.get("last_observed") not in dates:
            raise ValueError("Feature has a missing or invalid last_observed date")
        if props["last_observed"] != dates[-1]:
            continue
        stable_id = props.get("stable_id")
        family = meta.get("acquisition_family", {})
        identifier = _zone_id(geometry, family, stable_id)
        # Stage 3 can emit the same footprint for separate temporal anomaly
        # tracks. The API models geographic zones, not individual tracks, so
        # identical polygons share one region history and should appear once.
        # A different shape may collide because _zone_id rounds to 5 decimals:
        # preserve it with a precise, still run-independent geometry hash.
        geometry_key = json.dumps(geometry, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if identifier in used_geometry_by_id:
            if used_geometry_by_id[identifier] == geometry_key:
                duplicate_features += 1
                continue
            if stable_id:
                raise ValueError(
                    f"Conflicting explicit stable_id {identifier!r} for different polygons"
                )
            identifier = _precise_zone_id(geometry, family)
            if identifier in used_geometry_by_id:
                if used_geometry_by_id[identifier] == geometry_key:
                    duplicate_features += 1
                    continue
                raise ValueError(f"Unexpected precise geometry ID collision: {identifier}")
            logging.warning(
                "Different polygons had the same rounded zone ID; used precise ID %s",
                identifier,
            )
        used_geometry_by_id[identifier] = geometry_key
        projected = transform_geom("EPSG:4326", grid["crs"], geometry)
        footprint = geometry_mask(
            [projected], out_shape=displacement.shape[1:], transform=transform, invert=True
        )
        pair_q = None
        if latest_pair_grid is not None and footprint.any():
            values = latest_pair_grid[footprint]
            values = values[np.isfinite(values)]
            if values.size:
                pair_q = float(np.mean(values))
        series = None
        speed = shift = acceleration = None
        if compatible_los and actual_times is not None and footprint.any():
            # A fixed polygon footprint AND fixed pixels for every date.
            sub = np.asarray(displacement[:, footprint], dtype=np.float64)
            fixed = np.all(np.isfinite(sub), axis=0)
            if np.count_nonzero(fixed) >= min_common_pixels:
                means = np.mean(sub[:, fixed], axis=1)
                means -= means[0]  # Numerical anchor, no fabricated observations.
                series = [
                    {"time": _rfc3339(t), "displacement_mm": float(value)}
                    for t, value in zip(actual_times, means)
                ]
                shift = float(means[-1])
                if len(means) >= 2:
                    dt_days = (actual_times[-1] - actual_times[-2]).total_seconds() / 86400
                    if dt_days > 0:
                        speed = float((means[-1] - means[-2]) / dt_days * YEAR_DAYS)
                if len(means) >= 3:
                    delta_days = np.array([
                        (b - a).total_seconds() / 86400
                        for a, b in zip(actual_times, actual_times[1:])
                    ])
                    if np.all(delta_days > 0):
                        v = np.diff(means) / delta_days  # mm/day
                        mid = np.cumsum(np.r_[0, delta_days[:-1]]) + delta_days / 2
                        dt_mid = mid[-1] - mid[-2]
                        if dt_mid > 0:
                            acceleration = float((v[-1] - v[-2]) / dt_mid * YEAR_DAYS**2)
        # Only the v2 detector's explicit rate screening labels can populate
        # the dashboard. Never promote old 'priority' review categories to risk.
        screening_level = props.get("risk_level")
        if props.get("risk_method") != "user_defined_los_rate_bins_v1":
            screening_level = None
        if screening_level not in (None, "high", "elevated", "watch", "low"):
            raise ValueError(f"Invalid Stage 3 risk screening level: {screening_level}")
        if not compatible_los:
            screening_level = None
        zone: Zone = {
            "id": identifier,
            "geometry": geometry,
            "risk_level": screening_level,
            "summary": {
                "los_velocity_mm_per_year": speed,
                "cumulative_shift_mm": shift,
                "acceleration_mm_per_year2": acceleration,
                "latest_pair_mean_coherence": pair_q,
                "latest_pair_reference_time": latest_ref,
                "latest_pair_secondary_time": latest_sec,
            },
            "cumulative_series": series,
        }
        _validate_zone(zone)
        zones.append(zone)
    if duplicate_features:
        logging.warning(
            "Collapsed %d repeated Stage 3 polygon record(s) into %d distinct API zone(s)",
            duplicate_features, len(zones),
        )
    return zones


def export_cache(*, run_name: str, data_root: str | Path = "data", output: str | Path = "data/api/zones.json",
                 min_common_pixels: int = 1) -> Path:
    zones = build_zones(run_name=run_name, data_root=data_root, min_common_pixels=min_common_pixels)
    payload = {
        "schema_version": "1.0",
        "source_run": run_name,
        "generated_at": _rfc3339(datetime.now(timezone.utc)),
        "scientific_status": "exploratory_unvalidated",
        "zones": zones,
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def main() -> int:
    p = argparse.ArgumentParser(description="Build static Zones API JSON from Stage 2 and Stage 3")
    p.add_argument("--run", required=True, help="Exact shared Stage 2 and Stage 3 run folder name")
    p.add_argument("--data-root", default="data", help="Root containing raw/, processed/, detection/")
    p.add_argument("--out", default="data/api/zones.json", help="JSON payload to include in deployment")
    p.add_argument("--min-common-pixels", type=int, default=1)
    args = p.parse_args()
    path = export_cache(run_name=args.run, data_root=args.data_root,
                        output=args.out, min_common_pixels=args.min_common_pixels)
    print(f"Prepared {path} for API deployment ({len(get_zones_from_path(path))} zones)")
    return 0


def get_zones_from_path(path: str | Path) -> list[Zone]:
    payload = _read_json(Path(path))
    zones = payload["zones"]
    for zone in zones:
        _validate_zone(zone)
    return zones


if __name__ == "__main__":
    raise SystemExit(main())
