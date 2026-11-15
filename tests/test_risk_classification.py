"""Pure-Python unit tests for exploratory risk_level classification."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.zone_service import _validate_zone, classify_risk_level


def _square_polygon(span_deg_lon: float, span_deg_lat: float = 0.003) -> dict:
    lon0, lat0 = 35.4, 31.5
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon0, lat0],
            [lon0 + span_deg_lon, lat0],
            [lon0 + span_deg_lon, lat0 + span_deg_lat],
            [lon0, lat0 + span_deg_lat],
            [lon0, lat0],
        ]],
    }


def _series(displacements: list[float]) -> list[dict]:
    dates = [
        "2026-06-01T00:00:00Z",
        "2026-06-13T00:00:00Z",
        "2026-06-25T00:00:00Z",
        "2026-07-07T00:00:00Z",
        "2026-07-19T00:00:00Z",
    ]
    return [
        {"time": t, "displacement_mm": d}
        for t, d in zip(dates, displacements)
    ]


def _summary(velocity: float | None, coherence: float | None = 0.65) -> dict:
    return {
        "los_velocity_mm_per_year": velocity,
        "cumulative_shift_mm": -10.0,
        "acceleration_mm_per_year2": 0.0,
        "latest_pair_mean_coherence": coherence,
        "latest_pair_reference_time": "2026-07-07T00:00:00Z",
        "latest_pair_secondary_time": "2026-07-19T00:00:00Z",
    }


class RiskClassificationTests(unittest.TestCase):
    def setUp(self):
        self.geom = _square_polygon(0.003, 0.003)  # ~285 m x ~332 m
        self.steady_series = _series([0.0, -1.0, -2.0, -3.0, -4.0])
        self.accelerating_series = _series([0.0, -1.0, -2.5, -5.0, -9.0])

    def test_velocity_tiers(self):
        self.assertEqual(classify_risk_level(_summary(-120.0), self.steady_series, self.geom), "high")
        self.assertEqual(classify_risk_level(_summary(105.0), self.steady_series, self.geom), "high")
        self.assertEqual(classify_risk_level(_summary(-60.0), self.accelerating_series, self.geom), "elevated")
        self.assertEqual(classify_risk_level(_summary(-60.0), self.steady_series, self.geom), "watch")
        self.assertEqual(classify_risk_level(_summary(-25.0), self.steady_series, self.geom), "watch")
        self.assertEqual(classify_risk_level(_summary(-15.0), self.steady_series, self.geom), "low")
        self.assertEqual(classify_risk_level(_summary(-12.5), self.steady_series, self.geom), "low")
        self.assertEqual(classify_risk_level(_summary(5.0), self.steady_series, self.geom), "low")

    def test_quality_gates_return_none(self):
        self.assertIsNone(classify_risk_level(_summary(None), self.steady_series, self.geom))
        self.assertIsNone(classify_risk_level(_summary(-120.0, coherence=None), self.steady_series, self.geom))
        self.assertIsNone(classify_risk_level(_summary(-120.0, coherence=0.29), self.steady_series, self.geom))
        self.assertIsNone(classify_risk_level(_summary(-120.0), self.steady_series[:3], self.geom))
        small_geom = _square_polygon(0.001, 0.001)  # ~95 m x ~111 m < 200 m
        self.assertIsNone(classify_risk_level(_summary(-120.0), self.steady_series, small_geom))

    def test_zones_json_contract_valid(self):
        payload = json.loads(Path("data/api/zones.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertTrue(payload["source_run"])
        self.assertTrue(payload["zones"])
        for zone in payload["zones"]:
            _validate_zone(zone)


if __name__ == "__main__":
    unittest.main()
