#!/usr/bin/env python3
"""Quick viewer for Stage 2 NISAR NumPy displacement and coherence stacks.

Expected input directory:
    data/processed/displacement.npy  (T, Y, X), millimeters
    data/processed/coherence.npy     (T, Y, X), 0..1
    data/processed/metadata.json    shared spatial grid + acquisition-pair dates

Usage from project root (Windows PowerShell or shell):
    python src/view_nisar_stack.py --list
    python src/view_nisar_stack.py --scene 1 --show
    python src/view_nisar_stack.py --scene 2 --bbox 35.35 31.43 35.45 31.50 --show
    python src/view_nisar_stack.py --scene 1 --display-coh-min 0.5 --vlim 20

All bounding boxes are WEST SOUTH EAST NORTH in longitude/latitude.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from affine import Affine
from matplotlib.colors import Normalize, TwoSlopeNorm
from pyproj import Transformer
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds


def read_stack(folder: Path):
    metadata_file = folder / "metadata.json"
    if not metadata_file.is_file():
        raise FileNotFoundError(f"Missing {metadata_file}")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    files = metadata.get("files", {})
    displacement = np.load(folder / files.get("displacement", "displacement.npy"), mmap_mode="r")
    coherence = np.load(folder / files.get("coherence", "coherence.npy"), mmap_mode="r")
    if displacement.ndim != 3 or displacement.shape != coherence.shape:
        raise ValueError("Expected aligned 3D displacement and coherence arrays, shape (T, Y, X)")
    if list(displacement.shape) != metadata.get("array_shape"):
        raise ValueError("Array shapes disagree with metadata.json")
    pairs = metadata.get("pairs", [])
    if len(pairs) != displacement.shape[0]:
        raise ValueError("Pair metadata count doesn't match the time dimension")
    transform = metadata.get("grid", {}).get("transform_affine")
    if transform is None or len(transform) != 6:
        raise ValueError("Missing grid.transform_affine in metadata.json")
    crs = metadata.get("grid", {}).get("crs")
    if not crs:
        raise ValueError("Missing grid.crs in metadata.json")
    return displacement, coherence, metadata, Affine(*map(float, transform)), crs


def parse_crop(bbox, shape, affine, crs):
    """Return row/column slice indices for a WGS84 bounding box."""
    ny, nx = shape
    if bbox is None:
        return 0, ny, 0, nx
    w, s, e, n = map(float, bbox)
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError("BBOX must be west south east north (longitude, latitude)")
    projected = transform_bounds("EPSG:4326", crs, w, s, e, n, densify_pts=21)
    win = from_bounds(*projected, transform=affine)
    row0 = max(0, math.floor(win.row_off))
    col0 = max(0, math.floor(win.col_off))
    row1 = min(ny, math.ceil(win.row_off + win.height))
    col1 = min(nx, math.ceil(win.col_off + win.width))
    if row1 <= row0 or col1 <= col0:
        raise ValueError("Requested geographic bbox does not overlap this output grid")
    return row0, row1, col0, col1


def geographic_edges(affine, crs, row0, row1, col0, col1):
    """Transform cell *edges* to lon/lat for accurately located pcolormesh."""
    rows, cols = np.meshgrid(
        np.arange(row0, row1 + 1, dtype=float),
        np.arange(col0, col1 + 1, dtype=float),
        indexing="ij",
    )
    x = affine.a * cols + affine.b * rows + affine.c
    y = affine.d * cols + affine.e * rows + affine.f
    lon, lat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(x, y)
    return lon, lat


def make_plot(folder, index, displacement, coherence, metadata, affine, crs, args):
    r0, r1, c0, c1 = parse_crop(args.bbox, displacement.shape[1:], affine, crs)
    d = np.array(displacement[index, r0:r1, c0:c1], dtype=float)
    c = np.array(coherence[index, r0:r1, c0:c1], dtype=float)
    if args.display_coh_min is not None:
        d[c < args.display_coh_min] = np.nan
    lon, lat = geographic_edges(affine, crs, r0, r1, c0, c1)

    finite = np.abs(d[np.isfinite(d)])
    default_limit = float(np.percentile(finite, 98)) if finite.size else 1.0
    limit = args.vlim if args.vlim is not None else max(default_limit, 1e-4)
    if limit <= 0:
        raise ValueError("--vlim must be positive")

    pair = metadata["pairs"][index]
    start = pair["reference_time_utc"][:10]
    end = pair["secondary_time_utc"][:10]
    component = metadata.get("displacement", {}).get("component", "los").upper()
    baseline = pair.get("baseline_days")
    desc = f"{start} to {end}" + (f" | {baseline:.1f} days" if isinstance(baseline, (int, float)) else "")

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.7), layout="constrained")
    disp_cmap = plt.get_cmap("RdBu_r").copy()
    disp_cmap.set_bad("#dedede")
    coh_cmap = plt.get_cmap("viridis").copy()
    coh_cmap.set_bad("#dedede")

    im0 = axes[0].pcolormesh(
        lon, lat, np.ma.masked_invalid(d), cmap=disp_cmap,
        norm=TwoSlopeNorm(vcenter=0, vmin=-limit, vmax=limit), shading="flat",
        rasterized=True,
    )
    fig.colorbar(im0, ax=axes[0], label="Relative displacement (mm)")
    axes[0].set_title(f"Corrected {component} displacement")

    im1 = axes[1].pcolormesh(
        lon, lat, np.ma.masked_invalid(c), cmap=coh_cmap,
        norm=Normalize(0, 1), shading="flat", rasterized=True,
    )
    fig.colorbar(im1, ax=axes[1], label="Coherence")
    axes[1].set_title("Interferometric coherence")

    for ax in axes:
        ax.set_xlabel("Longitude (°E)")
        ax.set_ylabel("Latitude (°N)")
        ax.set_aspect("equal", adjustable="box")
        ax.grid(alpha=0.15)

    fig.suptitle(f"NISAR GUNW | Pair {index + 1}/{len(metadata['pairs'])} | {desc}")
    output = Path(args.out) if args.out else folder / f"quicklook_pair_{index+1:03d}.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=160)
    print(f"Saved: {output}")
    print(f"Valid displacement pixels shown: {np.isfinite(d).sum():,}/{d.size:,}")
    if args.show:
        plt.show()
    else:
        plt.close(fig)


def main():
    p = argparse.ArgumentParser(description="Plot aligned Stage 2 NISAR displacement/coherence NumPy stacks")
    p.add_argument("--input", default="data/processed", help="Folder containing the two .npy files and metadata.json")
    p.add_argument("--list", action="store_true", help="List acquisition pairs and exit")
    p.add_argument("--scene", type=int, default=1, help="One-based acquisition-pair index (default 1)")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), help="Optional WGS84 map crop")
    p.add_argument("--vlim", type=float, help="Symmetric displacement color limit in millimeters")
    p.add_argument("--display-coh-min", type=float, help="Additional *display-only* coherence mask (0..1)")
    p.add_argument("--out", help="PNG output path (default quicklook_pair_NNN.png inside input folder)")
    p.add_argument("--show", action="store_true", help="Open the matplotlib window")
    args = p.parse_args()
    if args.display_coh_min is not None and not 0 <= args.display_coh_min <= 1:
        p.error("--display-coh-min must be between 0 and 1")
    folder = Path(args.input)
    displacement, coherence, metadata, affine, crs = read_stack(folder)
    if args.list:
        print(f"Array shape: {displacement.shape}; component: {metadata.get('displacement', {}).get('component', 'unknown')}")
        for i, pair in enumerate(metadata["pairs"], start=1):
            print(f"{i:3}. {pair['reference_time_utc'][:10]} → {pair['secondary_time_utc'][:10]}  {pair.get('granule_name', '')}")
        return
    if not 1 <= args.scene <= displacement.shape[0]:
        p.error(f"--scene must be between 1 and {displacement.shape[0]}")
    make_plot(folder, args.scene-1, displacement, coherence, metadata, affine, crs, args)


if __name__ == "__main__":
    main()
