#!/usr/bin/env python3
"""Stage 2 — build a single aligned, pair-wise NISAR GUNW displacement stack.

Output (only three files):
    data/processed/displacement.npy   float32 (T, Y, X), NaN means unusable
    data/processed/coherence.npy      float32 (T, Y, X), NaN means no observation
    data/processed/metadata.json     map grid, dates, reference and provenance

Each time slice is the corrected displacement BETWEEN the reference and
secondary acquisitions of that GUNW interferogram. It is NOT a cumulative
motion time series, and coherence also refers to that acquisition PAIR.

Examples (from project root, PowerShell):
    python src/nisar_processing.py --input data/raw/index.json \
      --ref-bbox 35.3770 31.4490 35.3880 31.4555

    python src/nisar_processing.py --input data/raw/index.json \
      --ref-bbox 35.3770 31.4490 35.3880 31.4555 --component vertical

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
    frequency = float(args.frequency_mhz * 1e6 if args.frequency_mhz is not None
                      else md.get('center_frequency_hz') or float('nan'))
    if not np.isfinite(frequency) or not 1.1e9 < frequency < 1.4e9:
        raise ValueError('Missing/invalid frequency-A center frequency; fix Stage 1 or use a verified --frequency-mhz')
    wavelength = C / frequency

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

    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    t, ny, nx = len(scenes), grid['height'], grid['width']
    est_gb = 2 * t * ny * nx * 4 / (1024**3)
    LOG.info('Allocating two %s float32 arrays, shape (%d,%d,%d), %.3f GiB total',
             args.component, t, ny, nx, est_gb)
    records = []
    with tempfile.TemporaryDirectory(prefix='.stage2-', dir=output) as temp:
        scratch = Path(temp)
        d = np.lib.format.open_memmap(scratch / 'displacement.npy', mode='w+', dtype='float32', shape=(t,ny,nx))
        c = np.lib.format.open_memmap(scratch / 'coherence.npy', mode='w+', dtype='float32', shape=(t,ny,nx))
        for i, (path, scene) in enumerate(scenes):
            LOG.info('Processing pair %d/%d: %s', i+1, t, scene['scene'].get('granule_name', path.parent.name))
            d[i], c[i], stats = process_pair(path, scene, args, grid)
            stats['time_index'] = i
            stats['source_manifest'] = str(path)
            records.append(stats)
            LOG.info('Pair %d: %d valid displacement pixels, baseline %.2f days',
                     i+1, stats['valid_displacement_pixels'], stats['baseline_days'])
        d.flush(); c.flush()
        del d, c
        # A single 2-D affine grid maps EVERY time slice to the same location.
        transform = grid['transform']
        metadata = {
            'schema_version': SCHEMA_VERSION,
            'stage': 'geophysical_processing_stack',
            'created_utc': datetime.now(timezone.utc).isoformat(),
            'array_shape': [t,ny,nx],
            'axis_order': ['time_pair', 'row', 'column'],
            'files': {'displacement': 'displacement.npy', 'coherence': 'coherence.npy'},
            'displacement': {
                'unit': 'mm', 'component': args.component,
                'meaning': 'Relative displacement between EACH GUNW reference and secondary acquisition (NOT cumulative)',
                'phase_sign': args.phase_sign,
                'positive_los_direction': 'toward' if args.phase_sign == -1 else 'unverified_or_away',
                'phase_sign_verified_for_provisional_products': False,
                'assumes_zero_horizontal_motion': args.component == 'vertical',
                'reference_bbox_wsen': list(reference),
                'coherence_minimum_for_displacement': args.coh_min,
                'zero_on_reference': 'Median corrected phase on largest valid unwrapping component in reference bbox',
            },
            'coherence': {
                'unit': 'dimensionless', 'range': [0,1],
                'meaning': 'Interferometric coherence of each reference/secondary acquisition pair',
                'masking': 'NaN outside observed/valid coherence; low coherence retained here, masked in displacement',
            },
            'grid': {
                'crs': grid['crs'].to_string(),
                'transform_affine': [transform.a, transform.b, transform.c,
                                     transform.d, transform.e, transform.f],
                'transform_formula': 'x=a*(col+0.5)+b*(row+0.5)+c; y=d*(col+0.5)+e*(row+0.5)+f',
                'width': nx, 'height': ny, 'bounds_projected': grid['bbox_projected'],
                'requested_bbox_wsen': list(bbox), 'pixel_size_m': args.resolution_m,
                'resampling': 'nearest for pair displacement and coherence; never interpolate across disconnected unwrapping regions',
            },
            'acquisition_family': {'track': family[0], 'frame': family[1], 'direction': family[2],
                                   'mode': family[3], 'polarization': family[4], 'release': family[5],
                                   'crid': family[6]},
            'time_axis': 'One row per GUNW acquisition pair, sorted by secondary acquisition time. Times may repeat.',
            'pairs': records,
            'correction_policy': {
                'ionosphere': not args.no_ionosphere, 'troposphere': not args.no_troposphere,
                'troposphere_constant_height_is_approximate': not args.no_troposphere,
                'solid_earth_tides': False, 'orbital_ramp': False,
            },
            'scientific_warnings': [
                'Do not treat the time axis as cumulative displacement; use acquisition-pair baselines or perform network inversion.',
                'Single-LOS vertical estimates assume horizontal displacement is negligible.',
                'Stable reference and phase sign must be independently validated before public warnings.',
                'Troposphere evaluated at a constant ellipsoidal height is inadequate for precise corrections across strong relief.',
            ],
        }
        (scratch / 'metadata.json').write_text(json.dumps(metadata, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        # Publish only when all pairs were processed successfully.
        for filename in ('displacement.npy', 'coherence.npy', 'metadata.json'):
            os.replace(scratch / filename, output / filename)
    LOG.info('Saved %s (shape %s)', output, (t,ny,nx))
    return metadata


def main() -> int:
    p = argparse.ArgumentParser(description='Stack corrected GUNW pair displacement/coherence arrays')
    p.add_argument('--input', default='data/raw/index.json', help='Stage 1 acquisition index')
    p.add_argument('--out', default='data/processed', help='Directory holding the three outputs')
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
    p.add_argument('--frequency-mhz', type=float, help='Verified frequency override if metadata unavailable')
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
