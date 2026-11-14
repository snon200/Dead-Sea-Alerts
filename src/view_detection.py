#!/usr/bin/env python3
"""Quicklook viewer for Stage 3 NISAR deformation-detection results.

Reads a Stage 3 run and its matching Stage 2 cumulative stack:
    data/detection/<run>/velocity.npy
    data/detection/<run>/acceleration.npy
    data/detection/<run>/candidate_mask.npy
    data/detection/<run>/anomalies.geojson
    data/detection/<run>/metadata.json

    data/processed/<run>/displacement.npy
    data/processed/<run>/coherence.npy
    data/processed/<run>/metadata.json

Examples (from project root):
    python src/view_detection.py --list
    python src/view_detection.py --scene 3 --show
    python src/view_detection.py --date 2026-10-19 --show
    python src/view_detection.py --input data/detection/run_YYYYMMDD_HHMMSS_ffffff --show
    python src/view_detection.py --scene 3 --bbox 35.35 31.43 35.45 31.50 --show
    python src/view_detection.py --input data/detection/run_... --processed data/processed/run_... --vlim 25 --show

--scene is a 1-based ACQUISITION DATE index (not a GUNW pair index).
If omitted, the latest acquisition date is shown. Plots are in projected UTM
coordinates (km); the scale bar measures true map distance in metres/kilometres.

IMPORTANT: candidate_mask.npy records regions for EVERY acquisition date, but
anomalies.geojson stores only the LAST geometry of each tracked anomaly.
The viewer outlines polygons ONLY on their matching 'last_observed' date.
It cannot reconstruct historical polygon outlines that Stage 3 did not save.

Dependencies: numpy, matplotlib, rasterio, pyproj, affine.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from affine import Affine
from matplotlib import patheffects
from matplotlib.colors import Normalize, TwoSlopeNorm
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter
from pyproj import Transformer
from rasterio.windows import from_bounds


PRIORITY_COLORS = {
    "watch": "#f5a623", "review": "#e96b23", "urgent_review": "#d62828"
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def select_detection_dir(path: Path) -> Path:
    if (path / "metadata.json").is_file():
        if load_json(path / "metadata.json").get("stage") == "subsidence_detection":
            return path
    candidates = [
        p.parent for p in path.glob("*/metadata.json")
        if load_json(p).get("stage") == "subsidence_detection"
    ] if path.is_dir() else []
    if not candidates:
        raise FileNotFoundError(f"No Stage 3 detection run found under: {path}")
    chosen = max(candidates, key=lambda p: (p / "metadata.json").stat().st_mtime)
    print(f"Using latest detection run: {chosen}")
    return chosen


def find_processing_dir(det_dir: Path, det_meta: dict, override: str | None) -> Path:
    if override:
        folder = Path(override)
    else:
        candidate = Path(det_meta.get("source_processing_directory", ""))
        if candidate.is_dir():
            folder = candidate
        else:
            folder = Path("data/processed") / det_dir.name
    if not (folder / "metadata.json").is_file():
        raise FileNotFoundError(
            f"Cannot locate Stage 2 data at {folder}. Pass --processed PATH explicitly."
        )
    return folder


def read_inputs(det_dir: Path, proc_override: str | None):
    det_meta = load_json(det_dir / "metadata.json")
    proc_dir = find_processing_dir(det_dir, det_meta, proc_override)
    proc_meta = load_json(proc_dir / "metadata.json")
    if proc_meta.get("stage") != "geophysical_processing_cumulative_stack":
        raise ValueError("Expected a cumulative Stage 2 stack")

    df = det_meta.get("files", {})
    pf = proc_meta.get("files", {})
    displacement = np.load(proc_dir / pf.get("displacement", "displacement.npy"), mmap_mode="r")
    coherence = np.load(proc_dir / pf.get("coherence", "coherence.npy"), mmap_mode="r")
    velocity = np.load(det_dir / df.get("velocity", "velocity.npy"), mmap_mode="r")
    acceleration = np.load(det_dir / df.get("acceleration", "acceleration.npy"), mmap_mode="r")
    mask = np.load(det_dir / df.get("candidate_mask", "candidate_mask.npy"), mmap_mode="r")
    geo = load_json(det_dir / df.get("polygons", "anomalies.geojson"))

    if displacement.ndim != 3 or coherence.shape != displacement.shape or mask.shape != displacement.shape:
        raise ValueError("displacement, coherence and candidate_mask must have identical (T,Y,X) shape")
    t, h, w = displacement.shape
    if velocity.shape != (t - 1, h, w) or acceleration.shape != (max(0, t - 2), h, w):
        raise ValueError("Stage 3 velocity/acceleration array shapes do not match Stage 2")
    dates = proc_meta.get("dates", [])
    if len(dates) != t or dates != det_meta.get("dates"):
        raise ValueError("Stage 2 and Stage 3 acquisition dates disagree")
    if not isinstance(geo.get("features"), list):
        raise ValueError("Invalid anomalies.geojson")

    grid = proc_meta.get("grid", {})
    aff = grid.get("transform_affine")
    crs = grid.get("crs")
    if not aff or len(aff) != 6 or not crs:
        raise ValueError("Stage 2 metadata missing projected grid.transform_affine or grid.crs")
    return proc_meta, det_meta, displacement, coherence, velocity, acceleration, mask, geo, Affine(*aff), crs


def crop_indices(bbox, width, height, transform, crs):
    if bbox is None:
        return (0, height, 0, width)
    west, south, east, north = bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("--bbox must be WEST SOUTH EAST NORTH in longitude/latitude")
    left, bottom, right, top = Transformer.from_crs(
        "EPSG:4326", crs, always_xy=True
    ).transform_bounds(west, south, east, north, densify_pts=21)
    # Window slicing assumes north-up, unrotated UTM grids (as produced by Stage 2).
    if transform.b != 0 or transform.d != 0 or transform.a <= 0 or transform.e >= 0:
        raise ValueError("This viewer requires north-up projected rasters")
    win = from_bounds(left, bottom, right, top, transform=transform)
    r0, r1 = max(0, math.floor(win.row_off)), min(height, math.ceil(win.row_off + win.height))
    c0, c1 = max(0, math.floor(win.col_off)), min(width, math.ceil(win.col_off + win.width))
    if r1 <= r0 or c1 <= c0:
        raise ValueError("Requested bbox does not overlap this stack")
    return r0, r1, c0, c1


def scale_limit(data: np.ndarray, supplied: float | None) -> float:
    if supplied is not None:
        if not np.isfinite(supplied) or supplied <= 0:
            raise ValueError("Color limits must be positive")
        return float(supplied)
    finite = np.abs(data[np.isfinite(data)])
    return max(1e-3, float(np.percentile(finite, 98))) if finite.size else 1.0


def nice_distance(metres: float) -> float:
    if metres <= 0:
        return 100
    order = 10 ** math.floor(math.log10(metres))
    for unit in (5, 2, 1):
        if unit * order <= metres:
            return unit * order
    return 0.5 * order


def add_scalebar(ax, extent):
    left, right, bottom, top = extent
    width, height = right - left, top - bottom
    distance = nice_distance(width * 0.22)
    x0, y = left + width * 0.07, bottom + height * 0.08
    stroke = [patheffects.Stroke(linewidth=5, foreground="white"), patheffects.Normal()]
    ax.plot([x0, x0 + distance], [y, y], color="black", lw=3, path_effects=stroke, zorder=10)
    tick = height * 0.011
    for x in (x0, x0 + distance):
        ax.plot([x, x], [y - tick, y + tick], color="black", lw=2, path_effects=stroke, zorder=10)
    label = f"{distance/1000:g} km" if distance >= 1000 else f"{distance:g} m"
    ax.text(x0 + distance/2, y + tick * 1.7, label, ha="center", va="bottom",
            fontsize=9, path_effects=[patheffects.withStroke(linewidth=3, foreground="white")], zorder=11)


def projected_geometry(geometry, projector):
    if not geometry:
        return
    kind = geometry.get("type")
    if kind == "Polygon":
        polygons = [geometry.get("coordinates", [])]
    elif kind == "MultiPolygon":
        polygons = geometry.get("coordinates", [])
    else:
        return
    for polygon in polygons:
        for ring in polygon:
            if len(ring) < 2:
                continue
            xs, ys = projector.transform([p[0] for p in ring], [p[1] for p in ring])
            yield xs, ys


def draw_polygons(ax, features, projector):
    for feature in features:
        props = feature.get("properties", {})
        color = PRIORITY_COLORS.get(props.get("priority"), "#9c27b0")
        for x, y in projected_geometry(feature.get("geometry", {}), projector):
            ax.plot(x, y, color=color, linewidth=2.3,
                    path_effects=[patheffects.Stroke(linewidth=3.6, foreground="white"), patheffects.Normal()],
                    zorder=8)


def show_figure(det_dir: Path, meta: dict, det_meta: dict, d, c, v, a, mask, geo,
                affine, crs, t: int, args):
    n, height, width = d.shape
    r0, r1, c0, c1 = crop_indices(args.bbox, width, height, affine, crs)
    x0 = affine.c + c0 * affine.a
    x1 = affine.c + c1 * affine.a
    y1 = affine.f + r0 * affine.e
    y0 = affine.f + r1 * affine.e
    extent = (x0, x1, y0, y1)
    crop = np.s_[r0:r1, c0:c1]
    disp = np.asarray(d[t][crop], dtype=float)
    coh = np.asarray(c[t][crop], dtype=float)
    candidates = np.asarray(mask[t][crop], dtype=bool)
    rate = np.asarray(v[t-1][crop], dtype=float) if t >= 1 else None
    accel = np.asarray(a[t-2][crop], dtype=float) if t >= 2 else None
    if args.display_coh_min is not None:
        disp[coh < args.display_coh_min] = np.nan
        if rate is not None:
            rate[coh < args.display_coh_min] = np.nan
        if accel is not None:
            accel[coh < args.display_coh_min] = np.nan
    date_selected = meta["dates"][t]
    active = [f for f in geo["features"] if f.get("properties", {}).get("last_observed") == date_selected]
    projector = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    fig, axes = plt.subplots(2, 2, figsize=(13, 10), layout="constrained")
    diverging = plt.get_cmap("RdBu_r").copy()
    diverging.set_bad("#dedede")
    sequential = plt.get_cmap("viridis").copy()
    sequential.set_bad("#dedede")
    for ax in axes.flat:
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel(f"Easting (km, {crs})")
        ax.set_ylabel("Northing (km)")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x / 1000:g}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y / 1000:g}"))
        ax.grid(alpha=0.12, zorder=0)

    def panel(ax, array, title, units, limit=None, cmap=None):
        ax.set_title(title, fontsize=11)
        if array is None:
            ax.text(0.5, 0.5, "Not available for this date", transform=ax.transAxes,
                    ha="center", va="center", fontsize=11)
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
        else:
            if cmap is None:
                lim = scale_limit(array, limit)
                img = ax.imshow(np.ma.masked_invalid(array), extent=extent, origin="upper",
                                cmap=diverging, norm=TwoSlopeNorm(vmin=-lim, vcenter=0, vmax=lim),
                                interpolation="nearest")
            else:
                img = ax.imshow(np.ma.masked_invalid(array), extent=extent, origin="upper",
                                cmap=cmap, norm=Normalize(vmin=0, vmax=1), interpolation="nearest")
            fig.colorbar(img, ax=ax, shrink=0.78, label=units)
        add_scalebar(ax, extent)
        return ax

    component = meta.get("displacement", {}).get("component", "los").upper()
    ax0 = panel(axes[0, 0], disp, f"Cumulative {component} displacement", "mm", args.vlim)
    if t >= 1:
        period = f"{meta['dates'][t-1]} → {date_selected}"
        ax1 = panel(axes[0, 1], rate, f"Velocity over {period}", "mm/day", args.rate_limit)
    else:
        ax1 = panel(axes[0, 1], None, "Velocity", "mm/day")
    if t >= 2:
        ax2 = panel(axes[1, 0], accel, "Acceleration (difference of consecutive rates)", "mm/day²", args.accel_limit)
    else:
        ax2 = panel(axes[1, 0], None, "Acceleration", "mm/day²")
    ax3 = panel(axes[1, 1], coh, "Coherence quality proxy + detected pixels", "0–1", cmap=sequential)

    # Candidate pixels are the per-date evidence. Draw their exact outline on
    # rate and quality panels, even when a track's last stored polygon is older.
    if candidates.any():
        for ax in (ax0, ax1, ax2, ax3):
            if (ax is ax1 and rate is None) or (ax is ax2 and accel is None):
                continue
            # Masked contour uses pixel-cell centers in projected CRS.
            centers_x = affine.c + (np.arange(c0, c1) + 0.5) * affine.a
            centers_y = affine.f + (np.arange(r0, r1) + 0.5) * affine.e
            if candidates.shape[0] > 1 and candidates.shape[1] > 1:
                ax.contour(centers_x, centers_y, candidates.astype(float), levels=[0.5],
                           colors=["#ff00a8"], linewidths=1.0, zorder=7)

    for ax in (ax0, ax3):
        draw_polygons(ax, active, projector)
    if active:
        handles = [Line2D([0], [0], color=PRIORITY_COLORS.get(name, "#9c27b0"), lw=2.6, label=name)
                   for name in ("watch", "review", "urgent_review")
                   if any(f.get("properties", {}).get("priority") == name for f in active)]
        ax0.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.85)

    fig.suptitle(f"NISAR anomaly screening | {det_dir.name} | Acquisition {t+1}/{n}: {date_selected}", fontsize=12)
    fig.text(0.5, 0.006,
             f"Magenta = candidate-pixel boundary; colored outlines = tracked polygons last observed on this date ({len(active)}). "
             "Exploratory screening, not a confirmed sinkhole/hazard map.",
             ha="center", va="bottom", fontsize=8)
    out = Path(args.out) if args.out else det_dir / f"detection_quicklook_{t+1:03d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    print(f"Saved: {out}")
    print(f"Date: {date_selected}; candidate pixels: {int(candidates.sum())}; "
          f"tracked polygon outlines last observed on this date: {len(active)}; total tracked polygons: {len(geo['features'])}")
    if args.show:
        plt.show()
    else:
        plt.close(fig)


def main():
    p = argparse.ArgumentParser(description="View Stage 3 anomaly masks, displacement, velocities and polygons")
    p.add_argument("--input", default="data/detection", help="Stage 3 run folder (or parent, to use newest run)")
    p.add_argument("--processed", help="Stage 2 run folder if moved from its original location")
    p.add_argument("--list", action="store_true", help="List acquisition dates and number of candidate regions")
    p.add_argument("--scene", type=int, help="1-based acquisition date index (default latest)")
    p.add_argument("--date", help="Acquisition date YYYY-MM-DD (alternative to --scene)")
    p.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"), help="Crop in longitude/latitude")
    p.add_argument("--vlim", type=float, help="Symmetric displacement color limit in mm")
    p.add_argument("--rate-limit", type=float, help="Symmetric velocity color limit in mm/day")
    p.add_argument("--accel-limit", type=float, help="Symmetric acceleration color limit in mm/day²")
    p.add_argument("--display-coh-min", type=float, help="Extra display-only quality threshold (0 to 1)")
    p.add_argument("--out", help="Output PNG path (default detection_quicklook_NNN.png inside detection run)")
    p.add_argument("--show", action="store_true", help="Show interactive Matplotlib plot")
    args = p.parse_args()
    if args.scene is not None and args.date:
        p.error("Use either --scene or --date, not both")
    if args.display_coh_min is not None and not 0 <= args.display_coh_min <= 1:
        p.error("--display-coh-min must be between 0 and 1")
    run_dir = select_detection_dir(Path(args.input))
    (proc_meta, det_meta, d, c, v, a, mask, geo, aff, crs) = read_inputs(run_dir, args.processed)
    dates = proc_meta["dates"]
    if args.list:
        stats_by_date = {s["end"]: s.get("regions") for s in det_meta.get("intervals", [])}
        for i, day in enumerate(dates, 1):
            nregions = stats_by_date.get(day, 0)
            active = sum(f.get("properties", {}).get("last_observed") == day for f in geo["features"])
            print(f"{i:3d}. {day} | detected regions: {nregions} | tracked polygons ending here: {active}")
        return
    if args.date:
        if args.date not in dates:
            p.error(f"{args.date} not in acquisition dates: {dates}")
        index = dates.index(args.date)
    elif args.scene is not None:
        if not 1 <= args.scene <= len(dates):
            p.error(f"--scene must be between 1 and {len(dates)}")
        index = args.scene - 1
    else:
        index = len(dates) - 1
    show_figure(run_dir, proc_meta, det_meta, d, c, v, a, mask, geo, aff, crs, index, args)


if __name__ == "__main__":
    main()
