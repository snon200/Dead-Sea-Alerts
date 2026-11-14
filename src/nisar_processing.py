
"""Stage 2: NISAR GUNW geophysical processing.

Inputs: Scene manifests and GeoTIFFs from nisar_acquisition.py

Outputs per scene:
    corrected_phase_rad.tif
    los_displacement_mm.tif
    vertical_estimate_mm.tif (if incidence angle available)
    valid_mask.tif
    manifest.json

Install:
    pip install numpy rasterio

Run:
    python src/nisar_processing.py --input data/raw

If Stage 1 metadata is unavailable, provide verified parameters:
    python src/nisar_processing.py --input data/raw \
        --frequency-mhz 1229 --incidence-deg 40

IMPORTANT:
    - Frequency and angle in the example are illustrative.
    - Phase-to-displacement sign must be verified.
    - Vertical conversion assumes zero horizontal displacement.
    - Scene-wide median is NOT a validated stable reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio

from rasterio.features import geometry_mask
from rasterio.warp import transform_geom


C = 299_792_458.0
LOG = logging.getLogger("nisar.processing")


# ============================================================
# CONFIGURATION
# ============================================================

@dataclass(frozen=True)
class ProcessingConfig:

    coherence_min: float = 0.3

    reference_bbox: tuple[float, float, float, float] | None = None

    min_reference_pixels: int = 10

    apply_ionosphere: bool = True

    # Overrides only when acquisition metadata is missing.
    frequency_mhz: float | None = None
    incidence_deg: float | None = None

    # Physical sign convention must be verified.
    phase_sign: int = 1

    def validate(self):

        if not 0 <= self.coherence_min <= 1:
            raise ValueError(
                "coherence_min must be between 0 and 1"
            )

        if self.frequency_mhz is not None:
            if not 1100 < self.frequency_mhz < 1400:
                raise ValueError(
                    "Check the NISAR center frequency in MHz"
                )

        if self.incidence_deg is not None:
            if not 0 <= self.incidence_deg < 85:
                raise ValueError(
                    "incidence_deg must be between 0 and 85"
                )

        if self.min_reference_pixels < 1:
            raise ValueError(
                "min_reference_pixels must be positive"
            )

        if self.phase_sign not in (-1, 1):
            raise ValueError(
                "phase_sign must be +1 or -1"
            )

        if self.reference_bbox is not None:
            w, s, e, n = self.reference_bbox

            if not (
                -180 <= w < e <= 180
                and -90 <= s < n <= 90
            ):
                raise ValueError(
                    "Reference bbox must be WEST SOUTH EAST NORTH"
                )


# ============================================================
# FILE AND MANIFEST UTILITIES
# ============================================================

def load_json(path: Path) -> dict:

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path: Path, data: dict) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)

    temp = path.with_name(path.name + ".tmp")

    temp.write_text(
        json.dumps(data, indent=2, allow_nan=False),
        encoding="utf-8"
    )

    temp.replace(path)


def find_manifests(root: Path) -> list[Path]:

    if root.is_file():
        data = load_json(root)

        if "assets" in data:
            return [root]

        # Acquisition index.json
        return sorted(root.parent.glob("*/manifest.json"))

    return sorted(root.glob("*/manifest.json"))


def asset_path(
    manifest: dict,
    manifest_file: Path,
    key: str
) -> Path:

    item = manifest.get("assets", {}).get(key)

    if not isinstance(item, dict) or not item.get("path"):
        raise ValueError(
            f"Missing required asset: {key}"
        )

    value = Path(item["path"])

    if value.is_absolute():
        path = value
    else:
        candidates = [
            Path.cwd() / value,
            manifest_file.parent / value
        ]

        path = next(
            (p for p in candidates if p.is_file()),
            candidates[0]
        )

    if not path.is_file():
        raise FileNotFoundError(f"{key}: {path}")

    return path


# ============================================================
# GEOTIFF INPUT / OUTPUT
# ============================================================

def read_raster(path: Path) -> tuple[np.ndarray, dict]:

    with rasterio.open(path) as ds:

        if ds.crs is None or ds.count != 1:
            raise ValueError(
                f"Expected one georeferenced band: {path}"
            )

        band = ds.read(1, masked=True)

        if np.issubdtype(band.dtype, np.integer):
            array = np.asarray(band.filled(0))
        else:
            array = np.asarray(
                band.filled(np.nan),
                dtype=np.float64
            )

        return array, ds.profile.copy()


def read_aligned(path: Path, reference: dict) -> np.ndarray:

    data, profile = read_raster(path)

    fields = ("crs", "transform", "width", "height")

    if any(
        profile[key] != reference[key]
        for key in fields
    ):
        raise ValueError(
            f"Raster grid not aligned with phase: {path}"
        )

    return data


def save_tif(
    path: Path,
    array: np.ndarray,
    source_profile: dict,
    units: str
) -> None:

    is_float = array.dtype.kind == "f"

    out = array.astype(
        "float32" if is_float else "uint8"
    )

    profile = source_profile.copy()

    profile.update(
        driver="GTiff",
        count=1,
        dtype=out.dtype,
        nodata=float("nan") if is_float else 0,
        compress="deflate",
        tiled=False
    )

    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)

    path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(out, 1)
        dst.update_tags(1, units=units)


# ============================================================
# PHASE CORRECTIONS
# ============================================================

def apply_phase_corrections(
    phase: np.ndarray,
    ionosphere: np.ndarray | None
) -> np.ndarray:

    """
    Apply phase corrections in radians.

    Modular entry point for future additions:
        - Troposphere
        - Solid Earth tides
        - Residual orbital effects
    """

    corrected = phase.astype(
        np.float64, copy=True
    )

    if ionosphere is not None:
        corrected -= ionosphere

    return corrected


# ============================================================
# REFERENCE AREA SELECTION
# ============================================================

def make_reference_mask(
    profile: dict,
    bbox: tuple[float, float, float, float] | None,
    good: np.ndarray
) -> np.ndarray:

    if bbox is None:

        LOG.warning(
            "Using scene median reference, "
            "NOT a verified stable-ground reference"
        )

        return good.copy()

    w, s, e, n = bbox

    ring = [
        [w, s],
        [e, s],
        [e, n],
        [w, n],
        [w, s]
    ]

    polygon = {
        "type": "Polygon",
        "coordinates": [ring]
    }

    projected = transform_geom(
        "EPSG:4326",
        profile["crs"],
        polygon
    )

    footprint = geometry_mask(
        [projected],
        out_shape=good.shape,
        transform=profile["transform"],
        invert=True
    )

    return good & footprint


def choose_reference(
    phase: np.ndarray,
    components: np.ndarray,
    candidates: np.ndarray,
    nmin: int
) -> tuple[int, float, int]:

    if np.count_nonzero(candidates) < nmin:
        raise ValueError(
            "Too few valid reference pixels"
        )

    labels, counts = np.unique(
        components[candidates],
        return_counts=True
    )

    component = int(
        labels[np.argmax(counts)]
    )

    pixels = (
        candidates &
        (components == component)
    )

    count = int(np.count_nonzero(pixels))

    if count < nmin:
        raise ValueError(
            "Too few reference pixels in one component"
        )

    offset = float(np.median(phase[pixels]))

    return component, offset, count


# ============================================================
# PROCESS ONE GUNW SCENE
# ============================================================

def process_scene(
    manifest_path: Path | str,
    config: ProcessingConfig,
    output_root: Path | str = "data/processed",
    force: bool = False
) -> dict:

    config.validate()

    manifest_path = Path(manifest_path).resolve()
    source = load_json(manifest_path)

    # Allow scenes whose TIFFs downloaded successfully,
    # even when subsequent metadata extraction failed.
    if source.get("status") not in (
        "complete",
        "metadata_incomplete"
    ):
        raise ValueError(
            "Acquisition has not downloaded the required rasters"
        )

    # --------------------------------------------------------
    # 1. Resolve input assets
    # --------------------------------------------------------

    phase_path = asset_path(
        source, manifest_path, "phase"
    )

    coherence_path = asset_path(
        source, manifest_path, "coherence"
    )

    component_path = asset_path(
        source, manifest_path, "connected_components"
    )

    iono_path = None

    if config.apply_ionosphere:
        iono_path = asset_path(
            source, manifest_path, "ionosphere"
        )

    incidence_path = None

    if (
        config.incidence_deg is None
        and "incidence_angle" in source.get("assets", {})
    ):
        incidence_path = asset_path(
            source, manifest_path, "incidence_angle"
        )

    # --------------------------------------------------------
    # 2. Resolve radar wavelength
    # --------------------------------------------------------

    metadata = source.get("radar_metadata") or {}

    if config.frequency_mhz is not None:
        frequency_hz = config.frequency_mhz * 1e6
    else:
        frequency_hz = metadata.get(
            "center_frequency_hz"
        )

    if (
        frequency_hz is None
        or not 1.1e9 < float(frequency_hz) < 1.4e9
    ):
        raise ValueError(
            "Missing per-scene radar frequency. "
            "Fix Stage 1 metadata extraction or provide "
            "--frequency-mhz using a verified value."
        )

    wavelength_m = C / float(frequency_hz)

    # --------------------------------------------------------
    # 3. Cache fingerprint
    # --------------------------------------------------------

    files = [
        p for p in (
            phase_path,
            coherence_path,
            component_path,
            iono_path,
            incidence_path
        )
        if p is not None
    ]

    fingerprint_data = {
        "config": asdict(config),
        "frequency_hz": float(frequency_hz),
        "files": [
            (
                str(p),
                p.stat().st_size,
                p.stat().st_mtime_ns
            )
            for p in files
        ]
    }

    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_data,
            sort_keys=True
        ).encode()
    ).hexdigest()[:16]

    out_dir = (
        Path(output_root).resolve()
        / manifest_path.parent.name
    )

    output_manifest = out_dir / "manifest.json"

    if not force and output_manifest.is_file():

        old = load_json(output_manifest)

        if (
            old.get("fingerprint") == fingerprint
            and old.get("status") == "complete"
            and all(
                Path(v["path"]).is_file()
                for v in old.get("outputs", {}).values()
            )
        ):
            LOG.info(
                "Using cached result: %s", out_dir
            )

            return old

    # --------------------------------------------------------
    # 4. Read source rasters
    # --------------------------------------------------------

    phase, profile = read_raster(phase_path)

    coherence = read_aligned(
        coherence_path, profile
    ).astype(np.float64)

    components = read_aligned(
        component_path, profile
    )

    ionosphere = None

    if iono_path is not None:
        ionosphere = read_aligned(
            iono_path, profile
        ).astype(np.float64)

    # --------------------------------------------------------
    # 5. Apply geophysical corrections
    # --------------------------------------------------------

    corrected = apply_phase_corrections(
        phase, ionosphere
    )

    # --------------------------------------------------------
    # 6. Construct quality mask
    # --------------------------------------------------------

    # Connected components:
    # 0 = unwrapped data absent
    # 65535 = common fill value

    good = (
        np.isfinite(corrected)
        & np.isfinite(coherence)
        & (coherence >= config.coherence_min)
        & (coherence <= 1)
        & (components > 0)
        & (components < 65535)
    )

    # --------------------------------------------------------
    # 7. Choose relative phase reference
    # --------------------------------------------------------

    candidates = make_reference_mask(
        profile,
        config.reference_bbox,
        good
    )

    component, offset, reference_count = (
        choose_reference(
            corrected,
            components,
            candidates,
            config.min_reference_pixels
        )
    )

    # Different unwrapped components can contain
    # independent arbitrary phase offsets.
    # Use only the component connected to our reference.

    good &= (components == component)

    corrected = np.where(
        good, corrected, np.nan
    )

    relative_phase = corrected - offset

    # --------------------------------------------------------
    # 8. Convert phase to LOS displacement
    # --------------------------------------------------------

    # LOS = phase_sign * wavelength / (4*pi) * phase

    los = (
        relative_phase
        * config.phase_sign
        * wavelength_m
        * 1000
        / (4 * np.pi)
    ).astype(np.float32)

    # --------------------------------------------------------
    # 9. Estimate vertical displacement
    # --------------------------------------------------------

    vertical = None
    incidence_description = None

    if config.incidence_deg is not None:

        theta = config.incidence_deg

        vertical = (
            los / math.cos(math.radians(theta))
        ).astype(np.float32)

        incidence_description = (
            f"Constant {theta} degrees (approximate)"
        )

    elif incidence_path is not None:

        theta = read_aligned(
            incidence_path, profile
        ).astype(np.float64)

        acceptable = (
            np.isfinite(theta)
            & (theta >= 0)
            & (theta < 85)
        )

        vertical = np.full(
            los.shape,
            np.nan,
            dtype=np.float32
        )

        selected = good & acceptable

        vertical[selected] = (
            los[selected]
            / np.cos(np.deg2rad(theta[selected]))
        )

        incidence_description = str(
            incidence_path
        )

    # --------------------------------------------------------
    # 10. Write output GeoTIFFs
    # --------------------------------------------------------

    arrays = {
        "corrected_phase_rad": (
            corrected.astype(np.float32), "radian"
        ),
        "los_displacement_mm": (
            los, "mm"
        ),
        "valid_mask": (
            good.astype(np.uint8), "boolean"
        )
    }

    if vertical is not None:
        arrays["vertical_estimate_mm"] = (
            vertical, "mm"
        )

    outputs = {}

    for name, (data, unit) in arrays.items():

        target = out_dir / f"{name}.tif"

        save_tif(
            target,
            data,
            profile,
            unit
        )

        outputs[name] = {
            "path": str(target),
            "unit": unit
        }

    # --------------------------------------------------------
    # 11. Processing manifest
    # --------------------------------------------------------

    scene = source.get("scene", {})

    result = {
        "schema_version": 1,
        "stage": "geophysical_processing",
        "status": "complete",
        "created_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "fingerprint": fingerprint,
        "source_manifest": str(manifest_path),
        "scene": scene,
        "granule_id": scene.get("granule_id"),
        "radar_wavelength_m": wavelength_m,
        "center_frequency_hz": float(frequency_hz),

        "reference": {
            "bbox_wsen": config.reference_bbox,
            "phase_offset_rad": offset,
            "connected_component": component,
            "pixel_count": reference_count
        },

        "processing": {
            "ionosphere_corrected": config.apply_ionosphere,
            "coherence_min": config.coherence_min,
            "phase_sign": config.phase_sign,
            "positive_los_direction": "NOT VERIFIED",
            "incidence_source": incidence_description,
            "vertical_assumes_zero_horizontal_motion": (
                vertical is not None
            ),
            "not_corrected": [
                "troposphere",
                "solid_earth_tides",
                "orbital_ramp"
            ]
        },

        "statistics": {
            "pixels_total": int(good.size),
            "pixels_valid": int(good.sum()),
            "valid_fraction": float(good.mean())
        },

        "outputs": outputs
    }

    save_json(output_manifest, result)

    LOG.info(
        "Processed %s: %.1f%% valid pixels",
        out_dir.name,
        100 * good.mean()
    )

    return result


# ============================================================
# PROCESS ALL ACQUIRED SCENES
# ============================================================

def process_directory(
    input_path: Path | str,
    config: ProcessingConfig,
    output_root: Path | str = "data/processed",
    force: bool = False
):

    manifests = find_manifests(Path(input_path))

    if not manifests:
        raise FileNotFoundError(
            f"No Stage 1 manifests found at {input_path}"
        )

    results = []

    for path in manifests:

        try:
            result = process_scene(
                path,
                config,
                output_root,
                force
            )

            results.append(result)

        except (
            ValueError,
            FileNotFoundError,
            rasterio.errors.RasterioError
        ) as exc:

            LOG.error(
                "FAILED %s: %s",
                path.parent.name,
                exc
            )

            results.append({
                "status": "failed",
                "scene_dir": path.parent.name,
                "error": str(exc)
            })

    save_json(
        Path(output_root) / "index.json",
        {
            "stage": "geophysical_processing",
            "scenes": results
        }
    )

    return results


# ============================================================
# COMMAND-LINE INTERFACE
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="NISAR GUNW Stage 2 processing"
    )

    parser.add_argument(
        "--input", default="data/raw"
    )

    parser.add_argument(
        "--out", default="data/processed"
    )

    parser.add_argument(
        "--coh-min", type=float, default=0.3
    )

    parser.add_argument(
        "--ref-bbox", nargs=4, type=float,
        metavar=("W", "S", "E", "N")
    )

    parser.add_argument(
        "--min-ref-pixels", type=int, default=10
    )

    parser.add_argument(
        "--frequency-mhz", type=float
    )

    parser.add_argument(
        "--incidence-deg", type=float
    )

    parser.add_argument(
        "--phase-sign", type=int,
        choices=(-1, 1), default=1
    )

    parser.add_argument(
        "--no-ionosphere", action="store_true"
    )

    parser.add_argument(
        "--force", action="store_true"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s"
    )

    config = ProcessingConfig(
        coherence_min=args.coh_min,
        reference_bbox=(
            tuple(args.ref_bbox)
            if args.ref_bbox else None
        ),
        min_reference_pixels=args.min_ref_pixels,
        apply_ionosphere=not args.no_ionosphere,
        frequency_mhz=args.frequency_mhz,
        incidence_deg=args.incidence_deg,
        phase_sign=args.phase_sign
    )

    config.validate()

    results = process_directory(
        args.input,
        config,
        args.out,
        args.force
    )

    n = sum(
        r["status"] == "complete"
        for r in results
    )

    print(
        f"Completed {n}/{len(results)} scene(s), "
        f"saved under {args.out}"
    )

    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
