#!/usr/bin/env python3
"""Stage 3 v2: localized-deformation detection and user-defined screening levels.

Run from the repository root:
    python src/subsidence_detection_v2.py

The acquisition .npz is pairwise/raw. First run nisar_processing_v2.py to
produce CORRECTED CUMULATIVE displacement.npy/coherence.npy/metadata.json.
This script then reads that Stage 2 run and saves to data/detection/<same run>/.

The risk thresholds in this script are USER-DEFINED screening bins inspired by
Dead Sea subsidence research; they are NOT published/validated hazard classes.
LOS direction and stable reference remain provisional.

Dependencies: numpy scipy rasterio affine
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
from affine import Affine
from rasterio.features import shapes
from rasterio.warp import transform_geom
from scipy import ndimage

LOG = logging.getLogger('nisar.stage3_v2')

# ========================= EDIT HERE ================================
# Specify an exact Stage 2 directory OR data/processed to select the latest.
PROCESSED = Path("data/processed")
OUTPUT_ROOT = Path("data/detection")

# Candidate detection thresholds are distinct from risk screening thresholds.
# Note: 10 mm/year is only ~0.33 mm over 12 days, beneath typical InSAR noise;
# a low-risk label is NOT evidence that small sinks are reliably detected.
COHERENCE_MIN = 0.35
LOCAL_SIGMA_M = 100.0
BACKGROUND_SIGMA_M = 900.0
MINIMUM_MOVEMENT_MM = 3.0
MINIMUM_RATE_MM_DAY = 0.0
MINIMUM_SNR = 2.5
NOISE_FLOOR_MM = 1.5
MINIMUM_PIXELS = 4
DETECT_CUMULATIVE_ANOMALIES = True  # Also identify slower persistent motion.

# USER-SPECIFIED (not paper-calibrated) recent LOS-away rate bins [mm/year].
WATCH_MIN_MM_YEAR = 10.0
ELEVATED_MIN_MM_YEAR = 40.0
HIGH_ABOVE_MM_YEAR = 100.0
# "Accelerating in last three intervals" means three usable consecutive
# intervals with successive sinking-rate increases of >= this amount.
ACCEL_MIN_STEP_MM_YEAR = 5.0
MIN_TEMPORAL_PIXELS = 3
# ====================================================================

YEAR_DAYS = 365.25


def select_processing_run(path: Path) -> Path:
    """Accept a Stage 2 run, or choose the newest valid run under a directory."""
    if (path / 'metadata.json').is_file():
        return path
    if path.is_dir():
        runs = [p.parent for p in path.glob('*/metadata.json')
                if json.loads(p.read_text(encoding='utf-8')).get('stage')
                == 'geophysical_processing_cumulative_stack']
        if runs:
            chosen = max(runs, key=lambda p: (p / 'metadata.json').stat().st_mtime)
            LOG.info('Selected latest Stage 2 run: %s', chosen)
            return chosen
    raise FileNotFoundError(f'No cumulative Stage 2 run found at {path}')


def classify_rate(subsidence_mm_year: float | None,
                  acceleration_last_3: bool | None) -> str | None:
    """Classify LOS-away speed in user-defined screening categories.

    Exactly 100 -> elevated when accelerating, else watch; >100 -> high.
    Unknown acceleration means 'without confirmed acceleration' -> watch.
    """
    if subsidence_mm_year is None or not math.isfinite(subsidence_mm_year):
        return None
    speed = max(0., float(subsidence_mm_year))
    if speed > HIGH_ABOVE_MM_YEAR:
        return 'high'
    if speed >= ELEVATED_MIN_MM_YEAR and acceleration_last_3 is True:
        return 'elevated'
    if speed >= WATCH_MIN_MM_YEAR:
        return 'watch'
    return 'low'


def temporal_metrics(dis: np.ndarray, coherence: np.ndarray, day_numbers: np.ndarray,
                     region_mask: np.ndarray, last_date_index: int,
                     coh_min: float, min_pixels: int = MIN_TEMPORAL_PIXELS) -> dict:
    """Fixed-footprint medians; never impute unavailable acquisition dates.

    Last interval is the reporting-rate window; three latest complete intervals
    are required for the acceleration check. The returned signed rate is LOS
    mm/year (toward positive under Stage 2's provisional convention).
    """
    first = last_date_index - 1
    if first < 0:
        return {'velocity_mm_year': None, 'subsidence_mm_year': None,
                'accelerating_last_3': None, 'recent_rates_mm_year': []}
    valid = region_mask & np.isfinite(dis[first]) & np.isfinite(dis[last_date_index])
    valid &= (np.isfinite(coherence[first]) & np.isfinite(coherence[last_date_index])
              & (coherence[first] >= coh_min) & (coherence[last_date_index] >= coh_min))
    if np.count_nonzero(valid) < min_pixels:
        return {'velocity_mm_year': None, 'subsidence_mm_year': None,
                'accelerating_last_3': None, 'recent_rates_mm_year': []}
    dt = float(day_numbers[last_date_index] - day_numbers[first])
    if dt <= 0:
        raise ValueError('Non-positive acquisition interval')
    # Use the same spatial footprint across the two dates, not differing masks.
    change = dis[last_date_index, valid].astype(float) - dis[first, valid].astype(float)
    signed_rate = float(np.median(change) / dt * YEAR_DAYS)
    subsidence = max(0., -signed_rate)
    accelerated = None
    rates = []
    if last_date_index >= 3:
        a = last_date_index - 3
        all_valid = region_mask.copy()
        for k in range(a, last_date_index+1):
            all_valid &= (np.isfinite(dis[k]) & np.isfinite(coherence[k])
                          & (coherence[k] >= coh_min))
        if np.count_nonzero(all_valid) >= min_pixels:
            daily_diffs = np.diff(dis[a:last_date_index+1, all_valid].astype(float), axis=0)
            intervals = np.diff(day_numbers[a:last_date_index+1])
            # Median at fixed pixels of the subsidence rates (mm/year).
            rates = list(map(float, np.median(-daily_diffs / intervals[:, None]
                                               * YEAR_DAYS, axis=1)))
            accelerated = (rates[0] > 0 and
                           rates[1] - rates[0] >= ACCEL_MIN_STEP_MM_YEAR and
                           rates[2] - rates[1] >= ACCEL_MIN_STEP_MM_YEAR)
    return {'velocity_mm_year': signed_rate, 'subsidence_mm_year': subsidence,
            'accelerating_last_3': accelerated, 'recent_rates_mm_year': rates}



@dataclass(frozen=True)
class DetectionConfig:
    coherence_min: float = 0.3
    local_sigma_m: float = 100.0
    background_sigma_m: float = 900.0
    minimum_filter_support: float = 0.35
    minimum_movement_mm: float = 3.0
    minimum_rate_mm_day: float = 0.0
    minimum_snr: float = 2.5
    noise_floor_mm: float = 1.5
    minimum_pixels: int = 4
    persistence_overlap_fraction: float = 0.2
    matching_dilation_pixels: int = 1

    def check(self):
        if not (0 <= self.coherence_min <= 1):
            raise ValueError('coherence_min must be 0..1')
        if not (0 < self.local_sigma_m < self.background_sigma_m):
            raise ValueError('local_sigma_m must be smaller than background_sigma_m')
        if not (0 < self.minimum_filter_support <= 1):
            raise ValueError('minimum_filter_support must be in (0,1]')
        if min(self.minimum_movement_mm, self.minimum_rate_mm_day,
               self.minimum_snr) < 0:
            raise ValueError('Thresholds must not be negative')
        if self.noise_floor_mm <= 0 or self.minimum_pixels < 1:
            raise ValueError('noise floor must be positive and min pixels >=1')
        if not 0 < self.persistence_overlap_fraction <= 1:
            raise ValueError('overlap fraction must be between 0 and 1')
        if self.matching_dilation_pixels < 0:
            raise ValueError('matching dilation must be >=0')


def smooth_valid(data: np.ndarray, valid: np.ndarray, sy: float, sx: float):
    """Normalized Gaussian: no NaNs bleeding into valid terrain."""
    data = np.asarray(data, dtype=np.float64)
    w = valid.astype(np.float64)
    num = ndimage.gaussian_filter(np.where(valid, data, 0.), (sy, sx), mode='constant', cval=0.)
    den = ndimage.gaussian_filter(w, (sy, sx), mode='constant', cval=0.)
    result = np.full(data.shape, np.nan)
    np.divide(num, den, out=result, where=den > 1e-8)
    return result, den


def detect_interval(delta: np.ndarray, coherence: np.ndarray, duration_days: float,
                    pixel_steps: tuple[float,float], cfg: DetectionConfig):
    """Find local anomalies relative to broad surrounding deformation."""
    good = np.isfinite(delta) & np.isfinite(coherence) & (coherence >= cfg.coherence_min)
    if int(good.sum()) < max(50, 5 * cfg.minimum_pixels):
        return np.zeros(delta.shape, dtype=bool), np.full(delta.shape,np.nan), {'status': 'insufficient_pixels'}
    sy = cfg.local_sigma_m / pixel_steps[0]
    sx = cfg.local_sigma_m / pixel_steps[1]
    by = cfg.background_sigma_m / pixel_steps[0]
    bx = cfg.background_sigma_m / pixel_steps[1]
    local, lw = smooth_valid(delta, good, sy, sx)
    background, bw = smooth_valid(delta, good, by, bx)
    eligible = (good & (lw >= cfg.minimum_filter_support) & (bw >= cfg.minimum_filter_support)
                & np.isfinite(local) & np.isfinite(background))
    residual = local - background
    if int(eligible.sum()) < max(50, 5 * cfg.minimum_pixels):
        return np.zeros(delta.shape, dtype=bool), residual, {'status': 'insufficient_spatial_support'}
    values = residual[eligible]
    center = float(np.median(values))
    noise = max(cfg.noise_floor_mm, 1.4826 * float(np.median(np.abs(values-center))))
    threshold = max(cfg.minimum_movement_mm, cfg.minimum_rate_mm_day*duration_days,
                    cfg.minimum_snr*noise)
    centered = residual - center
    # A localized LOS movement AWAY from the satellite is the expected
    # direction of vertical subsidence (horizontal motion can also contribute).
    candidate = eligible & (centered <= -threshold)
    # Keep original NaN areas as invalid even if filtering interpolates nearby signals.
    residual[~eligible] = np.nan
    return candidate, centered, {'status': 'ok', 'noise_mm': noise,
                                  'threshold_mm': threshold, 'valid_pixels': int(eligible.sum()),
                                  'median_local_residual_mm': center}


def load_stack(folder: Path):
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    meta = json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
    if meta.get('stage') != 'geophysical_processing_cumulative_stack':
        raise ValueError('Stage 3 requires cumulative Stage 2 data; rerun Stage 2 with new script')
    d = np.load(folder / meta['files']['displacement'], mmap_mode='r')
    c = np.load(folder / meta['files']['coherence'], mmap_mode='r')
    if d.shape != c.shape or d.ndim != 3 or list(d.shape) != meta.get('array_shape'):
        raise ValueError('Stage 2 array shape mismatch')
    dates = meta.get('dates')
    if not isinstance(dates,list) or len(dates) != d.shape[0]:
        raise ValueError('Expected one acquisition date for each stack slice')
    days = np.array([date.fromisoformat(v).toordinal() for v in dates], dtype=float)
    if not np.all(np.diff(days) > 0):
        raise ValueError('Acquisition dates must increase')
    crs = meta.get('grid',{}).get('crs')
    aff_vals = meta.get('grid',{}).get('transform_affine')
    if not crs or not aff_vals or len(aff_vals) != 6:
        raise ValueError('Missing projected grid georeferencing')
    if not (crs.upper().startswith('EPSG:') and 32600 < int(crs.split(':')[-1]) < 32800):
        raise ValueError('This detector expects projected UTM EPSG coordinates in metres')
    return d, c, meta, days, Affine(*aff_vals), crs


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def main_process(folder: Path, out_parent: Path, cfg: DetectionConfig) -> dict:
    cfg.check()
    dis, coherence, meta, days, transform, crs = load_stack(folder)
    if meta.get('displacement',{}).get('component') != 'los':
        LOG.warning('Input uses %s (not primary LOS); classification assumes positive-up vertical if sign verified',
                    meta.get('displacement',{}).get('component'))
    n, ny, nx = dis.shape
    if n < 2:
        raise ValueError('At least two acquisition dates are required')
    dt = np.diff(days)
    mid = (days[1:] + days[:-1])/2
    step_x = math.hypot(transform.a, transform.d)
    step_y = math.hypot(transform.b, transform.e)
    area = abs(transform.a*transform.e - transform.b*transform.d)
    if min(step_x,step_y,area) <= 0:
        raise ValueError('Invalid projected grid resolution')
    out = out_parent / folder.name
    if out.resolve() == folder.resolve():
        raise ValueError('Detection output cannot be the Stage 2 input folder')
    out.mkdir(parents=True, exist_ok=True)

    LOG.info('Computing velocity and acceleration from %d cumulative acquisition dates', n)
    v = np.full((n-1,ny,nx),np.nan,np.float32)
    # Differences between cumulative displacements in adjacent dates. NaN if either unavailable.
    for i in range(n-1):
        v[i] = (dis[i+1].astype(np.float32) - dis[i].astype(np.float32)) / dt[i]
    a = np.full((max(0,n-2),ny,nx),np.nan,np.float32)
    for i in range(n-2):
        a[i] = (v[i+1] - v[i]) / (mid[i+1]-mid[i])
    np.save(out/'velocity.npy',v)
    np.save(out/'acceleration.npy',a)

    mask = np.zeros((n,ny,nx),np.uint8)
    # Track one polygon per persistent spatial patch, saving its latest geometry.
    tracks: list[dict] = []
    states: list[dict] = []
    sign_verified = meta.get('displacement',{}).get('phase_sign_verified_for_provisional_products') is True
    toward = meta.get('displacement',{}).get('positive_los_direction') == 'toward'

    for t in range(1, n):
        delta = np.asarray(dis[t]-dis[t-1],dtype=float)
        # Use conservative quality: both dates must be supported.
        q0 = np.asarray(coherence[t-1],dtype=float)
        q1 = np.asarray(coherence[t],dtype=float)
        pair_support = np.where(np.isfinite(q0) & np.isfinite(q1), np.minimum(q0,q1), np.nan)
        candidate, residual, stats = detect_interval(
            delta, pair_support, dt[t-1], (step_y, step_x), cfg)
        cumulative_stats = None
        if DETECT_CUMULATIVE_ANOMALIES and t >= 2:
            q_start = np.asarray(coherence[0], dtype=float)
            q_cum = np.where(np.isfinite(q_start) & np.isfinite(q1),
                             np.minimum(q_start, q1), np.nan)
            cum_candidate, cum_residual, cumulative_stats = detect_interval(
                np.asarray(dis[t]-dis[0], dtype=float), q_cum,
                days[t]-days[0], (step_y, step_x), cfg)
            residual = np.where(candidate, residual, cum_residual)
            candidate |= cum_candidate
        labelled, nlabels = ndimage.label(candidate, structure=ndimage.generate_binary_structure(2,1))
        accepted = np.zeros((ny,nx),dtype=np.int32)
        components = []
        for lab in range(1,nlabels+1):
            pix = labelled==lab
            count = int(pix.sum())
            if count < cfg.minimum_pixels:
                continue
            mean_signed = float(np.nanmean(residual[pix]))
            mean_velocity = float(np.nanmean(v[t-1][pix]))
            mean_cum = float(np.nanmean(np.asarray(dis[t])[pix]))
            mean_coherence = float(np.nanmean(pair_support[pix]))
            accelerated = float(np.nanmean(a[t-2][pix])) if t>=2 and np.isfinite(a[t-2][pix]).any() else None
            components.append({'label':lab, 'mask':pix, 'pixel_count':count,
                               'mean_local_change_mm':mean_signed,
                               'mean_velocity_mm_day':mean_velocity,
                               'mean_acceleration_mm_day2':accelerated,
                               'mean_cumulative_displacement_mm':mean_cum,
                               'mean_coherence':mean_coherence})
            accepted[pix] = lab
        mask[t] = (accepted>0).astype('uint8')
        # Polygonize each accepted component, then match it to prior interval's tracks.
        polygons = {}
        for geom, val in shapes(accepted, mask=accepted>0, transform=transform):
            if int(val) > 0:
                polygons.setdefault(int(val), []).append(transform_geom(crs,'EPSG:4326',geom,precision=7))
        used_tracks = set()
        for comp in components:
            current_mask=comp.pop('mask')
            chosen = None
            best_overlap = 0.0
            for k,track in enumerate(tracks):
                if track['last_interval'] != t-1 or k in used_tracks:
                    continue
                previous = track['last_mask']
                expanded = ndimage.binary_dilation(previous, iterations=cfg.matching_dilation_pixels) if cfg.matching_dilation_pixels else previous
                overlap = int((expanded & current_mask).sum()) / max(1, int(current_mask.sum()))
                if overlap >= cfg.persistence_overlap_fraction and overlap > best_overlap:
                    chosen = k
                    best_overlap = overlap
            if chosen is None:
                id_basis=f"{folder.name}-{meta['dates'][t]}-{int(np.flatnonzero(current_mask)[0])}"
                track = {'id':'anomaly-'+hashlib.sha1(id_basis.encode()).hexdigest()[:14],
                         'first_interval': t, 'last_interval':t, 'detections':1,
                         'last_mask':current_mask,'history':[],'last_geo':[], 'last_comp':{}}
                tracks.append(track)
                chosen = len(tracks)-1
            else:
                track=tracks[chosen]
                track['detections']+=1
            used_tracks.add(chosen)
            track['last_interval']=t
            track['last_mask']=current_mask
            track['last_geo']=polygons.get(comp['label'],[])
            track['last_comp']=comp
            track['history'].append({'date':meta['dates'][t],
                                     'velocity_mm_day':comp['mean_velocity_mm_day'],
                                     'acceleration_mm_day2':comp['mean_acceleration_mm_day2']})
        states.append({'interval_index':t, 'start':meta['dates'][t-1], 'end':meta['dates'][t],
                       'duration_days':float(dt[t-1]),
                       'regions':len(components), 'screening':stats,
                       'cumulative_screening':cumulative_stats})
        LOG.info('%s → %s: %d region(s)',meta['dates'][t-1],meta['dates'][t],len(components))

    np.save(out/'candidate_mask.npy', mask)
    features=[]
    for tr in tracks:
        c=tr['last_comp']
        if not tr['last_geo']:
            continue
        sign_supports_subsidence = toward and c['mean_local_change_mm'] < 0
        persistence = tr['detections'] >= 2
        # The latest polygon is held fixed across the evaluated dates.
        screening = temporal_metrics(dis, coherence, days, tr['last_mask'],
                                     n-1, cfg.coherence_min)
        # Explicitly user-defined screening bins, not confirmed physical hazard.
        screening['risk_level'] = (classify_rate(screening['subsidence_mm_year'],
                              screening['accelerating_last_3'])
                              if toward and meta.get('displacement',{}).get('component') == 'los'
                              else None)
        accel = c['mean_acceleration_mm_day2']
        priority = ('urgent_review' if screening['risk_level'] == 'high' else
                    'review' if screening['risk_level'] == 'elevated' else
                    'watch')
        properties={
            'id':tr['id'],
            'classification':'subsidence_candidate' if sign_supports_subsidence else 'deformation_anomaly',
            'priority':priority,
            'evidence':'repeated' if persistence else 'single_interval',
            'review_status':'unreviewed',
            'first_observed':meta['dates'][tr['first_interval']],
            'last_observed':meta['dates'][tr['last_interval']],
            'observation_count':tr['detections'],
            'acquisition_pairs':tr['detections'],
            'area_m2':c['pixel_count']*area,
            'pixel_count':c['pixel_count'],
            'mean_los_displacement_mm':c['mean_cumulative_displacement_mm'] if meta['displacement']['component']=='los' else None,
            'mean_vertical_displacement_mm':c['mean_cumulative_displacement_mm'] if meta['displacement']['component']=='vertical' else None,
            'mean_vertical_rate_mm_per_day':c['mean_velocity_mm_day'] if meta['displacement']['component']=='vertical' else None,
            'mean_rate_mm_day':c['mean_velocity_mm_day'],
            'mean_acceleration_mm_day2':accel,
            'mean_coherence':c['mean_coherence'],
            'risk_level': screening['risk_level'],
            'recent_los_velocity_mm_per_year': screening['velocity_mm_year'],
            'recent_subsidence_mm_per_year': screening['subsidence_mm_year'],
            'accelerating_last_3_intervals': screening['accelerating_last_3'],
            'recent_3_interval_rates_mm_per_year': screening['recent_rates_mm_year'],
            'risk_method': 'user_defined_los_rate_bins_v1',
            'risk_as_of': meta['dates'][-1],
            'detected_on_latest_date': tr['last_interval'] == n-1,
            'mean_local_change_mm':c['mean_local_change_mm'],
            'sign_verified':sign_verified,
            'review_required':True,
            'source_run':folder.name,
            'processed_at':datetime.now(timezone.utc).isoformat(),
        }
        geoms=tr['last_geo']
        if len(geoms)==1:
            geometry=geoms[0]
        else:
            geometry={'type':'MultiPolygon', 'coordinates':[p['coordinates'] for p in geoms if p['type']=='Polygon']}
        features.append({'type':'Feature','id':tr['id'], 'geometry':geometry,'properties':properties})
    geojson={'type':'FeatureCollection','features':features,
             'meta':{'stage':'exploratory_cumulative_time_series_screening','total':len(features),
                     'source_run':folder.name,'official_hazard_assessment':False}}
    write_json(out/'anomalies.geojson',geojson)
    out_metadata={'stage':'subsidence_detection','schema_version':2,
                  'source_processing_directory':str(folder.resolve()),
                  'files':{'velocity':'velocity.npy','acceleration':'acceleration.npy',
                           'candidate_mask':'candidate_mask.npy','polygons':'anomalies.geojson'},
                  'dates':meta['dates'],'velocity_units':'mm/day', 'acceleration_units':'mm/day^2',
                  'velocity_shape':[n-1,ny,nx],'acceleration_shape':[max(0,n-2),ny,nx],
                  'candidate_mask_shape':[n,ny,nx], 'detection_config':asdict(cfg),
                  'cumulative_spatial_detection_enabled': DETECT_CUMULATIVE_ANOMALIES,
                  'risk_scheme':{'low':'0 to <10 mm/year', 'watch':'10 to <40 mm/year OR 40 to 100 without three-interval acceleration',
                                 'elevated':'40 to 100 mm/year with three-interval acceleration',
                                 'high':'>100 mm/year', 'speed_direction':'negative LOS (away from satellite)',
                                 'rate_window':'latest observation interval for all polygons, even if last detected earlier',
                                 'acceleration_window':'last three complete intervals; consecutive increases >=5 mm/year',
                                 'source':'user_specified_experimental_not_paper_calibrated'},
                  'intervals':states,'n_tracked_anomalies':len(features),
                  'warnings':[
                      'This is exploratory deformation screening, not a validated sinkhole warning system.',
                      'Nof et al. (2019) supports monitoring localized subsidence and acceleration, but does NOT validate these numerical 10/40/100 mm/year bins.',
                      'Per-date coherence is an incident-pair quality proxy from Stage 2.',
                      'Acceleration is a noisy finite-difference estimate, not a fitted significance test.',
                      'Atmospheric trends and reference errors may cause false positives.',
                      'Physical LOS sign and stable ground reference require independent validation.',
                      'Risk labels are exploratory LOS screening categories, not operational safety or true vertical rates.'
                  ]}
    write_json(out/'metadata.json',out_metadata)
    LOG.info('Saved Stage 3 outputs: %s (%d tracked polygon(s))',out,len(features))
    return out_metadata


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    cfg = DetectionConfig(
        coherence_min=COHERENCE_MIN, local_sigma_m=LOCAL_SIGMA_M,
        background_sigma_m=BACKGROUND_SIGMA_M,
        minimum_movement_mm=MINIMUM_MOVEMENT_MM,
        minimum_rate_mm_day=MINIMUM_RATE_MM_DAY,
        minimum_snr=MINIMUM_SNR, noise_floor_mm=NOISE_FLOOR_MM,
        minimum_pixels=MINIMUM_PIXELS)
    folder = select_processing_run(PROCESSED)
    main_process(folder, OUTPUT_ROOT, cfg)
    print(f'Stage 3 ready: {OUTPUT_ROOT / folder.name}')


if __name__ == '__main__':
    main()
