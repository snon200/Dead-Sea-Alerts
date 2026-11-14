#!/usr/bin/env python3
"""Stage 2 — cumulative NISAR GUNW displacement stack via network inversion.

Output (only three files, in a unique run_... directory):
    data/processed/run_.../displacement.npy   float32 (T, Y, X), NaN means unusable
    data/processed/run_.../coherence.npy      float32 (T, Y, X), NaN means no observation
    data/processed/run_.../metadata.json     map grid, dates, reference and provenance

Each time slice is cumulative displacement relative to the first acquisition
date. Overlapping interferogram pairs are combined via per-pixel weighted least
squares, never added twice. The coherence stack is per-date quality summaries.

Examples (from project root, PowerShell):
    python src/nisar_processing.py --input data/raw/index.json \
      --ref-bbox 35.3770 31.4490 35.3880 31.4555

    python src/nisar_processing.py --input data/raw/index.json \
      --ref-bbox 35.3770 31.4490 35.3880 31.4555 --component vertical

    # Frequency A is detected automatically from each granule mode.
    # For exploratory data lacking troposphere metadata only:
    python src/nisar_processing.py --input data/raw/index.json \
      --ref-bbox 35.3770 31.4490 35.3880 31.4555 --no-troposphere

Study areas (W S E N, EPSG:4326):
    # Ein Gedi:        35.35 31.43 35.45 31.50
    # West Dead Sea:   35.30 30.95 35.54 31.80

Dependencies: pip install numpy rasterio

Caveats:
- Positive LOS is provisionally defined as motion TOWARD the satellite
  (phase_sign=-1); verify convention for the actual product before alerts.
- --component vertical assumes negligible horizontal movement. It is an
  ESTIMATE, not a measured decomposition of the displacement vector.
- If acquired at constant ellipsoidal height, tropospheric correction remains
  approximate in the mountainous Dead Sea terrain.
- Reference box must be a stable, coherent LAND region; the sample kibbutz box
  has NOT been independently validated as stable.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds, transform_geom

LOG = logging.getLogger('nisar.stage2')
C = 299_792_458.0
SCHEMA_VERSION = 1

# NISAR L-band frequency-A center frequency by first two digits of
# the four-digit range-bandwidth mode in the granule name.
# Source: ASF NISAR Data User Guide, "About NISAR", Frequencies table.
# The filename mode 2000 is 20 MHz frequency A -> 1229.0 MHz.
FREQUENCY_A_BY_BANDWIDTH_MHZ = {
    "05": 1221.5,
    "20": 1229.0,
    "40": 1239.0,
    "77": 1257.5,
}


def frequency_a_from_mode(scene: dict) -> tuple[float, str]:
    """Infer frequency-A from a NISAR 4-digit mode; never use frequency B.

    The first two digits are frequency-A bandwidth in MHz and the last two
    describe frequency-B bandwidth. E.g. 2000, 2005, 2020 -> 1229 MHz A.
    """
    identity = scene.get("scene", {})
    name = Path(str(identity.get("granule_name") or "")).name
    parts = name.removesuffix(".h5").split("_")
    filename_mode = parts[9] if len(parts) >= 20 and parts[0] == "NISAR" else None
    manifest_mode = identity.get("mode")
    if filename_mode and manifest_mode and str(filename_mode) != str(manifest_mode):
        raise ValueError(f"Mode mismatch between manifest {manifest_mode!r} and granule {filename_mode!r}")
    mode = str(manifest_mode or filename_mode or "")
    if not re.fullmatch(r"[0-9]{4}", mode):
        raise ValueError(f"Cannot determine NISAR frequency-A mode from {mode!r}; refusing to guess")
    bandwidth_a = mode[:2]
    if bandwidth_a == "00":
        raise ValueError(f"NISAR mode {mode} has no frequency-A band")
    mhz = FREQUENCY_A_BY_BANDWIDTH_MHZ.get(bandwidth_a)
    if mhz is None:
        raise ValueError(f"Unsupported NISAR frequency-A bandwidth in mode {mode}; update verified mapping")
    return mhz * 1e6, mode


def resolve_frequency_a(scene: dict, manual_frequency_mhz: float | None) -> tuple[float, str, str]:
    """Return (frequency_hz, source, mode); cross-check all available values."""
    expected_hz, mode = frequency_a_from_mode(scene)
    md = scene.get("radar_metadata") or {}
    metadata_value = md.get("center_frequency_hz")
    if metadata_value is not None:
        metadata_hz = float(metadata_value)
        if not np.isfinite(metadata_hz) or abs(metadata_hz - expected_hz) > 5e5:
            raise ValueError(f"Frequency metadata {metadata_hz} Hz conflicts with mode {mode} ({expected_hz} Hz)")
    else:
        metadata_hz = None

    if manual_frequency_mhz is not None:
        frequency_hz = float(manual_frequency_mhz) * 1e6
        source = "manual_override"
        if not np.isfinite(frequency_hz) or abs(frequency_hz - expected_hz) > 5e5:
            raise ValueError(f"Manual frequency {frequency_hz} Hz conflicts with mode {mode} ({expected_hz} Hz)")
    elif metadata_hz is not None:
        frequency_hz, source = metadata_hz, "scene_metadata"
    else:
        frequency_hz, source = expected_hz, "granule_mode"
    return frequency_hz, source, mode


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def file_path(raw: str | Path, source_manifest: Path) -> Path:
    p = Path(raw)
    possibilities = [p] if p.is_absolute() else [Path.cwd() / p, source_manifest.parent / p]
    for candidate in possibilities:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f'Cannot find asset {p} referenced by {source_manifest}')


def asset_path(scene: dict, manifest: Path, kind: str) -> Path:
    record = scene.get('assets', {}).get(kind)
    if not isinstance(record, dict) or not record.get('path'):
        raise ValueError(f'{manifest.parent.name}: missing required asset {kind!r}')
    return file_path(record['path'], manifest)


def input_scenes(index_path: Path) -> tuple[dict, list[tuple[Path, dict]]]:
    """Only read the ordered, explicitly selected scenes from Stage 1 index.json."""
    index = load_json(index_path)
    if not isinstance(index.get('scenes'), list):
        raise ValueError('Expected Stage 1 index.json with a scenes list')
    sources = []
    for item in index['scenes']:
        status = item.get('status')
        if status not in ('complete', 'metadata_incomplete'):
            raise ValueError(f'Acquisition contains {status!r} scene; finish Stage 1 first')
        name = item.get('manifest_path')
        if not name:
            if not item.get('directory'):
                raise ValueError('Index scene lacks manifest_path and directory')
            name = str(Path(item['directory']) / 'manifest.json')
        path = file_path(name, index_path)
        manifest = load_json(path)
        if manifest.get('status') not in ('complete', 'metadata_incomplete'):
            raise ValueError(f'Scene manifest is not complete: {path}')
        sources.append((path, manifest))
    if not sources:
        raise ValueError('Stage 1 index has no selected acquisitions')
    # Sort the *pairs* in time, but retain both reference and secondary dates.
    for path, scene in sources:
        identity = scene.get('scene', {})
        if not identity.get('reference_time') or not identity.get('secondary_time'):
            raise ValueError(f'Missing acquisition times in {path}')
    sources.sort(key=lambda pair: (pair[1]['scene']['secondary_time'], pair[1]['scene']['reference_time']))
    ids = [v.get('scene', {}).get('granule_id') for _, v in sources]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate GUNW scenes in acquisition index')
    return index, sources


def family_key(item: dict) -> tuple:
    scene = item.get('scene', {})
    granule_name = scene.get('granule_name', '')
    fields = Path(granule_name).name.removesuffix('.h5').split('_')
    crid = fields[15] if len(fields) >= 20 else None
    return (
        scene.get('track'), scene.get('frame'), scene.get('direction'),
        scene.get('mode'), item.get('requested', {}).get('polarization'),
        scene.get('release'), crid,
    )


def assert_consistent_family(sources: list[tuple[Path, dict]]):
    families = {family_key(scene) for _, scene in sources}
    if len(families) != 1:
        raise ValueError(f'Mixed track/frame/polarization/CRID detected: {families}. Run a single --timeseries acquisition family.')
    fam = next(iter(families))
    if any(v is None for v in fam):
        raise ValueError(f'Incomplete scene family identifiers: {fam}')

    # Prevent a disconnected set being mistaken for a temporal sequence.
    edges = [(s['scene']['reference_time'][:10], s['scene']['secondary_time'][:10]) for _, s in sources]
    adjacency: dict[str, set[str]] = {}
    for a, b in edges:
        if a >= b:
            raise ValueError(f'Invalid acquisition date ordering: {a} to {b}')
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    visited = set()
    stack = [edges[0][0]]
    while stack:
        current = stack.pop()
        if current not in visited:
            visited.add(current)
            stack.extend(adjacency[current] - visited)
    if visited != set(adjacency):
        raise ValueError('Acquisition pairs are not a connected time series')
    return fam


def validate_bbox(bbox) -> tuple[float, float, float, float]:
    if bbox is None or len(bbox) != 4:
        raise ValueError('Expected four WGS84 bounding-box coordinates: W S E N')
    w, s, e, n = map(float, bbox)
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError(f'Invalid bounding box: {bbox}')
    return w, s, e, n


def create_grid(bbox: tuple, epsg: int, resolution_m: float) -> dict:
    crs = CRS.from_epsg(epsg)
    if not crs.is_projected or not crs.linear_units.lower().startswith('metre'):
        raise ValueError('Grid CRS must be projected in meters, e.g. EPSG:32636')
    if not np.isfinite(resolution_m) or resolution_m <= 0:
        raise ValueError('Resolution must be positive')
    left, bottom, right, top = transform_bounds('EPSG:4326', crs, *bbox, densify_pts=21)
    # Grid origin snapped to the resolution for reproducible pixel registration.
    left = math.floor(left / resolution_m) * resolution_m
    top = math.ceil(top / resolution_m) * resolution_m
    width = math.ceil((right - left) / resolution_m)
    height = math.ceil((top - bottom) / resolution_m)
    if width <= 0 or height <= 0:
        raise ValueError('Empty target grid')
    if width * height > 15_000_000:
        raise ValueError(f'Target grid {width}x{height} is too large. Narrow --bbox or increase --resolution-m.')
    transform = from_origin(left, top, resolution_m, resolution_m)
    return dict(crs=crs, transform=transform, height=height, width=width,
                bbox_projected=[left, top - height * resolution_m,
                                left + width * resolution_m, top])


def read_native(path: Path, reference: dict | None = None, integer: bool = False):
    with rasterio.open(path) as ds:
        if ds.count != 1 or ds.crs is None:
            raise ValueError(f'Expected single-band georeferenced GeoTIFF: {path}')
        grid = (ds.crs, ds.transform, ds.width, ds.height)
        if reference is not None and grid != reference['grid']:
            raise ValueError(f'Raster not aligned with phase: {path}')
        band = ds.read(1, masked=True)
        if integer:
            array = np.asarray(band.filled(0), dtype=np.int32)
        else:
            array = np.asarray(band.filled(np.nan), dtype=np.float64)
        info = {'grid': grid, 'transform': ds.transform, 'crs': ds.crs,
                'width': ds.width, 'height': ds.height}
    return array, info


def reference_pixels(info: dict, bbox: tuple, valid: np.ndarray) -> np.ndarray:
    w, s, e, n = bbox
    geojson = {'type': 'Polygon', 'coordinates': [[(w,s),(e,s),(e,n),(w,n),(w,s)]]}
    projected = transform_geom('EPSG:4326', info['crs'], geojson)
    selection = geometry_mask([projected], out_shape=valid.shape,
                              transform=info['transform'], invert=True)
    return valid & selection


def reference_phase(corrected: np.ndarray, components: np.ndarray, good: np.ndarray,
                    info: dict, bbox: tuple, minimum: int) -> tuple[int, float, int]:
    pixels = reference_pixels(info, bbox, good)
    labels, counts = np.unique(components[pixels], return_counts=True)
    if not counts.size:
        raise ValueError('No valid pixels within reference bounding box')
    major = int(labels[np.argmax(counts)])
    chosen = pixels & (components == major)
    n = int(chosen.sum())
    if n < minimum:
        raise ValueError(f'Reference area has only {n} valid pixels in its largest component; need {minimum}')
    return major, float(np.median(corrected[chosen])), n


def warp_float(src: np.ndarray, src_info: dict, target: dict, *, method=Resampling.nearest) -> np.ndarray:
    dest = np.full((target['height'], target['width']), np.nan, dtype=np.float32)
    # NaN outside source coverage; masked pixels do NOT interpolate across phase discontinuities.
    reproject(source=src.astype(np.float32, copy=False), destination=dest,
              src_transform=src_info['transform'], src_crs=src_info['crs'],
              src_nodata=np.nan, dst_transform=target['transform'], dst_crs=target['crs'],
              dst_nodata=np.nan, resampling=method, init_dest_nodata=True)
    return dest


def process_pair(path: Path, scene: dict, args, grid: dict) -> tuple[np.ndarray, np.ndarray, dict]:
    assets = ['phase', 'coherence', 'connected_components']
    if not args.no_ionosphere:
        assets += ['ionosphere']
    if not args.no_troposphere:
        assets += ['tropo_wet', 'tropo_dry']
    if args.component == 'vertical' and args.incidence_deg is None:
        assets += ['incidence_angle']
    paths = {key: asset_path(scene, path, key) for key in assets}
    phase, info = read_native(paths['phase'])
    coherence, _ = read_native(paths['coherence'], reference=info)
    components, _ = read_native(paths['connected_components'], reference=info, integer=True)

    md = scene.get('radar_metadata') or {}
    frequency, frequency_source, frequency_mode = resolve_frequency_a(
        scene, args.frequency_mhz
    )
    wavelength = C / frequency
    LOG.info('Frequency A: %.1f MHz (mode %s, source: %s)',
             frequency / 1e6, frequency_mode, frequency_source)

    corrected = phase.copy()
    applied = []
    for key, label in [('ionosphere', 'ionospherePhaseScreen'),
                       ('tropo_wet', 'wetTroposphericPhaseScreen'),
                       ('tropo_dry', 'hydrostaticTroposphericPhaseScreen')]:
        if key in paths:
            screen, _ = read_native(paths[key], reference=info)
            corrected -= screen  # NASA correction phase screens are SUBTRACTED.
            applied.append(label)

    valid = (np.isfinite(corrected) & np.isfinite(coherence) & (coherence >= args.coh_min)
             & (coherence <= 1) & (components > 0) & (components < 65535))
    main_component, zero, ref_count = reference_phase(
        corrected, components, valid, info, tuple(args.ref_bbox), args.min_ref_pixels)
    valid &= (components == main_component)
    los = (args.phase_sign * (wavelength * 1000 / (4 * math.pi)) * (corrected - zero))
    los[~valid] = np.nan

    incidence_source = None
    if args.component == 'vertical':
        if args.incidence_deg is not None:
            theta = float(args.incidence_deg)
            if not 0 <= theta < 85:
                raise ValueError('Incidence angle must be between 0 and 85 degrees')
            los /= math.cos(math.radians(theta))
            incidence_source = f'constant {theta} degrees (manual assumption)'
        else:
            incidence, _ = read_native(paths['incidence_angle'], reference=info)
            acceptable = np.isfinite(incidence) & (incidence >= 0) & (incidence < 85)
            los = np.where(acceptable, los / np.cos(np.deg2rad(incidence)), np.nan)
            incidence_source = str(paths['incidence_angle'])

    # Keep coherence on land even for pixels below threshold; Stage 3 can inspect
    # the original evidence. Displacement is NaN wherever the quality mask fails.
    coherence[~np.isfinite(coherence) | (coherence < 0) | (coherence > 1)] = np.nan
    displaced = warp_float(los, info, grid)
    cohered = warp_float(coherence, info, grid)
    if not np.isfinite(displaced).any():
        raise ValueError('No valid displacement overlaps target grid. Check bbox/reference/coherence.')

    timing = scene['scene']
    start = datetime.fromisoformat(timing['reference_time'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(timing['secondary_time'].replace('Z', '+00:00'))
    delta_days = (end - start).total_seconds() / 86400
    if delta_days <= 0:
        raise ValueError(f'Invalid acquisition pair baseline: {start} to {end}')
    record = {
        'granule_id': timing.get('granule_id'),
        'granule_name': timing.get('granule_name'),
        'reference_time_utc': start.isoformat(),
        'secondary_time_utc': end.isoformat(),
        'baseline_days': delta_days,
        'center_frequency_hz': frequency,
        'frequency_source': frequency_source,
        'frequency_mode': frequency_mode,
        'wavelength_m': wavelength,
        'reference_phase_rad': zero,
        'reference_component': main_component,
        'reference_valid_pixels': ref_count,
        'valid_displacement_pixels': int(np.isfinite(displaced).sum()),
        'valid_coherence_pixels': int(np.isfinite(cohered).sum()),
        'corrections_applied': applied,
        'incidence_source': incidence_source,
        'troposphere_height_assumption': md.get('troposphere_assumption') if not args.no_troposphere else None,
    }
    return displaced, cohered, record


def invert_pair_network(pair_d: np.ndarray, pair_c: np.ndarray, records: list[dict],
                        *, coherence_min: float, block_pixels: int = 4096):
    """Weighted pixel-wise network inversion, anchored to first acquisition date.

    Each interferogram p measures D(t_secondary)-D(t_reference). Distinct dates
    are joined by calendar day because individual NISAR passes can differ by
    seconds across independently processed pairs. No double-counting of pairs.
    Coherence for an output DATE is mean coherence among incident usable edges;
    it is a support proxy, not a physical single-image coherence measurement.
    """
    dates = sorted({r['reference_time_utc'][:10] for r in records} |
                   {r['secondary_time_utc'][:10] for r in records})
    n = len(dates)
    if n < 2 or n > 48:
        raise ValueError('Network inversion supports 2 to 48 acquisition dates; split large stacks')
    lookup = {day: k for k, day in enumerate(dates)}
    edges = [(lookup[r['reference_time_utc'][:10]], lookup[r['secondary_time_utc'][:10]])
             for r in records]
    # Require the acquisition network itself to be connected.
    nodes = {0}
    for _ in range(n):
        for a, b in edges:
            if a in nodes or b in nodes:
                nodes.update((a, b))
    if len(nodes) != n:
        raise ValueError('Acquisition-date network is disconnected')

    pcount, ny, nx = pair_d.shape
    if len(edges) != pcount or pair_c.shape != pair_d.shape:
        raise ValueError('Edge count / pair array mismatch')
    A = np.zeros((pcount, n - 1), dtype=np.float64)
    for p, (a, b) in enumerate(edges):
        if a > 0:
            A[p, a - 1] = -1.
        if b > 0:
            A[p, b - 1] = 1.
    d_out = np.full((n, ny, nx), np.nan, dtype=np.float32)
    c_out = np.full((n, ny, nx), np.nan, dtype=np.float32)
    flat_d = pair_d.reshape(pcount, -1)
    flat_c = pair_c.reshape(pcount, -1)
    flat_o = d_out.reshape(n, -1)
    flat_q = c_out.reshape(n, -1)
    total = ny * nx

    for begin in range(0, total, block_pixels):
        stop = min(begin + block_pixels, total)
        dd = np.asarray(flat_d[:, begin:stop], dtype=np.float64)
        cc = np.asarray(flat_c[:, begin:stop], dtype=np.float64)
        valid = np.isfinite(dd) & np.isfinite(cc) & (cc >= coherence_min) & (cc <= 1)
        m = stop - begin
        # Each pixel can have a different valid subgraph: determine which
        # acquisition dates can be linked to the reference using valid edges.
        reachable = np.zeros((n, m), dtype=bool)
        reachable[0] = True
        for _ in range(n):
            changed = False
            for p, (a, b) in enumerate(edges):
                joined = valid[p] & (reachable[a] | reachable[b])
                fresh_a = joined & ~reachable[a]
                fresh_b = joined & ~reachable[b]
                if fresh_a.any() or fresh_b.any():
                    reachable[a] |= joined
                    reachable[b] |= joined
                    changed = True
            if not changed:
                break
        any_support = reachable[1:].any(axis=0)
        usable = valid.copy()
        for p, (a, b) in enumerate(edges):
            usable[p] &= reachable[a] & reachable[b]

        # Coherence-derived least-squares weights are heuristic, not a formal
        # InSAR uncertainty model (multilooking and correlated errors unknown).
        weights = np.where(usable, np.maximum(cc, 0) ** 2, 0.0)
        observation = np.where(usable, dd, 0.0)
        normal = np.einsum('pi,pj,pk->kij', A, A, weights, optimize=True)
        rhs = np.einsum('pi,pk->ki', A, weights * observation, optimize=True)
        # Disconnected dates have unconstrained unknowns. Add diagonal only
        # for these nodes, then explicitly output NaN for them below.
        missing = ~reachable[1:].T
        ix = np.arange(n - 1)
        normal[:, ix, ix] += missing * 1.0 + 1e-9
        try:
            solution = np.linalg.solve(normal, rhs[..., None])[..., 0].T
        except np.linalg.LinAlgError as exc:
            raise ValueError('Singular inversion system; inspect pair connectivity') from exc
        flat_o[0, begin:stop] = np.where(any_support, 0., np.nan)
        for k in range(1, n):
            flat_o[k, begin:stop] = np.where(reachable[k], solution[k-1], np.nan)

        # Per-date summary of the ORIGINAL coherence of incident usable edges.
        quality_sum = np.zeros((n, m), dtype=float)
        quality_count = np.zeros((n, m), dtype=float)
        for p, (a, b) in enumerate(edges):
            count = usable[p].astype(float)
            value = np.where(usable[p], cc[p], 0.)
            quality_sum[a] += value
            quality_sum[b] += value
            quality_count[a] += count
            quality_count[b] += count
        np.divide(quality_sum, quality_count, out=quality_sum,
                  where=quality_count > 0)
        for k in range(n):
            flat_q[k, begin:stop] = np.where(reachable[k] & (quality_count[k] > 0),
                                              quality_sum[k], np.nan)
    return d_out, c_out, dates, edges


def run(args) -> dict:
    index_path = Path(args.input).resolve()
    index, scenes = input_scenes(index_path)
    family = assert_consistent_family(scenes)
    bbox = validate_bbox(args.bbox or index.get('request', {}).get('bbox_wsen'))
    reference = validate_bbox(args.ref_bbox)
    grid = create_grid(bbox, args.epsg, args.resolution_m)
    if not 0 <= args.coh_min <= 1 or args.min_ref_pixels < 1:
        raise ValueError('Invalid coherence threshold or minimum reference pixel count')
    if args.phase_sign not in (-1, 1):
        raise ValueError('phase-sign must be +1 or -1')

    # Every run gets its own immutable output directory, by default.
    run_name = args.run_name or datetime.now(timezone.utc).strftime('run_%Y%m%d_%H%M%S_%f')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', run_name):
        raise ValueError('--run-name may contain only letters, digits, underscore and hyphen')
    output = Path(args.out).resolve() / run_name
    output.mkdir(parents=True, exist_ok=False)
    pcount, ny, nx = len(scenes), grid['height'], grid['width']
    LOG.info('Processing %d interferogram pairs into %s', pcount, output)
    try:
        with tempfile.TemporaryDirectory(prefix='.stage2-', dir=output) as temp:
            scratch = Path(temp)
            pair_d = np.lib.format.open_memmap(scratch / 'pair_d.npy', mode='w+', dtype='float32',
                                                shape=(pcount, ny, nx))
            pair_c = np.lib.format.open_memmap(scratch / 'pair_c.npy', mode='w+', dtype='float32',
                                                shape=(pcount, ny, nx))
            records = []
            for i, (path, scene) in enumerate(scenes):
                LOG.info('Processing pair %d/%d: %s', i+1, pcount, scene['scene'].get('granule_name', path.parent.name))
                pair_d[i], pair_c[i], stats = process_pair(path, scene, args, grid)
                stats['pair_index'] = i
                stats['source_manifest'] = str(path)
                records.append(stats)
            pair_d.flush(); pair_c.flush()
            LOG.info('Inverting %d pair-wise displacement maps into cumulative dates', pcount)
            cumulative, quality, dates, edge_indices = invert_pair_network(
                pair_d, pair_c, records, coherence_min=args.coh_min)
            d_file = np.lib.format.open_memmap(scratch / 'displacement.npy', mode='w+', dtype='float32',
                                               shape=cumulative.shape)
            c_file = np.lib.format.open_memmap(scratch / 'coherence.npy', mode='w+', dtype='float32',
                                               shape=quality.shape)
            d_file[:] = cumulative
            c_file[:] = quality
            d_file.flush(); c_file.flush()
            del d_file, c_file, cumulative, quality, pair_d, pair_c
            tf = grid['transform']
            metadata = {
                'schema_version': 2,
                'stage': 'geophysical_processing_cumulative_stack',
                'created_utc': datetime.now(timezone.utc).isoformat(),
                'array_shape': [len(dates), ny, nx],
                'axis_order': ['acquisition_date', 'row', 'column'],
                'files': {'displacement': 'displacement.npy', 'coherence': 'coherence.npy'},
                'displacement': {
                    'unit': 'mm', 'component': args.component,
                    'meaning': 'CUMULATIVE displacement relative to first acquisition date; weighted pixel-wise inversion of GUNW pair differences',
                    'reference_date': dates[0], 'phase_sign': args.phase_sign,
                    'positive_los_direction': 'toward' if args.phase_sign == -1 else 'unverified_or_away',
                    'phase_sign_verified_for_provisional_products': False,
                    'assumes_zero_horizontal_motion': args.component == 'vertical',
                    'reference_bbox_wsen': list(reference),
                    'coherence_minimum_for_displacement': args.coh_min,
                },
                'coherence': {
                    'unit': 'dimensionless', 'range': [0, 1],
                    'meaning': 'Mean original GUNW pair coherence among usable pair edges incident to each acquisition date, a SUPPORT PROXY, not single-acquisition coherence',
                    'reference_date_has_coherence_proxy_not_independent_measurement': True,
                },
                'grid': {
                    'crs': grid['crs'].to_string(),
                    'transform_affine': [tf.a, tf.b, tf.c, tf.d, tf.e, tf.f],
                    'transform_formula': 'x=a*(col+0.5)+b*(row+0.5)+c; y=d*(col+0.5)+e*(row+0.5)+f',
                    'width': nx, 'height': ny, 'bounds_projected': grid['bbox_projected'],
                    'requested_bbox_wsen': list(bbox), 'pixel_size_m': args.resolution_m,
                    'resampling': 'nearest-neighbor on each phase-connected pair; no interpolation across masked regions',
                },
                'acquisition_family': {
                    'track': family[0], 'frame': family[1], 'direction': family[2],
                    'mode': family[3], 'polarization': family[4], 'release': family[5], 'crid': family[6],
                },
                'dates': dates,
                'time_axis': 'Unique UTC acquisition dates; displacement[0] is zero where connected to first date',
                'pairs': records,
                'inversion': {
                    'method': 'pixelwise weighted least squares anchored at first acquisition date',
                    'weights': 'coherence squared (heuristic)',
                    'missing_data': 'NaN for acquisition dates disconnected from reference at each pixel',
                    'edge_indices': [list(pair) for pair in edge_indices],
                    'cumulative_is_not_sum_when_pairs_overlap': True,
                },
                'correction_policy': {
                    'ionosphere': not args.no_ionosphere, 'troposphere': not args.no_troposphere,
                    'troposphere_constant_height_is_approximate': not args.no_troposphere,
                    'solid_earth_tides': False, 'orbital_ramp': False,
                },
                'scientific_warnings': [
                    'Phase sign and stable reference not independently validated for public hazard warnings.',
                    'A single LOS observation does not uniquely determine vertical motion.',
                    'Coherence.npy after inversion represents incident-pair quality summary, not independent image coherence.',
                    'Time-series uncertainties are not yet estimated; no tropospheric correction when disabled.',
                ],
            }
            (scratch / 'metadata.json').write_text(json.dumps(metadata, indent=2, allow_nan=False)+'\n', encoding='utf-8')
            for file in ('displacement.npy', 'coherence.npy', 'metadata.json'):
                os.replace(scratch / file, output / file)
        LOG.info('Saved cumulative stack %s (shape %s)', output, tuple(metadata['array_shape']))
        return metadata
    except Exception:
        LOG.error('Processing failed, leaving output directory %s for investigation', output)
        raise

def main() -> int:
    p = argparse.ArgumentParser(description='Invert connected GUNW pairs into a cumulative acquisition-date displacement stack')
    p.add_argument('--input', default='data/raw/index.json', help='Stage 1 acquisition index')
    p.add_argument('--out', default='data/processed', help='Parent directory for timestamped run output')
    p.add_argument('--run-name', help='Optional unique output folder name; default is a UTC timestamp')
    p.add_argument('--bbox', nargs=4, type=float, metavar=('W','S','E','N'),
                   help='Output AOI; default is Stage 1 request bbox')
    p.add_argument('--ref-bbox', nargs=4, type=float, required=True, metavar=('W','S','E','N'),
                   help='Land reference area in WGS84 W S E N; must be demonstrably stable')
    p.add_argument('--epsg', type=int, default=32636, help='Target projected EPSG, e.g. 32636 near Ein Gedi')
    p.add_argument('--resolution-m', type=float, default=80.0, help='Common raster pixel spacing in meters')
    p.add_argument('--component', choices=['los', 'vertical'], default='los',
                   help='Primary LOS displacement or approximate vertical estimate')
    p.add_argument('--coh-min', type=float, default=0.3)
    p.add_argument('--min-ref-pixels', type=int, default=10)
    p.add_argument('--no-ionosphere', action='store_true', help='Exploratory only: do not apply ionosphere screen')
    p.add_argument('--no-troposphere', action='store_true', help='Exploratory only: do not apply wet/dry corrections')
    p.add_argument('--frequency-mhz', type=float, help='Optional manual check/override; frequency A is normally inferred from mode')
    p.add_argument('--incidence-deg', type=float, help='Verified incidence override for vertical component')
    p.add_argument('--phase-sign', type=int, choices=(-1,1), default=-1)
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s: %(message)s')
    try:
        run(args)
        return 0
    except (ValueError, FileNotFoundError, rasterio.errors.RasterioError) as exc:
        LOG.error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
