#!/usr/bin/env python3
"""NISAR Stage 3 v3: robust history-based LOS deformation screening.

Run from repository root:
    python src/subsidence_detection_v3.py

Input is the *corrected cumulative* Stage 2 run, not the raw acquisition .npz.
Risk bins are user-defined experimental LOS-away screening levels, NOT calibrated
sinkhole probabilities or verified vertical subsidence. See STAGE3_V3_GUIDE.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
from affine import Affine
from rasterio.features import shapes
from rasterio.warp import transform_geom
from scipy import ndimage

# ============================== EDIT HERE ==============================
# Exact processed run or parent data/processed (selects the most recent run).
PROCESSED = Path("data/processed")
OUTPUT_ROOT = Path("data/detection")

COHERENCE_MIN = 0.35
MIN_HISTORY_DATES = 5
MIN_HISTORY_SPAN_DAYS = 48
MIN_ZONE_PIXELS = 5
MIN_ZONE_DATES = 5
MIN_ZONE_COHERENT_PIXELS = 5
MIN_ZONE_DATE_FRACTION = 0.50
MIN_ZONE_COMMON_DATE_FRACTION = 0.70

# Detect local LOS-away velocity relative to broader background.
LOCAL_SIGMA_M = 90.0
BACKGROUND_SIGMA_M = 900.0
MIN_FILTER_SUPPORT = 0.35
MIN_LOCAL_RATE_MM_YEAR = 10.0
LOCAL_RATE_SNR = 2.0
SPATIAL_RATE_NOISE_FLOOR_MM_YEAR = 5.0
MIN_ABSOLUTE_AWAY_RATE_MM_YEAR = 10.0

# Separately detect a persistent abrupt LOS-away step in any pair of >=2-date windows.
DETECT_ABRUPT_STEPS = True
ABRUPT_MIN_STEP_MM = 12.0
ABRUPT_STEP_SNR = 2.5
ABRUPT_NOISE_FLOOR_MM = 3.0

# User-defined risk screening bins, NOT calibrated hazard categories.
WATCH_MIN_MM_YEAR = 10.0
ELEVATED_MIN_MM_YEAR = 40.0
HIGH_ABOVE_MM_YEAR = 100.0
# Compare robust slope of last 4 dates to the preceding 4 (1-date overlap).
ACCEL_MIN_RATE_CHANGE_MM_YEAR = 10.0
ACCEL_SIGNIFICANCE_SIGMA = 1.5
# If the full-history rate has excessive uncertainty, leave risk unclassified.
MAX_TREND_RELATIVE_UNCERTAINTY = 0.75
MAX_TREND_ABSOLUTE_UNCERTAINTY_MM_YEAR = 20.0

# Existing Stage 2 temporal spike filter may have replaced some dates. Those
# values are never counted as *independent evidence* when the mask is present.
EXCLUDE_TEMPORAL_REPLACEMENTS = True
# ======================================================================

YEAR_DAYS = 365.25
RISK_METHOD = "user_defined_full_history_los_trend_v2"
LOG = logging.getLogger("nisar.stage3_v3")


@dataclass(frozen=True)
class DetectionConfig:
    coherence_min: float = COHERENCE_MIN
    min_history_dates: int = MIN_HISTORY_DATES
    min_history_span_days: float = MIN_HISTORY_SPAN_DAYS
    min_zone_pixels: int = MIN_ZONE_PIXELS
    min_zone_dates: int = MIN_ZONE_DATES
    min_zone_coherent_pixels: int = MIN_ZONE_COHERENT_PIXELS
    min_zone_date_fraction: float = MIN_ZONE_DATE_FRACTION
    min_zone_common_date_fraction: float = MIN_ZONE_COMMON_DATE_FRACTION
    local_sigma_m: float = LOCAL_SIGMA_M
    background_sigma_m: float = BACKGROUND_SIGMA_M
    min_filter_support: float = MIN_FILTER_SUPPORT
    min_local_rate_mm_year: float = MIN_LOCAL_RATE_MM_YEAR
    local_rate_snr: float = LOCAL_RATE_SNR
    spatial_rate_noise_floor_mm_year: float = SPATIAL_RATE_NOISE_FLOOR_MM_YEAR
    min_absolute_away_rate_mm_year: float = MIN_ABSOLUTE_AWAY_RATE_MM_YEAR
    detect_abrupt_steps: bool = DETECT_ABRUPT_STEPS
    abrupt_min_step_mm: float = ABRUPT_MIN_STEP_MM
    abrupt_step_snr: float = ABRUPT_STEP_SNR
    abrupt_noise_floor_mm: float = ABRUPT_NOISE_FLOOR_MM
    accel_min_rate_change_mm_year: float = ACCEL_MIN_RATE_CHANGE_MM_YEAR
    accel_significance_sigma: float = ACCEL_SIGNIFICANCE_SIGMA
    max_trend_relative_uncertainty: float = MAX_TREND_RELATIVE_UNCERTAINTY
    max_trend_absolute_uncertainty_mm_year: float = MAX_TREND_ABSOLUTE_UNCERTAINTY_MM_YEAR
    exclude_temporal_replacements: bool = EXCLUDE_TEMPORAL_REPLACEMENTS

    def check(self):
        if not 0 <= self.coherence_min <= 1:
            raise ValueError("COHERENCE_MIN must be 0..1")
        if not (0 < self.local_sigma_m < self.background_sigma_m):
            raise ValueError("Require 0 < LOCAL_SIGMA_M < BACKGROUND_SIGMA_M")
        if min(self.min_history_dates, self.min_zone_pixels, self.min_zone_dates,
               self.min_zone_coherent_pixels) < 2:
            raise ValueError("Minimum sample/pixel counts must be >= 2")
        if not 0 < self.min_zone_date_fraction <= 1 or not 0 < self.min_zone_common_date_fraction <= 1:
            raise ValueError("Zone fraction thresholds must be (0,1]")
        if not 0 < self.min_filter_support <= 1:
            raise ValueError("MIN_FILTER_SUPPORT must be (0,1]")
        if min(self.min_local_rate_mm_year, self.local_rate_snr,
               self.spatial_rate_noise_floor_mm_year, self.abrupt_min_step_mm,
               self.abrupt_step_snr, self.abrupt_noise_floor_mm,
               self.accel_min_rate_change_mm_year, self.accel_significance_sigma) < 0:
            raise ValueError("Thresholds must not be negative")


def select_processing_run(path: Path) -> Path:
    if (path / "metadata.json").is_file():
        return path
    if path.is_dir():
        candidates = []
        for file in path.glob("*/metadata.json"):
            try:
                if json.loads(file.read_text(encoding="utf-8")).get("stage") == "geophysical_processing_cumulative_stack":
                    candidates.append(file.parent)
            except (ValueError, OSError):
                continue
        if candidates:
            return max(candidates, key=lambda x: (x / "metadata.json").stat().st_mtime)
    raise FileNotFoundError(f"No Stage 2 cumulative run found under {path}")


def load_stack(folder: Path, cfg: DetectionConfig):
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("stage") != "geophysical_processing_cumulative_stack":
        raise ValueError("Expected corrected cumulative Stage 2 run, not raw acquisition .npz")
    f = meta.get("files", {})
    dis = np.load(folder / f.get("displacement", "displacement.npy"), mmap_mode="r", allow_pickle=False)
    coh = np.load(folder / f.get("coherence", "coherence.npy"), mmap_mode="r", allow_pickle=False)
    if dis.ndim != 3 or dis.shape != coh.shape or list(dis.shape) != meta.get("array_shape"):
        raise ValueError("Stage 2 array dimensions mismatch")
    dates = meta.get("dates", [])
    if len(dates) != dis.shape[0]:
        raise ValueError("Stage 2 acquisition dates do not match array length")
    days = np.array([date.fromisoformat(t).toordinal() for t in dates], float)
    if np.any(np.diff(days) <= 0):
        raise ValueError("Acquisition dates must be strictly increasing")
    grid = meta.get("grid", {})
    vals = grid.get("transform_affine")
    crs = grid.get("crs", "")
    if not isinstance(vals, list) or len(vals) != 6 or not crs.startswith("EPSG:"):
        raise ValueError("Stage 2 grid must include projected affine transform and EPSG")
    transform = Affine(*vals)
    if (grid.get("width") != dis.shape[2] or grid.get("height") != dis.shape[1]
            or not (32601 <= int(crs[5:]) <= 32760)):
        raise ValueError("Expected valid UTM metre grid")
    replaced = np.zeros(dis.shape, np.bool_)
    rf = f.get("temporal_replaced_mask", "temporal_replaced_mask.npy")
    if cfg.exclude_temporal_replacements and (folder / rf).is_file():
        replaced = np.asarray(np.load(folder / rf, mmap_mode="r", allow_pickle=False), dtype=bool)
        if replaced.shape != dis.shape:
            raise ValueError("Temporal replacement mask dimensions mismatch")
        LOG.info("Excluding %d temporally imputed pixel-dates from trend evidence", int(replaced.sum()))
    return dis, coh, replaced, meta, days, transform, crs


def robust_line(t_year: np.ndarray, y: np.ndarray):
    """Theil–Sen line plus approximate residual-derived slope uncertainty.

    Returns slope, intercept, sigma_slope (units y/year), residual MAD and n.
    Sigma is a diagnostic approximation (not an independent-pixel confidence CI).
    """
    t = np.asarray(t_year, float)
    y = np.asarray(y, float)
    good = np.isfinite(t) & np.isfinite(y)
    t, y = t[good], y[good]
    n = len(y)
    if n < 2:
        return np.nan, np.nan, np.nan, np.nan, n
    slopes = [(y[j] - y[i]) / (t[j] - t[i]) for i in range(n) for j in range(i+1,n) if t[j] > t[i]]
    slope = float(np.median(slopes))
    intercept = float(np.median(y - slope * t))
    resid = y - (intercept + slope*t)
    mad = float(1.4826*np.median(np.abs(resid - np.median(resid))))
    sigma = float(mad / max(np.sqrt(n)*np.std(t, ddof=0), 1e-8))
    return slope, intercept, sigma, mad, n


def pixelwise_trend(dis: np.ndarray, valid: np.ndarray, days: np.ndarray, cfg: DetectionConfig,
                    block: int = 16000):
    """Theil–Sen median-of-pair-slopes; chunked to cap peak memory consumption."""
    n, h, w = dis.shape
    t = (days - days[0]) / YEAR_DAYS
    y = dis.reshape(n, -1)
    ok = valid.reshape(n, -1)
    slopes = np.full(h*w, np.nan, np.float32)
    support = np.zeros(h*w, np.uint8)
    date_span = np.zeros(h*w, np.float32)
    for start in range(0, h*w, block):
        stop = min(start+block, h*w)
        obs = np.asarray(y[:, start:stop], dtype=np.float32)
        observed = ok[:, start:stop]
        counts = observed.sum(axis=0)
        support[start:stop] = counts.astype(np.uint8)
        time_first = np.where(observed, days[:, None], np.inf).min(axis=0)
        time_last = np.where(observed, days[:, None], -np.inf).max(axis=0)
        span = np.where(np.isfinite(time_first) & np.isfinite(time_last), time_last-time_first, 0.)
        date_span[start:stop] = span
        rates = []
        for i in range(n-1):
            for j in range(i+1,n):
                r = (obs[j]-obs[i]) / (t[j]-t[i])
                rates.append(np.where(observed[i] & observed[j], r, np.nan))
        with np.errstate(all='ignore'):
            # np.nanmedian emits warnings when some pixels have no pairs.
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                v = np.nanmedian(np.stack(rates), axis=0)
        insufficient = (counts < cfg.min_history_dates) | (span < cfg.min_history_span_days)
        v[insufficient] = np.nan
        slopes[start:stop] = v.astype(np.float32)
    return slopes.reshape(h, w), support.reshape(h, w), date_span.reshape(h, w)


def smooth_valid(arr, good, sigma_y, sigma_x):
    weights = good.astype(float)
    numerator = ndimage.gaussian_filter(np.where(good, arr, 0.).astype(float),
                                        (sigma_y, sigma_x), mode="constant")
    denominator = ndimage.gaussian_filter(weights, (sigma_y, sigma_x), mode="constant")
    result = np.full(arr.shape, np.nan, float)
    np.divide(numerator, denominator, out=result, where=denominator > 1e-8)
    return result, denominator


def spatial_anomaly(arr, valid, steps, cfg, *, minimum, snr, floor):
    good = valid & np.isfinite(arr)
    if np.count_nonzero(good) < max(50, cfg.min_zone_pixels*5):
        return np.zeros(arr.shape, bool), np.full(arr.shape, np.nan), float('nan')
    sy, sx = steps
    near, sw = smooth_valid(arr, good, cfg.local_sigma_m/sy, cfg.local_sigma_m/sx)
    broad, bw = smooth_valid(arr, good, cfg.background_sigma_m/sy, cfg.background_sigma_m/sx)
    eligible = good & (sw>=cfg.min_filter_support) & (bw>=cfg.min_filter_support)
    residual = near - broad
    center = float(np.median(residual[eligible])) if np.any(eligible) else 0.
    noise = max(floor, 1.4826*float(np.median(np.abs(residual[eligible]-center)))) if np.any(eligible) else floor
    anomaly = residual-center
    threshold = max(minimum,snr*noise)
    selected = eligible & (anomaly < -threshold)
    anomaly[~eligible] = np.nan
    return selected, anomaly, threshold


def abrupt_step_screen(dis, valid, days, pixel_rate, cfg, steps):
    """A step requires two coherent observations both before and after its date.

    Operates on residuals after each pixel's robust linear fit; a single spike
    cannot create a persistent two-date step by itself.
    """
    n, h, w = dis.shape
    flag = np.zeros((n, h, w), bool)
    strongest = np.full((h,w), np.nan, np.float32)
    step_dates = np.zeros((h,w),np.int16) - 1
    if not cfg.detect_abrupt_steps or n < 5:
        return flag, strongest, step_dates
    # Keep the pixelwise linear component from triggering an 'abrupt' signal.
    t = (days-days[0])/YEAR_DAYS
    # A robust intercept, per pixel, from all valid observations.
    y = np.asarray(dis, dtype=np.float32)
    model_offset = np.where(valid, y-pixel_rate[None,:,:]*t[:,None,None], np.nan)
    with np.errstate(all='ignore'):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            intercept = np.nanmedian(model_offset, axis=0)
    detrended = y - (pixel_rate[None,:,:]*t[:,None,None] + intercept[None,:,:])
    good = valid & np.isfinite(pixel_rate)[None,:,:]
    for cut in range(2,n-1):
        if cut+1 >= n:
            continue
        # Two observations on each side, no imputation.
        both = np.all(good[cut-2:cut+2],axis=0)
        if np.count_nonzero(both) < 30:
            continue
        left = .5*(detrended[cut-2] + detrended[cut-1])
        right = .5*(detrended[cut] + detrended[cut+1])
        jump = right-left
        local_detect, spatial_residual, _ = spatial_anomaly(
            jump, both, steps, cfg, minimum=cfg.abrupt_min_step_mm,
            snr=cfg.abrupt_step_snr, floor=cfg.abrupt_noise_floor_mm)
        # Strong consecutive evidence: each post point must shift away vs the
        # median pre value, not merely one abnormal post observation.
        persisted = (detrended[cut] - left <= -cfg.abrupt_min_step_mm*.6)
        persisted &= (detrended[cut+1]-left <= -cfg.abrupt_min_step_mm*.6)
        # Reject a solitary spike near the boundary: both *pre* samples must
        # agree, as must both *post* samples. Otherwise a positive spike in
        # the last pre-date looks like a false negative step at the next cut.
        agreement = np.maximum(2*cfg.abrupt_noise_floor_mm, .5*np.abs(jump))
        persisted &= np.abs(detrended[cut-1]-detrended[cut-2]) <= agreement
        persisted &= np.abs(detrended[cut+1]-detrended[cut]) <= agreement
        active = local_detect & persisted & np.isfinite(spatial_residual)
        # Mark the confirmation date (second post-event observation), not all
        # future dates: subsequent dates are NOT independent step detections.
        flag[cut+1, active] = True
        better = active & (~np.isfinite(strongest) | (jump< strongest))
        strongest[better] = jump[better].astype(np.float32)
        step_dates[better] = cut
    return flag, strongest, step_dates


def classify_rate(subsidence_mm_year: float | None, accelerating: bool | None,
                  reliable: bool = True):
    if not reliable or subsidence_mm_year is None or not math.isfinite(subsidence_mm_year):
        return None
    rate = max(0., float(subsidence_mm_year))
    if rate > HIGH_ABOVE_MM_YEAR:
        return 'high'
    if rate >= ELEVATED_MIN_MM_YEAR and accelerating is True:
        return 'elevated'
    if rate >= WATCH_MIN_MM_YEAR:
        return 'watch'
    return 'low'


def zone_history(dis, valid, footprint, days, cfg):
    """One fixed spatial support set across all dates; do not fill missing dates."""
    n = len(days)
    counts = valid[:,footprint].sum(axis=0)
    common_min = max(cfg.min_zone_dates,int(math.ceil(cfg.min_zone_common_date_fraction*n)))
    consistent = counts >= common_min
    date_medians = np.full(n,np.nan)
    date_counts = np.zeros(n,int)
    if consistent.sum() < cfg.min_zone_coherent_pixels:
        return date_medians, date_counts, int(consistent.sum())
    for i in range(n):
        vals = np.asarray(dis[i, footprint])[consistent]
        good = np.asarray(valid[i, footprint])[consistent]
        date_counts[i] = int(good.sum())
        if date_counts[i] >= max(cfg.min_zone_coherent_pixels,
                                math.ceil(consistent.sum()*cfg.min_zone_date_fraction)):
            date_medians[i] = float(np.median(vals[good]))
    return date_medians, date_counts, int(consistent.sum())


def zone_metrics(y, days, cfg):
    """Full-history robust rate + recent-vs-prior robust slopes and uncertainties."""
    t = (days-days[0])/YEAR_DAYS
    finite = np.isfinite(y)
    if finite.sum() < cfg.min_zone_dates or np.ptp(days[finite]) < cfg.min_history_span_days:
        return {'trend_rate_mm_year':None,'trend_sigma_mm_year':None,'trend_mad_mm':None,
                'subsidence_mm_year':None, 'recent_rate_mm_year':None,
                'acceleration_mm_year2':None, 'accelerating':None, 'reliable':False}
    slope, _, sigma, mad, _ = robust_line(t, y)
    reliable = (np.isfinite(slope) and np.isfinite(sigma) and
                sigma <= max(cfg.max_trend_absolute_uncertainty_mm_year,
                             abs(slope)*cfg.max_trend_relative_uncertainty))
    recent_rate = accel = None
    accelerating = None
    if len(y)>=7 and np.all(finite[-4:]) and np.all(finite[-7:-3]):
        recent,_,recent_sigma,_,_ = robust_line(t[-4:], y[-4:])
        prior,_,prior_sigma,_,_ = robust_line(t[-7:-3],y[-7:-3])
        recent_rate = float(recent)
        change_in_away_rate = -recent+prior
        combined = math.hypot(recent_sigma,prior_sigma)
        accelerating = (recent < 0 and change_in_away_rate >= cfg.accel_min_rate_change_mm_year
                        and change_in_away_rate >= cfg.accel_significance_sigma*combined)
        delta_t = float(np.mean(t[-4:])-np.mean(t[-7:-3]))
        accel = float((recent-prior)/delta_t) if delta_t>0 else None
    return {'trend_rate_mm_year':float(slope), 'trend_sigma_mm_year':float(sigma),
            'trend_mad_mm':float(mad), 'subsidence_mm_year':float(max(0.,-slope)),
            'recent_rate_mm_year':recent_rate, 'acceleration_mm_year2':accel,
            'accelerating':accelerating,'reliable':bool(reliable)}


def geometry_from_mask(mask: np.ndarray, transform: Affine, crs: str):
    geo = []
    for geom, value in shapes(mask.astype(np.uint8), mask=mask, transform=transform):
        if value == 1:
            geo.append(transform_geom(crs, 'EPSG:4326', geom, precision=7))
    if not geo:
        return None
    if len(geo)==1:
        return geo[0]
    return {'type':'MultiPolygon','coordinates':[g['coordinates'] for g in geo]}


def main_process(folder: Path, output_root: Path, cfg: DetectionConfig):
    cfg.check()
    dis, coh, replaced, meta, days, transform, crs = load_stack(folder,cfg)
    n,h,w = dis.shape
    if n < 2:
        raise ValueError('At least two cumulative acquisition dates required')
    step_x = math.hypot(transform.a,transform.d)
    step_y = math.hypot(transform.b,transform.e)
    pixel_area = abs(transform.a*transform.e-transform.b*transform.d)
    if min(step_x,step_y,pixel_area)<=0:
        raise ValueError('Invalid projected map cell size')
    out = output_root / folder.name
    if out.resolve()==folder.resolve():
        raise ValueError('Stage 3 output must differ from Stage 2 input')
    out.mkdir(parents=True,exist_ok=True)
    valid = np.isfinite(dis) & np.isfinite(coh) & (coh >= cfg.coherence_min) & (coh<=1) & ~replaced
    LOG.info('Fitting full-history trends (%d dates; %d grid pixels)',n,h*w)
    trend, support, span = pixelwise_trend(dis, valid, days, cfg)
    np.save(out/'trend_velocity_mm_year.npy',trend)
    np.save(out/'trend_support_dates.npy',support)
    dt = np.diff(days)
    midpoint = (days[1:]+days[:-1])*.5
    v = np.full((n-1,h,w),np.nan,np.float32)
    for i in range(n-1):
        good = valid[i] & valid[i+1]
        v[i] = np.where(good, (dis[i+1]-dis[i])/dt[i], np.nan)
    a = np.full((max(0,n-2),h,w),np.nan,np.float32)
    for i in range(n-2):
        a[i] = (v[i+1]-v[i])/(midpoint[i+1]-midpoint[i])
    np.save(out/'velocity.npy',v)
    np.save(out/'acceleration.npy',a)
    trend_ok = np.isfinite(trend)
    trend_mask, residual_v, trend_threshold = spatial_anomaly(
        trend,trend_ok,(step_y,step_x),cfg,minimum=cfg.min_local_rate_mm_year,
        snr=cfg.local_rate_snr,floor=cfg.spatial_rate_noise_floor_mm_year)
    trend_mask &= trend <= -cfg.min_absolute_away_rate_mm_year
    LOG.info('Trend anomaly threshold %.2f mm/year; selected pixels %d',trend_threshold,int(trend_mask.sum()))
    step_flags, step_sizes, step_dates = abrupt_step_screen(dis,valid,days,trend,cfg,(step_y,step_x))
    any_steps = np.any(step_flags,axis=0)
    union = trend_mask | any_steps
    labelled,num = ndimage.label(union, structure=ndimage.generate_binary_structure(2,1))
    labels = np.zeros((h,w),np.int32)
    features=[]
    disp_info = meta.get('displacement',{})
    los_compatible = (disp_info.get('component')=='los' and disp_info.get('positive_los_direction')=='toward')
    sign_verified = disp_info.get('phase_sign_verified_for_provisional_products') is True
    for lid in range(1,num+1):
        component = labelled==lid
        if int(component.sum()) < cfg.min_zone_pixels:
            continue
        # The fixed polygon encompasses trend-supported and/or persistent-step pixels.
        y, date_counts, npix = zone_history(dis,valid,component,days,cfg)
        metrics = zone_metrics(y,days,cfg)
        strong_history = npix>=cfg.min_zone_coherent_pixels and np.isfinite(y).sum()>=cfg.min_zone_dates
        reliable = strong_history and metrics['reliable'] and los_compatible
        risk = classify_rate(metrics['subsidence_mm_year'],metrics['accelerating'],reliable)
        geometry = geometry_from_mask(component,transform,crs)
        if geometry is None:
            continue
        id_basis = json.dumps({'family':meta.get('acquisition_family',{}),
                               'geometry':geometry},sort_keys=True,separators=(',',':'))
        identifier = 'anomaly-' + hashlib.sha1(id_basis.encode()).hexdigest()[:16]
        labels[component] = len(features)+1
        trend_pixels = int(np.count_nonzero(component & trend_mask))
        step_pixels = int(np.count_nonzero(component & any_steps))
        step_in = np.where(component,np.nan_to_num(step_sizes,nan=np.inf),np.inf)
        step_index = np.unravel_index(np.argmin(step_in),step_in.shape)
        strongest = float(step_sizes[step_index]) if np.isfinite(step_in[step_index]) else None
        strongest_date = (meta['dates'][int(step_dates[step_index])] if strongest is not None else None)
        last_index = int(np.max(np.where(np.any(step_flags[:,component],axis=1))[0])) if step_pixels else None
        latest = (trend_pixels>0 or (last_index is not None and last_index==n-1))
        usable_date_idx = np.flatnonzero(np.isfinite(y))
        last_observed = meta['dates'][int(usable_date_idx[-1])] if len(usable_date_idx) else None
        first_observed = meta['dates'][int(usable_date_idx[0])] if len(usable_date_idx) else None
        mean_coh = np.asarray(coh[-1,component],float)
        mean_coh = float(np.nanmean(mean_coh)) if np.isfinite(mean_coh).any() else None
        # One fixed footprint, one median location statistic per acquisition date.
        med_list = [float(z) if np.isfinite(z) else None for z in y]
        first_idx = int(np.where(np.isfinite(y))[0][0]) if np.isfinite(y).any() else None
        latest_shift = float(y[-1]-y[first_idx]) if first_idx is not None and np.isfinite(y[-1]) else None
        props = {
            'id':identifier,'classification':'subsidence_candidate',
            'priority':('urgent_review' if risk=='high' else 'review' if risk=='elevated' else 'watch'),
            'risk_level':risk,'risk_method':RISK_METHOD,'risk_as_of':meta['dates'][-1],
            'review_required':True,'review_status':'unreviewed','sign_verified':sign_verified,
            'source_run':folder.name,'processed_at':datetime.now(timezone.utc).isoformat(),
            'evidence':('trend_and_persistent_step' if trend_pixels and step_pixels else
                        'full_history_velocity' if trend_pixels else 'persistent_step'),
            'first_observed':first_observed,'last_observed':last_observed,
            'detected_on_latest_date':bool(latest), 'observation_count':int(np.isfinite(y).sum()),
            'acquisition_pairs':n-1,'pixel_count':int(component.sum()),
            'area_m2':float(component.sum()*pixel_area),
            'full_history_los_velocity_mm_per_year':metrics['trend_rate_mm_year'],
            'full_history_subsidence_mm_per_year':metrics['subsidence_mm_year'],
            'trend_velocity_sigma_mm_per_year':metrics['trend_sigma_mm_year'],
            'trend_residual_mad_mm':metrics['trend_mad_mm'],
            'trend_reliable':bool(reliable),
            'recent_los_velocity_mm_per_year':metrics['recent_rate_mm_year'],
            'recent_acceleration_mm_per_year2':metrics['acceleration_mm_year2'],
            'accelerating_recently':metrics['accelerating'],
            'accelerating_last_3_intervals':metrics['accelerating'],
            'recent_3_interval_rates_mm_per_year':[],
            'zone_median_displacement_mm_by_date':med_list,
            'zone_valid_pixel_counts_by_date':date_counts.tolist(),
            'zone_fixed_footprint_support_pixels':npix,
            'temporal_interpolation_excluded':bool(cfg.exclude_temporal_replacements),
            'abrupt_step_mm':strongest,'abrupt_step_date':strongest_date,
            'trend_candidate_pixels':trend_pixels,'abrupt_candidate_pixels':step_pixels,
            'mean_los_displacement_mm':latest_shift,
            'mean_vertical_displacement_mm':None,
            'mean_vertical_rate_mm_per_day':None,
            'mean_rate_mm_day':(metrics['trend_rate_mm_year']/YEAR_DAYS
                                if metrics['trend_rate_mm_year'] is not None else None),
            'mean_acceleration_mm_day2':(metrics['acceleration_mm_year2']/YEAR_DAYS**2
                                         if metrics['acceleration_mm_year2'] is not None else None),
            'mean_coherence':mean_coh,
        }
        features.append({'type':'Feature','id':identifier,'geometry':geometry,'properties':props})
    # Preserve legacy viewer shape. Trend flags appear on the latest map; step
    # flags appear only at the second corroborating post-event observation.
    candidate=np.zeros((n,h,w),np.uint8)
    candidate[-1] = trend_mask.astype(np.uint8)
    candidate |= step_flags.astype(np.uint8)
    np.save(out/'candidate_mask.npy',candidate)
    np.save(out/'zone_labels.npy',labels)
    output_geo={'type':'FeatureCollection','features':features,
                'meta':{'stage':'full_history_los_trend_and_persistent_step_screen',
                        'source_run':folder.name,'total':len(features),
                        'official_hazard_assessment':False,
                        'risk_method':RISK_METHOD}}
    (out/'anomalies.geojson').write_text(json.dumps(output_geo,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    intervals = [{'interval_index':i+1,'start':meta['dates'][i],'end':meta['dates'][i+1],
                  'duration_days':float(dt[i]),
                  'regions':int(ndimage.label(candidate[i+1])[1])}
                 for i in range(n-1)]
    output_meta = {
        'stage':'subsidence_detection','schema_version':3,'source_processing_directory':str(folder.resolve()),
        'source_run':folder.name,'dates':meta['dates'],
        'files':{'velocity':'velocity.npy','acceleration':'acceleration.npy',
                 'candidate_mask':'candidate_mask.npy','polygons':'anomalies.geojson',
                 'trend_velocity':'trend_velocity_mm_year.npy','trend_support':'trend_support_dates.npy',
                 'zone_labels':'zone_labels.npy'},
        'velocity_units':'mm/day','acceleration_units':'mm/day^2',
        'velocity_shape':[n-1,h,w],'acceleration_shape':[max(n-2,0),h,w],
        'candidate_mask_shape':[n,h,w],'detection_config':asdict(cfg),
        'n_tracked_anomalies':len(features),'intervals':intervals,
        'risk_method':RISK_METHOD,
        'risk_scheme':{'low':'<10 mm/year','watch':'10–40, or 40–100 without evidence of acceleration',
                       'elevated':'40–100 with supported recent acceleration','high':'>100',
                       'basis':'Full-history fixed-footprint LOS-away robust rate; recent acceleration separate',
                       'source':'user-defined exploratory bins, not validated hazard categories'},
        'warnings':[
            'Relative LOS-away motion is not vertical subsidence and the provisional phase sign requires verification.',
            'Temporal-filtered samples are excluded from independent trend and step evidence when a replacement mask exists.',
            'Theil–Sen median slope can conceal abrupt events; persistent-step screening is independent.',
            'Temporal residual sigma is an approximate diagnostic, not a calibrated uncertainty or probability.',
            'Risk null indicates unsupported evidence; absence of polygons does not imply low risk.',
            'Strong broad-scale deformation can be suppressed by spatial-background subtraction.'
        ]}
    (out/'metadata.json').write_text(json.dumps(output_meta,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    LOG.info('Wrote %d zone(s) to %s',len(features),out)
    return output_meta


def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    cfg=DetectionConfig()
    folder=select_processing_run(PROCESSED)
    LOG.info('Using Stage 2 run: %s',folder)
    main_process(folder,OUTPUT_ROOT,cfg)
    print('Stage 3 complete:',OUTPUT_ROOT/folder.name)
    print('Export API using: python src/zone_service.py --run',folder.name,
          '--data-root data --out data/api/zones.json')


if __name__=='__main__':
    main()
