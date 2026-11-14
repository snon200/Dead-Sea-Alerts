"""Offline adapter tests. Run: python -m unittest discover -s tests -v"""
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from affine import Affine
from rasterio.warp import transform_geom, transform

from src.zone_service import (
    ZoneCalculationUnavailable, build_zones, export_cache, get_zones,
)


class ZoneServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.run = "run_test_001"
        self.proc = self.root / "processed" / self.run
        self.detect = self.root / "detection" / self.run
        self.raw = self.root / "raw" / "G2_test"
        for p in (self.proc, self.detect, self.raw):
            p.mkdir(parents=True)

        x, y = transform("EPSG:4326", "EPSG:32636", [35.39], [31.46])
        self.tf = Affine(80, 0, x[0] - 80, 0, -80, y[0] + 80)
        self.shape = (5, 6)
        d = np.stack([
            np.zeros(self.shape, dtype=np.float32),
            np.full(self.shape, -2.0, dtype=np.float32),
            np.full(self.shape, -4.0, dtype=np.float32),
        ])
        np.save(self.proc / "displacement.npy", d)
        np.save(self.proc / "coherence.npy", np.full_like(d, 0.98))
        self.stage2 = {
            "stage": "geophysical_processing_cumulative_stack",
            "array_shape": list(d.shape),
            "files": {"displacement": "displacement.npy", "coherence": "coherence.npy"},
            "dates": ["2026-06-21", "2026-07-03", "2026-07-15"],
            "grid": {
                "crs": "EPSG:32636", "transform_affine": list(self.tf)[:6],
                "width": self.shape[1], "height": self.shape[0],
            },
            "acquisition_family": {"track": 100, "frame": 17, "direction": "A", "polarization": "HH"},
            "displacement": {"component": "los", "positive_los_direction": "toward",
                             "phase_sign": -1, "reference_bbox_wsen": [35.377, 31.449, 35.388, 31.4555]},
            "pairs": [
                {"granule_id": "G1", "reference_time_utc": "2026-06-21T02:59:41+00:00",
                 "secondary_time_utc": "2026-07-03T02:59:40+00:00"},
                {"granule_id": "G2", "reference_time_utc": "2026-07-03T02:59:40+00:00",
                 "secondary_time_utc": "2026-07-15T02:59:39+00:00"},
            ],
        }
        self.write_json(self.proc / "metadata.json", self.stage2)

        # Build an exactly aligned 2x2 WGS84 polygon over interior pixel centers.
        left, top = self.tf * (1, 1)
        right, bottom = self.tf * (3, 3)
        geom_projected = {"type": "Polygon", "coordinates": [[
            [left, top], [right, top], [right, bottom], [left, bottom], [left, top]
        ]]}
        self.poly = transform_geom("EPSG:32636", "EPSG:4326", geom_projected)
        self.feature = {"type": "Feature", "id": "anomaly-run-specific", "geometry": self.poly,
                        "properties": {"source_run": self.run, "priority": "urgent_review"}}
        self.write_detector([self.feature])

        # Actual pair-coherence raster (deliberately unlike 0.98 date quality proxy).
        tif = self.raw / "pair_coherence.tif"
        with rasterio.open(
            tif, "w", driver="GTiff", width=self.shape[1], height=self.shape[0],
            count=1, dtype="float32", crs="EPSG:32636", transform=self.tf, nodata=-9999,
        ) as dst:
            dst.write(np.full(self.shape, 0.62, dtype=np.float32), 1)
        self.write_json(self.raw / "manifest.json", {
            "scene": {"granule_id": "G2"},
            "assets": {"coherence": {"path": str(tif)}},
        })

    @staticmethod
    def write_json(path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def write_detector(self, features):
        self.write_json(self.detect / "metadata.json", {
            "stage": "subsidence_detection", "source_processing_directory": str(self.proc)
        })
        self.write_json(self.detect / "anomalies.geojson", {
            "type": "FeatureCollection", "features": features, "meta": {"source_run": self.run}
        })

    def test_populates_true_pair_coherence_and_history(self):
        zones = build_zones(run_name=self.run, data_root=self.root)
        self.assertEqual(len(zones), 1)
        z = zones[0]
        self.assertIsNone(z["risk_level"])  # review != risk
        self.assertTrue(z["id"].startswith("zone-"))
        self.assertEqual(z["geometry"]["type"], "Polygon")
        self.assertEqual([p["displacement_mm"] for p in z["cumulative_series"]], [0.0, -2.0, -4.0])
        self.assertTrue(z["cumulative_series"][0]["time"].endswith("02:59:41Z"))
        self.assertAlmostEqual(z["summary"]["latest_pair_mean_coherence"], .62, places=5)
        self.assertAlmostEqual(z["summary"]["cumulative_shift_mm"], -4, places=5)
        self.assertAlmostEqual(z["summary"]["los_velocity_mm_per_year"], -2/12*365.25, delta=.02)
        self.assertAlmostEqual(z["summary"]["acceleration_mm_per_year2"], 0, delta=.05)
        self.assertEqual(z["summary"]["latest_pair_secondary_time"], "2026-07-15T02:59:39Z")

    def test_original_coherence_unavailable_means_null_not_proxy(self):
        (self.raw / "manifest.json").unlink()
        zones = build_zones(run_name=self.run, data_root=self.root)
        self.assertIsNone(zones[0]["summary"]["latest_pair_mean_coherence"])

    def test_missing_common_pixels_means_null_not_fake_series(self):
        arr = np.load(self.proc / "displacement.npy")
        arr[1, :, :] = np.nan
        np.save(self.proc / "displacement.npy", arr)
        zones = build_zones(run_name=self.run, data_root=self.root)
        self.assertIsNone(zones[0]["cumulative_series"])
        self.assertIsNone(zones[0]["summary"]["cumulative_shift_mm"])
        self.assertIsNone(zones[0]["summary"]["los_velocity_mm_per_year"])

    def test_vertical_source_does_not_fabricate_los_history(self):
        meta = self.stage2.copy()
        meta["displacement"] = {**meta["displacement"], "component": "vertical"}
        self.write_json(self.proc / "metadata.json", meta)
        zones = build_zones(run_name=self.run, data_root=self.root)
        self.assertIsNone(zones[0]["cumulative_series"])
        self.assertIsNone(zones[0]["summary"]["cumulative_shift_mm"])

    def test_empty_detection_is_empty_success(self):
        self.write_detector([])
        self.assertEqual(build_zones(run_name=self.run, data_root=self.root), [])
        output = self.root / "api" / "zones.json"
        export_cache(run_name=self.run, data_root=self.root, output=output)
        with patch.dict(os.environ, {"NISAR_ZONES_FILE": str(output)}):
            self.assertEqual(get_zones(), [])

    def test_multipolygon_and_stable_id(self):
        mp = {"type": "MultiPolygon", "coordinates": [self.poly["coordinates"]]}
        self.feature["geometry"] = mp
        self.write_detector([self.feature])
        z = build_zones(run_name=self.run, data_root=self.root)[0]
        self.assertEqual(z["geometry"]["type"], "MultiPolygon")
        self.assertEqual(z["risk_level"], None)

    def test_missing_unbuilt_cache_is_unavailable(self):
        with patch.dict(os.environ, {"NISAR_ZONES_FILE": str(self.root / "missing.json")}):
            with self.assertRaises(ZoneCalculationUnavailable):
                get_zones()

    def test_malformed_geometry_raises_not_empty_success(self):
        self.feature["geometry"] = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]}
        self.write_detector([self.feature])
        with self.assertRaises(ValueError):
            build_zones(run_name=self.run, data_root=self.root)

    def test_wrong_run_rejected(self):
        self.write_json(self.detect / "metadata.json", {
            "stage": "subsidence_detection", "source_processing_directory": "data/processed/other_run"
        })
        with self.assertRaises(ValueError):
            build_zones(run_name=self.run, data_root=self.root)

    def test_cached_payload_is_runtime_only(self):
        output = self.root / "api" / "zones.json"
        export_cache(run_name=self.run, data_root=self.root, output=output)
        with patch.dict(os.environ, {"NISAR_ZONES_FILE": str(output)}):
            zones = get_zones()
        self.assertEqual(len(zones), 1)
        self.assertTrue(math.isfinite(zones[0]["summary"]["cumulative_shift_mm"]))


if __name__ == "__main__":
    unittest.main()
