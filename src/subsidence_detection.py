#!/usr/bin/env python3
"""Stage 3 — exploratory localized deformation detection from a cumulative NISAR stack.

Inputs from Stage 2:
    data/processed/run_.../{displacement.npy,coherence.npy,metadata.json}

Outputs:
    data/detection/<SAME RUN NAME>/
        velocity.npy       (T-1,Y,X) mm/day
        acceleration.npy   (T-2,Y,X) mm/day^2
        candidate_mask.npy (T,Y,X) binary, t=0 is zero
        anomalies.geojson  latest geometry of each tracked anomaly
        metadata.json      thresholds, results, caveats

Method: no-data-aware small Gaussian smoothing; rate map per interval;
large-scale background subtraction; robust MAD detection; connected components;
polygonization and temporal matching. It is inspired by the early-warning
monitoring logic of Nof et al. (2019), not a validated hazard prediction system.

Example:
    python src/subsidence_detection.py --input data/processed/run_20261114_180000_000001
    python src/subsidence_detection.py --input data/processed/run_... --min-mm 8 --min-rate 0.3

Dependencies: numpy scipy rasterio
"""
from __future__ import annotations

import argparse
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

LOG = logging.getLogger('nisar.stage3')


@dataclass(frozen=True)
class DetectionConfig:
    coherence_min: float = 0.3
    local_sigma_m: float = 100.0
    background_sigma_m: float = 900.0
    minimum_filter_support: float = 0.35
    minimum_movement_mm: float = 8.0
    minimum_rate_mm_day: float = 0.3
    minimum_snr: float = 3.0
    noise_floor_mm: float = 2.0
    minimum_pixels: int = 4
    persistence_overlap_fraction: float = 0.2
    matching_dilation_pixels: int = 1
    acceleration_threshold_mm_day2: float = 0.02

    def check(self):
        if not (0 <= self.coherence_min <= 1):
            raise ValueError('coherence_min must be 0..1')
        if not (0 < self.local_sigma_m < self.background_sigma_m):
            raise ValueError('local_sigma_m must be smaller than background_sigma_m')
        if not (0 < self.minimum_filter_support <= 1):
            raise ValueError('minimum_filter_support must be in (0,1]')
        if min(self.minimum_movement_mm, self.minimum_rate_mm_day,
               self.minimum_snr, self.acceleration_threshold_mm_day2) < 0:
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
    candidate = eligible & (np.abs(centered) >= threshold)
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
        pair_support = np.fmin(q0,q1)
        candidate, residual, stats = detect_interval(delta, pair_support, dt[t-1],(step_y,step_x),cfg)
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
                       'regions':len(components), 'screening':stats})
        LOG.info('%s → %s: %d region(s)',meta['dates'][t-1],meta['dates'][t],len(components))

    np.save(out/'candidate_mask.npy', mask)
    features=[]
    for tr in tracks:
        c=tr['last_comp']
        if not tr['last_geo']:
            continue
        sign_supports_subsidence = sign_verified and toward and c['mean_local_change_mm'] < 0
        persistence = tr['detections']>=2
        # A speed increase in away-from-satellite direction is potentially
        # consistent with vertical subsidence only under a validated geometry.
        accel = c['mean_acceleration_mm_day2']
        accelerating = (persistence and sign_supports_subsidence and accel is not None and
                        accel <= -cfg.acceleration_threshold_mm_day2)
        priority = 'urgent_review' if accelerating else 'review' if persistence else 'watch'
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
    out_metadata={'stage':'subsidence_detection','schema_version':1,
                  'source_processing_directory':str(folder.resolve()),
                  'files':{'velocity':'velocity.npy','acceleration':'acceleration.npy',
                           'candidate_mask':'candidate_mask.npy','polygons':'anomalies.geojson'},
                  'dates':meta['dates'],'velocity_units':'mm/day', 'acceleration_units':'mm/day^2',
                  'velocity_shape':[n-1,ny,nx],'acceleration_shape':[max(0,n-2),ny,nx],
                  'candidate_mask_shape':[n,ny,nx], 'detection_config':asdict(cfg),
                  'intervals':states,'n_tracked_anomalies':len(features),
                  'warnings':[
                      'This is exploratory deformation screening, not a validated sinkhole warning system.',
                      'Published Nof et al. monitoring logic inspired temporal review categories; numeric thresholds are not paper validated.',
                      'Per-date coherence is an incident-pair quality proxy from Stage 2.',
                      'Acceleration is a noisy finite-difference estimate, not a fitted significance test.',
                      'Atmospheric trends and reference errors may cause false positives.',
                      'Physical LOS sign and stable ground reference require independent validation.'
                  ]}
    write_json(out/'metadata.json',out_metadata)
    LOG.info('Saved Stage 3 outputs: %s (%d tracked polygon(s))',out,len(features))
    return out_metadata


def main():
    p=argparse.ArgumentParser(description='Detect candidate deformation polygons from cumulative NISAR data')
    p.add_argument('--input',required=True, help='One Stage 2 run directory, e.g. data/processed/run_...')
    p.add_argument('--out',default='data/detection', help='Parent output folder; creates SAME subfolder name as input run')
    p.add_argument('--coh-min',type=float,default=0.3)
    p.add_argument('--local-sigma-m',type=float,default=100)
    p.add_argument('--background-sigma-m',type=float,default=900)
    p.add_argument('--min-mm',type=float,default=8)
    p.add_argument('--min-rate',type=float,default=0.3)
    p.add_argument('--min-snr',type=float,default=3)
    p.add_argument('--min-pixels',type=int,default=4)
    p.add_argument('--noise-floor-mm',type=float,default=2)
    p.add_argument('--min-filter-support',type=float,default=0.35)
    p.add_argument('--accel-threshold',type=float,default=0.02)
    args=p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    cfg=DetectionConfig(coherence_min=args.coh_min,local_sigma_m=args.local_sigma_m,
                        background_sigma_m=args.background_sigma_m,
                        minimum_filter_support=args.min_filter_support,
                        minimum_movement_mm=args.min_mm,minimum_rate_mm_day=args.min_rate,
                        minimum_snr=args.min_snr,noise_floor_mm=args.noise_floor_mm,
                        minimum_pixels=args.min_pixels,
                        acceleration_threshold_mm_day2=args.accel_threshold)
    main_process(Path(args.input),Path(args.out),cfg)

if __name__ == '__main__':
    main()
