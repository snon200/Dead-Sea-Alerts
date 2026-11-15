"""NISAR GUNW v2: raw aligned pairs -> corrected cumulative LOS displacement.

Run from the repository root (no CLI):
    python src/nisar_processing_v2.py

Input: any aligned acquisition-v2 .npz, including dead_sea_cropped_dem.npz.
Output: data/processed/run_v2_.../{displacement.npy,coherence.npy,metadata.json,...}

Each output displacement[k] is displacement since the FIRST acquisition date,
not since the preceding one. Units: mm, relative to the chosen reference area.
Optional gated temporal spike filtering changes only isolated interior-date
outliers; the original result is also saved to displacement_unfiltered.npy.

Dependencies: numpy, rasterio, matplotlib (matplotlib for diagnostics only).
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re

import numpy as np
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.warp import transform_geom

# ============================ EDIT HERE ===============================
INPUT = Path("data/raw/dead_sea_cropped_dem.npz")  # Change to your current processed-ready .npz
OUTPUT_ROOT = Path("data/processed")

# WGS84 longitude/latitude bounding box of a putatively stable LAND patch.
# This is an UNVERIFIED candidate: inspect and replace for your experiment.
REF_BBOX = (35.3770, 31.4490, 35.3880, 31.4555)  # W, S, E, N
COHERENCE_MIN = 0.35
MIN_REFERENCE_PIXELS = 8

# NASA's unwrapped phase and screens are in radians. Subtract screens before
# converting phase to LOS. Set a switch False for comparison experiments.
APPLY_IONOSPHERE = True
APPLY_WET_TROPO = False
APPLY_DRY_TROPO = False
APPLY_SOLID_EARTH_TIDES = False

# Remove a robust, long-wavelength (constant + x + y) slope per interferogram.
APPLY_PLANAR_RAMP = True
RAMP_COHERENCE_MIN = 0.65
RAMP_MIN_PIXELS = 40
RAMP_EXCLUDE_BBOXES = [
    # (35.397, 31.455, 35.410, 31.465),  # example ONLY, if known unstable
]
RAMP_FIT_BBOX = None  # None = entire polygon; otherwise (W,S,E,N)

# NISAR phase sign is provisionally assumed to be - for positive LOS toward
# satellite. MUST verify against a known displacement before hazard use.
PHASE_SIGN = -1
# If the mode cannot be identified, set a verified frequency in MHz here.
CENTER_FREQUENCY_MHZ = None

MAKE_DIAGNOSTIC_FIGURE = True
SHOW_FIGURE = False
DISPLAY_LIMIT_MM = 60.0

# Optional temporal *spike* removal AFTER cumulative inversion.
# Uses a 3-acquisition median as a candidate detector, not a blanket median
# filter. Replaces ONLY isolated interior outliers with an estimate interpolated
# between the adjacent actual acquisition dates (handles irregular date gaps).
# This can suppress genuine short-lived motion. Always inspect the original.
APPLY_TEMPORAL_SPIKE_FILTER = True
TEMPORAL_SPIKE_THRESHOLD_MM = 15.0
TEMPORAL_NEIGHBOR_AGREEMENT_MM = 12.0
# Empty: inspect every interior date. Otherwise only these suspicious dates.
TEMPORAL_FILTER_ONLY_DATES = []  # e.g. ["2026-07-15"]
# ======================================================================

C_LIGHT = 299_792_458.0
FREQUENCY_A_MHZ = {"05": 1221.5, "20": 1229.0, "40": 1239.0, "77": 1257.5}
CORRECTIONS = {
    "ionosphere": APPLY_IONOSPHERE,
    "tropo_wet": APPLY_WET_TROPO,
    "tropo_dry": APPLY_DRY_TROPO,
    "solid_earth_tides": APPLY_SOLID_EARTH_TIDES,
}


def rectangle_mask(bbox, shape, transform, crs):
    """Select UTM cell centers inside a WGS84 rectangle."""
    w, s, e, n = map(float, bbox)
    if not (w < e and s < n):
        raise ValueError(f"Invalid W,S,E,N bounding box: {bbox}")
    geom = {"type": "Polygon", "coordinates": [[
        [w, s], [e, s], [e, n], [w, n], [w, s]]]}
    projected = transform_geom("EPSG:4326", crs, geom)
    return geometry_mask([projected], out_shape=shape, transform=transform, invert=True)


def frequency_from_scenes(meta):
    """Determine frequency A from GUNW scene mode; prevent mixed frequencies."""
    scenes = meta.get("scenes", [])
    modes = {str(s.get("mode", "")) for s in scenes}
    if len(modes) != 1:
        raise ValueError(f"Expected one mode across scenes; got {modes}")
    mode = next(iter(modes))
    if not re.fullmatch(r"[0-9]{4}", mode):
        raise ValueError(f"Unknown NISAR mode {mode!r}")
    inferred = FREQUENCY_A_MHZ.get(mode[:2])
    if CENTER_FREQUENCY_MHZ is None:
        if inferred is None:
            raise ValueError(f"Unknown frequency-A mode {mode}; set CENTER_FREQUENCY_MHZ")
        return inferred * 1e6, "granule_mode", mode
    mhz = float(CENTER_FREQUENCY_MHZ)
    if not 1000 < mhz < 1500:
        raise ValueError("Expected NISAR frequency A near L band (MHz)")
    if inferred is not None and abs(mhz - inferred) > 0.5:
        raise ValueError(f"Manual {mhz} MHz disagrees with {mode} -> {inferred} MHz")
    return mhz * 1e6, "manual_verified", mode


def robust_ramp(displacement_mm, coherence, valid, fit_roi, x, y, ref_mask):
    """IRLS plane fitted to coherent background; remove its x/y slopes only."""
    fit = valid & fit_roi & np.isfinite(displacement_mm) & (coherence >= RAMP_COHERENCE_MIN)
    if int(fit.sum()) < RAMP_MIN_PIXELS:
        raise ValueError(f"Only {fit.sum()} ramp-fit pixels; select more stable land or lower RAMP_COHERENCE_MIN")
    xx, yy = np.meshgrid(x, y)
    center_x, center_y = float(np.mean(x)), float(np.mean(y))
    xkm, ykm = (xx - center_x)/1000.0, (yy - center_y)/1000.0
    A = np.column_stack((np.ones(fit.sum()), xkm[fit], ykm[fit]))
    values = displacement_mm[fit].astype(float)
    base_w = np.clip(coherence[fit].astype(float), 0.05, 1.0)**2
    w = base_w.copy()
    for _ in range(10):
        sw = np.sqrt(w)
        coef = np.linalg.lstsq(A * sw[:, None], values * sw, rcond=None)[0]
        residual = values - A @ coef
        sigma = max(1.4826 * np.median(np.abs(residual - np.median(residual))), 0.5)
        limit = 1.5*sigma
        next_w = base_w * np.minimum(1.0, limit/np.maximum(abs(residual), 1e-12))
        if np.allclose(next_w, w, rtol=1e-3, atol=1e-4):
            break
        w = next_w
    sw = np.sqrt(w)
    coef = np.linalg.lstsq(A * sw[:, None], values * sw, rcond=None)[0]
    # Only subtract the slope; a constant absolute offset is not observable
    # and will be separately removed using the stable reference patch.
    slope = coef[1]*xkm + coef[2]*ykm
    out = displacement_mm - slope
    if int((valid & ref_mask).sum()) < MIN_REFERENCE_PIXELS:
        raise ValueError("Insufficient coherent reference pixels after ramp correction")
    out -= np.median(out[valid & ref_mask])
    out[~valid] = np.nan
    return out, {
        "east_slope_mm_per_km": float(coef[1]),
        "north_slope_mm_per_km": float(coef[2]),
        "fit_pixels": int(fit.sum()),
        "fit_rms_before_mm": float(np.sqrt(np.mean((values - np.average(values, weights=base_w))**2))),
        "fit_rms_after_mm": float(np.sqrt(np.mean((values - A@coef)**2))),
    }


def invert_network(pair_mm, pair_coh, ref_times, sec_times, block=1024):
    """Coherence-weighted LS on acquisition-date graph; NaN where disconnected.

    Pair p measures D[secondary] - D[reference]. D[first date] = 0.
    Works for a chain or an overlapping network. Missing pixels do not become
    fictitious observations; only dates connected to date 0 receive values.
    """
    dates = sorted({s[:10] for s in ref_times} | {s[:10] for s in sec_times})
    if not (2 <= len(dates) <= 60):
        raise ValueError("Expected 2..60 unique acquisition dates")
    which = {d: i for i, d in enumerate(dates)}
    edges = [(which[a[:10]], which[b[:10]]) for a,b in zip(ref_times, sec_times)]
    n, p = len(dates), len(edges)
    if any(a >= b for a,b in edges):
        raise ValueError("Acquisition pair has reversed/non-positive date interval")
    if pair_mm.shape != pair_coh.shape or pair_mm.ndim != 3 or pair_mm.shape[0] != p:
        raise ValueError("Interferogram/quality/time dimensions disagree")
    connected = {0}
    for _ in range(n):
        for a,b in edges:
            if a in connected or b in connected:
                connected.update((a,b))
    if len(connected) != n:
        raise ValueError("Acquisition network is disconnected at the dataset level")

    A = np.zeros((p, n-1), dtype=float)
    for k,(a,b) in enumerate(edges):
        if a:
            A[k,a-1] = -1.0
        if b:
            A[k,b-1] = 1.0
    _, h, w = pair_mm.shape
    disp = np.full((n,h,w), np.nan, dtype=np.float32)
    quality = np.full((n,h,w), np.nan, dtype=np.float32)
    pd = pair_mm.reshape(p, -1)
    pq = pair_coh.reshape(p, -1)
    od, oq = disp.reshape(n,-1), quality.reshape(n,-1)
    for j in range(0, h*w, block):
        k = min(j+block, h*w)
        d = np.asarray(pd[:,j:k], float)
        c = np.asarray(pq[:,j:k], float)
        usable = np.isfinite(d) & np.isfinite(c) & (c >= COHERENCE_MIN) & (c <= 1)
        m = k-j
        reachable = np.zeros((n,m), dtype=bool)
        reachable[0] = True
        for _ in range(n):
            previous = reachable.copy()
            for row,(a,b) in enumerate(edges):
                both = usable[row] & (reachable[a] | reachable[b])
                reachable[a] |= both
                reachable[b] |= both
            if np.array_equal(reachable, previous):
                break
        for row,(a,b) in enumerate(edges):
            usable[row] &= reachable[a] & reachable[b]
        weight = np.where(usable, np.maximum(c,0)**2, 0.)
        observed = np.where(usable,d,0.)
        # Separate weighted normal equation for each ground pixel.
        H = np.einsum("pi,pj,pk->kij",A,A,weight,optimize=True)
        b = np.einsum("pi,pk->ki",A,weight*observed,optimize=True)
        absent = ~reachable[1:].T
        idx = np.arange(n-1)
        H[:,idx,idx] += absent.astype(float) + 1e-9
        sol = np.linalg.solve(H,b[...,None])[...,0].T
        od[0,j:k] = np.where(reachable[1:].any(axis=0),0.0,np.nan)
        for t in range(1,n):
            od[t,j:k] = np.where(reachable[t],sol[t-1],np.nan)
        qsum = np.zeros((n,m),float)
        count = np.zeros((n,m),float)
        for row,(a,b) in enumerate(edges):
            v = np.where(usable[row],c[row],0.)
            vcount = usable[row].astype(float)
            qsum[a] += v; qsum[b] += v
            count[a] += vcount; count[b] += vcount
        qmean = np.divide(qsum,count,out=np.zeros_like(qsum),where=count>0)
        oq[:,j:k] = np.where(reachable & (count>0),qmean,np.nan)
    return disp,quality,dates,edges


def temporal_spike_filter(cumulative, dates, *, enabled=True,
                          spike_threshold_mm=15.0,
                          neighbor_agreement_mm=12.0,
                          only_dates=()):
    """Conservative, 3-date temporal median spike screen.

    A pixel at interior date t is a candidate when its value deviates from
    median(D[t-1], D[t], D[t+1]) by >= spike_threshold_mm AND the neighboring
    dates agree within neighbor_agreement_mm. The replacement is a time-weighted
    interpolation between the *original*, real neighboring observations.

    No missing observations are filled. t=0 and t=last are never modified.
    All tests use the unmodified input, so changes never cascade in time.
    Caution: a genuinely transient event with recovery is indistinguishable
    from a measurement spike using only three observations.
    """
    stack = np.asarray(cumulative)
    if stack.ndim != 3 or len(dates) != stack.shape[0]:
        raise ValueError("Temporal filter expects cumulative (dates, rows, cols)")
    if spike_threshold_mm <= 0 or neighbor_agreement_mm < 0:
        raise ValueError("Temporal filter thresholds must be nonnegative (spike > 0)")
    ordinals = np.array([date.fromisoformat(str(d)[:10]).toordinal() for d in dates],dtype=float)
    if np.any(np.diff(ordinals) <= 0):
        raise ValueError("Temporal filter dates must be strictly increasing")
    date_whitelist = set(only_dates)
    unknown = sorted(date_whitelist.difference(dates))
    if unknown:
        raise ValueError(f"TEMPORAL_FILTER_ONLY_DATES absent from stack: {unknown}")

    filtered = stack.copy()
    modified = np.zeros(stack.shape, dtype=bool)
    report = []
    for t in range(1,stack.shape[0]-1):
        if date_whitelist and dates[t] not in date_whitelist:
            continue
        prev, center, nxt = stack[t-1],stack[t],stack[t+1]
        eligible = np.isfinite(prev) & np.isfinite(center) & np.isfinite(nxt)
        median3 = np.median(np.stack((prev,center,nxt),axis=0),axis=0)
        outlier = (eligible & (np.abs(center-median3) >= spike_threshold_mm)
                   & (np.abs(nxt-prev) <= neighbor_agreement_mm))
        if enabled and np.any(outlier):
            fraction = (ordinals[t]-ordinals[t-1])/(ordinals[t+1]-ordinals[t-1])
            estimate = prev + fraction*(nxt-prev)
            filtered[t][outlier] = estimate[outlier]
            modified[t] = outlier
        report.append({
            "date": str(dates[t]),
            "eligible_pixels": int(np.count_nonzero(eligible)),
            "candidate_pixels": int(np.count_nonzero(outlier)),
            "replaced_pixels": int(np.count_nonzero(modified[t])),
            "replaced_pct_of_eligible": float(100*np.count_nonzero(modified[t])/
                                               max(1,np.count_nonzero(eligible))),
            "median_abs_change_mm": (float(np.median(np.abs(
                (filtered[t]-center)[modified[t]]))) if np.any(modified[t]) else 0.0),
        })
    return filtered, modified, report


def quicklook(raw, atmo, final, x, y, dates, destination):
    """Four-panel geographic check; UTM coordinate axes are in correct order."""
    import matplotlib.pyplot as plt
    images = [raw[-1], atmo[-1], final[-1], final[-1]-atmo[-1]]
    headings = ["No corrections", "Iono + wet/dry + tide", "+ planar ramp", "Effect of ramp"]
    pixel_x = abs(float(np.median(np.diff(x))))
    pixel_y = abs(float(np.median(np.diff(y))))
    extent = [(x[0]-pixel_x/2)/1000,(x[-1]+pixel_x/2)/1000,
              (y[-1]-pixel_y/2)/1000,(y[0]+pixel_y/2)/1000]
    fig, axes = plt.subplots(2,2,figsize=(12,9),layout="constrained")
    for ax, image, heading in zip(axes.flat,images,headings):
        im = ax.imshow(image,extent=extent,origin="upper",cmap="RdBu_r",
                       vmin=-DISPLAY_LIMIT_MM,vmax=DISPLAY_LIMIT_MM)
        ax.set_title(heading)
        ax.set_xlabel("UTM easting [km]")
        ax.set_ylabel("UTM northing [km]")
        ax.set_aspect("equal")
        fig.colorbar(im,ax=ax,shrink=.75,label="Relative LOS [mm]")
    fig.suptitle(f"NISAR cumulative LOS, {dates[0]} → {dates[-1]}")
    fig.savefig(destination,dpi=160)
    if SHOW_FIGURE:
        plt.show()
    plt.close(fig)


def select_unique_pairs(phase, coh, comps, terms, reference_times, secondary_times, ids, polygon_mask):
    """Select one usable observation per acquisition interval from duplicated caches.

    Different Harmony requests can cache the same interferogram in multiple
    manifest folders. Prefer the copy with the largest number of usable pixels
    inside the polygon, including whichever corrections are enabled.
    """
    winners = {}
    scores = {}
    for i, (reference, secondary) in enumerate(zip(reference_times, secondary_times)):
        key = (reference[:10], secondary[:10])
        valid = (polygon_mask & np.isfinite(phase[i]) & np.isfinite(coh[i]) &
                 (coh[i] >= COHERENCE_MIN) & (coh[i] <= 1) &
                 np.isfinite(comps[i]) & (comps[i] > 0) & (comps[i] < 65535))
        for layer, enabled in CORRECTIONS.items():
            if enabled:
                valid &= np.isfinite(terms[layer][i])
        count = int(np.count_nonzero(valid))
        # Use mean coherence only to break coverage ties.
        quality = float(np.mean(coh[i][valid])) if count else -1.0
        score = (count, quality)
        if key not in winners or score > scores[key]:
            winners[key], scores[key] = i, score
    keep = sorted(winners.values(), key=lambda i: (reference_times[i], secondary_times[i]))
    return keep


def process():
    if PHASE_SIGN not in (-1,1):
        raise ValueError("PHASE_SIGN must be +1 or -1")
    if not 0 <= COHERENCE_MIN <= RAMP_COHERENCE_MIN <= 1:
        raise ValueError("Invalid coherence threshold")
    if not INPUT.is_file():
        raise FileNotFoundError(f"Acquisition v2 data missing: {INPUT}")
    with np.load(INPUT, allow_pickle=False) as f:
        required = ["phase","coherence","ionosphere","tropo_wet","tropo_dry",
                    "solid_earth_tides","connected_components","elevation","polygon_mask",
                    "reference_times","secondary_times","granule_ids","x","y","metadata_json"]
        if any(name not in f for name in required):
            raise ValueError(f"Missing acquisition-v2 keys: {[key for key in required if key not in f]}")
        phase = np.asarray(f["phase"],dtype=np.float64)
        coh = np.asarray(f["coherence"],dtype=np.float64)
        comps = np.asarray(f["connected_components"])
        terms = {key: np.asarray(f[key],dtype=np.float64) for key in CORRECTIONS}
        mask = np.asarray(f["polygon_mask"],dtype=bool)
        elevation = np.asarray(f["elevation"],dtype=np.float64)
        x, y = np.asarray(f["x"],float),np.asarray(f["y"],float)
        reference_times = list(map(str,f["reference_times"]))
        secondary_times = list(map(str,f["secondary_times"]))
        ids = list(map(str,f["granule_ids"]))
        source_meta = json.loads(str(f["metadata_json"].item()))
    original_pair_count = len(reference_times)
    keep = select_unique_pairs(phase, coh, comps, terms, reference_times,
                               secondary_times, ids, mask)
    if len(keep) != original_pair_count:
        print(f"Using {len(keep)} unique interferogram intervals; "
              f"skipping {original_pair_count - len(keep)} duplicate cached pair(s)")
        phase = phase[keep]
        coh = coh[keep]
        comps = comps[keep]
        terms = {name: arr[keep] for name, arr in terms.items()}
        reference_times = [reference_times[i] for i in keep]
        secondary_times = [secondary_times[i] for i in keep]
        ids = [ids[i] for i in keep]
        if len(source_meta.get("scenes", [])) == original_pair_count:
            source_meta["scenes"] = [source_meta["scenes"][i] for i in keep]
    if phase.ndim != 3 or phase.shape != coh.shape or comps.shape != phase.shape:
        raise ValueError("Expected aligned pair arrays of shape (pairs,rows,cols)")
    p,h,w = phase.shape
    if (any(z.shape != phase.shape for z in terms.values()) or mask.shape != (h,w)
            or elevation.shape != (h,w) or x.shape != (w,) or y.shape != (h,)
            or any(len(v) != p for v in (reference_times,secondary_times,ids))):
        raise ValueError("Source array shapes/timestamps are inconsistent")
    if np.any(np.diff(x)<=0) or np.any(np.diff(y)>=0):
        raise ValueError("x must increase and y must decrease (north-up UTM grid)")
    grid = source_meta.get("grid",{})
    epsg = int(grid.get("epsg",0))
    affine = grid.get("affine")
    if epsg<=0 or not isinstance(affine,list) or len(affine)!=6:
        raise ValueError("Missing source EPSG and affine transform")
    tf = Affine(*map(float,affine))
    crs = f"EPSG:{epsg}"
    if (not np.allclose(x,tf.c + tf.a*(np.arange(w)+.5))
            or not np.allclose(y,tf.f + tf.e*(np.arange(h)+.5))):
        raise ValueError("x/y grid coordinates inconsistent with input affine transform")
    ref = rectangle_mask(REF_BBOX,(h,w),tf,crs) & mask
    if not ref.any():
        raise ValueError("REF_BBOX misses polygon or contains no cell centers")
    fit_roi = mask.copy()
    if RAMP_FIT_BBOX is not None:
        fit_roi &= rectangle_mask(RAMP_FIT_BBOX,(h,w),tf,crs)
    for bbox in RAMP_EXCLUDE_BBOXES:
        fit_roi &= ~rectangle_mask(bbox,(h,w),tf,crs)

    frequency,frequency_source,mode = frequency_from_scenes(source_meta)
    mm_per_rad = PHASE_SIGN * (C_LIGHT/frequency)*1000/(4*np.pi)
    print(f"NISAR frequency A: {frequency/1e6:g} MHz ({frequency_source}); {p} pairs, {h}x{w} pixels")
    print(f"Reference candidate: {REF_BBOX} (NOT independently validated)")
    print("Correction switches:", CORRECTIONS, "ramp:", APPLY_PLANAR_RAMP)
    raw_pairs = np.full_like(phase,np.nan,dtype=np.float32)
    atmo_pairs = np.full_like(phase,np.nan,dtype=np.float32)
    final_pairs = np.full_like(phase,np.nan,dtype=np.float32)
    pair_coherence = np.where(mask[None,:,:] & np.isfinite(coh) & (coh>=0) & (coh<=1),coh,np.nan).astype(np.float32)
    records=[]

    for i in range(p):
        if date.fromisoformat(reference_times[i][:10]) >= date.fromisoformat(secondary_times[i][:10]):
            raise ValueError(f"Pair {i} has invalid date ordering")
        good = (mask & np.isfinite(phase[i]) & np.isfinite(coh[i]) &
                (coh[i]>=COHERENCE_MIN) & (coh[i]<=1) &
                np.isfinite(comps[i]) & (comps[i]>0) & (comps[i]<65535))
        for key,enabled in CORRECTIONS.items():
            if enabled:
                good &= np.isfinite(terms[key][i])
        labels,counts=np.unique(comps[i][good & ref],return_counts=True)
        if not len(labels):
            raise ValueError(f"Pair {i+1}: no high-coherence reference pixels. Check REF_BBOX.")
        label=int(labels[np.argmax(counts)])
        ref_good=good & ref & (comps[i]==label)
        if int(ref_good.sum())<MIN_REFERENCE_PIXELS:
            raise ValueError(f"Pair {i+1}: reference connected component has only {ref_good.sum()} good pixels")
        good &= comps[i]==label
        # Screen values are supplied in radians; do not scale before subtraction.
        phi_corr=phase[i].copy()
        for key,enabled in CORRECTIONS.items():
            if enabled:
                phi_corr-=terms[key][i]
        # Reference each pair once on the SAME valid connected component.
        raw = mm_per_rad*(phase[i]-np.median(phase[i][ref_good]))
        atmos = mm_per_rad*(phi_corr-np.median(phi_corr[ref_good]))
        raw[~good]=np.nan
        atmos[~good]=np.nan
        if APPLY_PLANAR_RAMP:
            final,stats=robust_ramp(atmos,coh[i],good,fit_roi,x,y,ref)
        else:
            final=atmos.copy()
            stats={"east_slope_mm_per_km":0.,"north_slope_mm_per_km":0.,
                   "fit_pixels":0,"fit_rms_before_mm":None,"fit_rms_after_mm":None}
        raw_pairs[i]=raw
        atmo_pairs[i]=atmos
        final_pairs[i]=final
        stats.update({
            "pair_index":i,"granule_id":ids[i],"reference_time_utc":reference_times[i],
            "secondary_time_utc":secondary_times[i],
            "baseline_days":(datetime.fromisoformat(secondary_times[i].replace("Z","+00:00"))-
                             datetime.fromisoformat(reference_times[i].replace("Z","+00:00"))).total_seconds()/86400,
            "reference_component":label,"reference_valid_pixels":int(ref_good.sum()),
            "valid_displacement_pixels":int(good.sum()),
            "center_frequency_hz":frequency,"frequency_mode":mode,
            "corrections_applied":[key for key,enabled in CORRECTIONS.items() if enabled],
        })
        records.append(stats)
        print(f"Pair {i+1:2}/{p} {reference_times[i][:10]} → {secondary_times[i][:10]}: "
              f"{good.sum()} pixels, ramp E={stats['east_slope_mm_per_km']:+.3f}, "
              f"N={stats['north_slope_mm_per_km']:+.3f} mm/km")

    cumulative,quality,dates,edges = invert_network(final_pairs,pair_coherence,reference_times,secondary_times)
    # Preserve untouched Stage 2 inversion: filtering happens AFTER the physical
    # corrections, reference, ramp removal and network inversion.
    original_cumulative = cumulative
    cumulative,temporal_replaced,temporal_report = temporal_spike_filter(
        original_cumulative, dates,
        enabled=APPLY_TEMPORAL_SPIKE_FILTER,
        spike_threshold_mm=TEMPORAL_SPIKE_THRESHOLD_MM,
        neighbor_agreement_mm=TEMPORAL_NEIGHBOR_AGREEMENT_MM,
        only_dates=TEMPORAL_FILTER_ONLY_DATES,
    )
    nchanged = int(temporal_replaced.sum())
    print(f"Temporal spike filter: {'enabled' if APPLY_TEMPORAL_SPIKE_FILTER else 'disabled'}; "
          f"replaced {nchanged} date-pixels")
    for row in temporal_report:
        if row['candidate_pixels']:
            print(f"  {row['date']}: {row['replaced_pixels']}/{row['eligible_pixels']} replaced "
                  f"({row['replaced_pct_of_eligible']:.2f}%)")
    # Keep scientific before/after comparison; diagnostic quicklook is spatial.
    if MAKE_DIAGNOSTIC_FIGURE:
        raw_cum,_,_,_=invert_network(raw_pairs,pair_coherence,reference_times,secondary_times)
        atmo_cum,_,_,_=invert_network(atmo_pairs,pair_coherence,reference_times,secondary_times)
    run_name = datetime.now(timezone.utc).strftime("run_v2_%Y%m%d_%H%M%S_%f")
    folder=OUTPUT_ROOT/run_name
    folder.mkdir(parents=True,exist_ok=False)
    np.save(folder/"displacement.npy",cumulative)
    np.save(folder/"displacement_unfiltered.npy",original_cumulative)
    np.save(folder/"temporal_replaced_mask.npy",temporal_replaced.astype(np.uint8))
    np.save(folder/"coherence.npy",quality)
    with (folder/"temporal_filter_report.csv").open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=["date","eligible_pixels","candidate_pixels",
                                              "replaced_pixels","replaced_pct_of_eligible",
                                              "median_abs_change_mm"])
        writer.writeheader()
        writer.writerows(temporal_report)
    metadata={
        "schema_version":2,"stage":"geophysical_processing_cumulative_stack",
        "created_utc":datetime.now(timezone.utc).isoformat(),
        "source_acquisition_npz":str(INPUT),
        "array_shape":list(cumulative.shape),
        "axis_order":["acquisition_date","row_north_to_south","column_west_to_east"],
        "files":{"displacement":"displacement.npy","coherence":"coherence.npy",
                 "displacement_unfiltered":"displacement_unfiltered.npy",
                 "temporal_replaced_mask":"temporal_replaced_mask.npy",
                 "temporal_filter_report":"temporal_filter_report.csv"},
        "dates":dates,"pairs":records,
        "grid":{"crs":crs,"transform_affine":list(map(float,affine)),
                "width":w,"height":h,"pixel_size_m":abs(float(tf.a)),
                "requested_bbox_wsen":grid.get("polygon_bbox_lonlat")},
        "displacement":{"unit":"mm","component":"los","reference_date":dates[0],
                        "reference_bbox_wsen":list(REF_BBOX),"phase_sign":PHASE_SIGN,
                        "positive_los_direction":"toward" if PHASE_SIGN==-1 else "unverified_or_away",
                        "phase_sign_verified_for_provisional_products":False,
                        "meaning":"Cumulative reference-relative LOS displacement since first acquisition date",
                        "coherence_minimum_for_displacement":COHERENCE_MIN},
        "coherence":{"unit":"dimensionless",
                     "meaning":"Mean supporting-pair coherence proxy, not a single-pair measurement; temporal filtering does not change coherence"},
        "temporal_filter":{
            "enabled":APPLY_TEMPORAL_SPIKE_FILTER,
            "method":"3-date median spike candidate with neighbor-consistency gate; replaced using date-weighted linear interpolation",
            "spike_threshold_mm":TEMPORAL_SPIKE_THRESHOLD_MM,
            "neighbor_agreement_mm":TEMPORAL_NEIGHBOR_AGREEMENT_MM,
            "only_dates":list(TEMPORAL_FILTER_ONLY_DATES),
            "replaced_total_date_pixels":nchanged,
            "per_date":temporal_report,
            "changes_are_estimates_not_independent_measurements":True,
            "endpoint_dates_unchanged":True,
            "preserves_nan_gaps":True,
        },
        "acquisition_family":{key:source_meta["scenes"][0].get(key)
                              for key in ("track","frame","direction","mode","release","crid")},
        "inversion":{"method":"Per-pixel coherence-weighted least squares, first date fixed at zero",
                     "edge_indices":[list(pair) for pair in edges],
                     "source_pair_count":original_pair_count,
                     "unique_pair_count":p,
                     "duplicate_cached_pairs_removed":original_pair_count-p,
                     "warning":"Without redundant pairs, an erroneous pair propagates to later dates"},
        "correction_policy":{"ionosphere":APPLY_IONOSPHERE,
                             "wet_troposphere":APPLY_WET_TROPO,
                             "hydrostatic_troposphere":APPLY_DRY_TROPO,
                             "solid_earth_tides":APPLY_SOLID_EARTH_TIDES,
                             "planar_ramp":APPLY_PLANAR_RAMP,
                             "troposphere_height_model":(
                                 "Per-pixel DEM ellipsoidal heights" if source_meta.get("method", {}).get("elevation_available", False)
                                 else "No DEM: cached atmospheric layers may use a constant height; see source manifests"),
                             "ramp_fit_coherence_min":RAMP_COHERENCE_MIN,
                             "ramp_exclusions_wsen":[list(b) for b in RAMP_EXCLUDE_BBOXES]},
        "scientific_warnings":[
            "Reference stability and LOS phase sign have not been independently validated",
            "Phase screen corrections are assumed additive in raw phase and therefore subtracted",
            "A fitted planar ramp may remove genuine long-wavelength deformation",
            "No automatic unwrapping-error repair or formal uncertainty estimate",
            "Provisional products may change on reprocessing",
            "Fast local GeoTIFF imports may have atmospheric corrections evaluated at a single height, not terrain-following",
            "Temporal spike filtering may suppress genuine transient ground motion; review unfiltered maps and report before interpreting risks",
            "The temporal filter cannot repair a bad interferogram edge causing a persistent step, nor validate phase unwrapping",
            "Filtered displacement values are estimates; coherence is retained as observational support, not as validation of interpolated values",
        ],
    }
    (folder/"metadata.json").write_text(json.dumps(metadata,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    with (folder/"ramp_fits.csv").open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=["pair_index","reference_time_utc","secondary_time_utc",
                                             "east_slope_mm_per_km","north_slope_mm_per_km",
                                             "fit_pixels","fit_rms_before_mm","fit_rms_after_mm"])
        writer.writeheader()
        for rec in records:
            writer.writerow({key:rec[key] for key in writer.fieldnames})
    if MAKE_DIAGNOSTIC_FIGURE:
        quicklook(raw_cum,atmo_cum,cumulative,x,y,dates,folder/"correction_comparison.png")
    print(f"\nSaved: {folder}\nCumulative stack: {cumulative.shape} (dates × rows × cols)")
    print("Compare displacement.npy against displacement_unfiltered.npy and temporal_filter_report.csv")
    return folder


if __name__=="__main__":
    process()
