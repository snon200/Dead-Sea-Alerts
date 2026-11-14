#!/usr/bin/env python3
"""Plot NISAR Harmony GUNW GeoTIFF phase and coherence products.

Examples:
    python visualize_nisar_gunw.py --dir data/nisar_gunw
    python visualize_nisar_gunw.py --dir data/nisar_gunw --show
    python visualize_nisar_gunw.py --dir data/nisar_gunw --bbox 35.35 31.43 35.45 31.50
    python visualize_nisar_gunw.py --phase path/to/unwrappedPhase.tif --coherence path/to/coherenceMagnitude.tif
    python visualize_nisar_gunw.py --dir data/nisar_gunw --list

The phase panel shows the UNCORRECTED, unwrapped phase centered on its
regional median for display only (radians). It is not an elevation map.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds


LAYER_TOKENS = re.compile(
    r"unwrapped[_\-.]?phase|unwphase|coherence[_\-.]?magnitude|coherencemagnitude|coherence|(?:^|[_\-.])coh(?:$|[_\-.])",
    re.IGNORECASE,
)


def layer_type(filename: str) -> str | None:
    name = Path(filename).stem.lower()
    if "unwrappedphase" in name or "unwrapped_phase" in name or "unwphase" in name:
        return "phase"
    if "coherencemagnitude" in name or "coherence_magnitude" in name or "coherence" in name or re.search(r"(?:^|[_\-.])coh(?:$|[_\-.])", name):
        return "coherence"
    return None


def scene_key(path: Path) -> str:
    return LAYER_TOKENS.sub("", path.stem).strip("_-.").lower()


def find_pairs(directory: Path) -> list[tuple[str, Path, Path | None]]:
    rasters = sorted(list(directory.glob("*.tif")) + list(directory.glob("*.tiff")))
    if not rasters:
        raise ValueError(f"No GeoTIFF files found in {directory.resolve()}")
    grouped: dict[str, dict[str, Path]] = {}
    skipped: list[Path] = []
    for p in rasters:
        kind = layer_type(p.name)
        if kind is None:
            skipped.append(p)
            continue
        key = scene_key(p)
        if kind in grouped.setdefault(key, {}):
            print(f"WARNING: Duplicate {kind} for {key}; using {grouped[key][kind].name}")
            continue
        grouped[key][kind] = p
    if skipped:
        print("Skipping other TIFF layers:", ", ".join(p.name for p in skipped))
    pairs = []
    for key, layers in sorted(grouped.items()):
        if "phase" in layers:
            pairs.append((key, layers["phase"], layers.get("coherence")))
        elif "coherence" in layers:
            print(f"WARNING: Coherence without matching phase: {layers['coherence'].name}")
    if not pairs:
        names = "\n  ".join(p.name for p in rasters)
        raise ValueError(
            "No unwrapped-phase TIFF recognized. Available TIFFs:\n  " + names
            + "\nTry --phase PATH --coherence PATH to specify files explicitly."
        )
    return pairs


def wgs84_bounds(ds) -> tuple[float, float, float, float]:
    if ds.crs is None:
        raise ValueError(f"GeoTIFF lacks a CRS: {ds.name}")
    return transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)


def intersect_bbox(full_bbox, chosen_bbox):
    if chosen_bbox is None:
        return full_bbox
    w, s, e, n = chosen_bbox
    fw, fs, fe, fn = full_bbox
    clipped = max(w, fw), max(s, fs), min(e, fe), min(n, fn)
    if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
        raise ValueError("Requested --bbox does not intersect the phase raster")
    return clipped


def sample_shape(ds, full_bbox, bounds, max_dim=1800):
    fw, fs, fe, fn = full_bbox
    w, s, e, n = bounds
    nx = max(2, round(ds.width * (e - w) / (fe - fw)))
    ny = max(2, round(ds.height * (n - s) / (fn - fs)))
    factor = min(1.0, max_dim / max(nx, ny))
    return max(2, round(ny * factor)), max(2, round(nx * factor))


def reproject_to_bbox(path: Path, bbox, shape):
    """Read/reproject only the requested area into a small geographic grid."""
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"Missing CRS: {path}")
        target = np.full(shape, np.nan, dtype=np.float32)
        reproject(
            source=rasterio.band(src, 1),
            destination=target,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=from_bounds(*bbox, width=shape[1], height=shape[0]),
            dst_crs="EPSG:4326",
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
            init_dest_nodata=True,
        )
    target[~np.isfinite(target)] = np.nan
    return target


def plot_pair(key: str, phase_path: Path, coherence_path: Path | None,
              bbox, threshold: float, output: Path, show: bool):
    with rasterio.open(phase_path) as src:
        print(f"\nPhase: {phase_path.name}\n  shape={src.height}x{src.width}, CRS={src.crs}, nodata={src.nodata}")
        full_bbox = wgs84_bounds(src)
        clipped = intersect_bbox(full_bbox, bbox)
        shape = sample_shape(src, full_bbox, clipped)

    phase = reproject_to_bbox(phase_path, clipped, shape)
    coherence = reproject_to_bbox(coherence_path, clipped, shape) if coherence_path else None
    valid_phase = np.isfinite(phase)
    if coherence is not None:
        good = valid_phase & np.isfinite(coherence) & (coherence >= threshold)
        valid_coh = np.isfinite(coherence)
        if valid_coh.any():
            print(f"Coherence: {coherence_path.name}; median={np.nanmedian(coherence):.3f}")
        print(f"  Valid phase pixels above coherence threshold {threshold:g}: {good.sum()} / {valid_phase.sum()}")
    else:
        print("WARNING: No coherence file paired; phase will not be coherence-masked")
        good = valid_phase

    if not valid_phase.any():
        print("WARNING: No valid phase pixels in region")
    if valid_phase.any() and not good.any():
        print("WARNING: No pixels meet the coherence threshold; lower --coh-min to inspect phase")

    # Center the unwrapped phase for DISPLAY ONLY (arbitrary phase reference).
    phase_display = np.where(good, phase, np.nan)
    if good.any():
        phase_display -= np.nanmedian(phase_display)
        phase_display *= 0.2384 * 1000 / (4 * np.pi)  # LOS displacement [mm]
        lim = float(np.nanpercentile(np.abs(phase_display), 98))
        if not np.isfinite(lim) or lim < 1e-6:
            lim = 1.0
    else:
        lim = 1.0

    fig, axs = plt.subplots(1, 2, figsize=(13.5, 5.8), constrained_layout=True)
    w, s, e, n = clipped
    extent = (w, e, s, n)
    im0 = axs[0].imshow(
        np.ma.masked_invalid(phase_display),
        extent=extent, origin="upper", cmap="RdBu_r", vmin=-lim, vmax=lim,
        interpolation="nearest",
    )
    plt.colorbar(im0, ax=axs[0], fraction=0.047, pad=0.02, label="Relative LOS displacement (mm)")
    axs[0].set_title(f"Relative LOS displacement\n(coherence ≥ {threshold:g}; regional median removed)")

    if coherence is not None:
        im1 = axs[1].imshow(
            np.ma.masked_invalid(coherence),
            extent=extent, origin="upper", cmap="viridis", vmin=0, vmax=1,
            interpolation="nearest",
        )
        plt.colorbar(im1, ax=axs[1], fraction=0.047, pad=0.02, label="Coherence")
        axs[1].set_title("Coherence magnitude")
    else:
        axs[1].axis("off")
        axs[1].text(0.5, 0.5, "No matching coherence GeoTIFF", transform=axs[1].transAxes,
                    va="center", ha="center")

    for ax in axs:
        if ax.axison:
            ax.set_xlabel("Longitude (°E)")
            ax.set_ylabel("Latitude (°N)")
            ax.set_aspect(1 / np.cos(np.deg2rad((s + n) / 2)))
            ax.grid(alpha=0.16, linewidth=0.4)

    label = phase_path.stem
    if len(label) > 83:
        label = label[:75] + "..."
    fig.suptitle(f"NISAR GUNW — {label}", fontsize=11)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=175, bbox_inches="tight")
    print(f"Saved: {output.resolve()}")
    if show:
        plt.show()
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/nisar_gunw"), help="Folder containing Harmony .tif files")
    p.add_argument("--phase", type=Path, help="Optional explicit unwrappedPhase TIFF")
    p.add_argument("--coherence", type=Path, help="Optional explicit coherenceMagnitude TIFF")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), help="Optional crop in longitude/latitude")
    p.add_argument("--coh-min", type=float, default=0.3, help="Mask phase pixels below this coherence (default 0.3)")
    p.add_argument("--out", type=Path, help="PNG output directory (default: <dir>/plots)")
    p.add_argument("--list", action="store_true", help="List recognized files and pairs without plotting")
    p.add_argument("--show", action="store_true", help="Open Matplotlib figures as well as saving PNGs")
    a = p.parse_args()
    if not 0 <= a.coh_min <= 1:
        p.error("--coh-min must be between 0 and 1")
    if a.bbox and not (a.bbox[0] < a.bbox[2] and a.bbox[1] < a.bbox[3]):
        p.error("--bbox must be W S E N with W < E and S < N")
    if a.coherence and not a.phase:
        p.error("--coherence requires --phase")
    if a.phase:
        if not a.phase.is_file() or (a.coherence and not a.coherence.is_file()):
            p.error("The --phase or --coherence file does not exist")
        pairs = [(scene_key(a.phase), a.phase, a.coherence)]
    else:
        pairs = find_pairs(a.dir)

    print(f"Recognized {len(pairs)} phase scene(s):")
    for i, (_, phase, coh) in enumerate(pairs, 1):
        print(f"  {i}. Phase: {phase.name}\n     Coherence: {coh.name if coh else 'NOT FOUND'}")
    if a.list:
        return

    out_dir = a.out or (a.dir / "plots")
    for key, phase, coh in pairs:
        short = re.sub(r"[^a-zA-Z0-9_.-]+", "_", key)[:72] or "GUNW"
        digest = hashlib.sha1(str(phase).encode("utf-8")).hexdigest()[:8]
        output = out_dir / f"gunw_{short}_{digest}.png"
        plot_pair(key, phase, coh, a.bbox, a.coh_min, output, a.show)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, rasterio.errors.RasterioError) as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        sys.exit(1)
