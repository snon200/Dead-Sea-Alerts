import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_osm_cache import _overpass_elements, build_cache


class OsmCacheTests(unittest.TestCase):
    def test_build_records_geometry_and_intersections(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[[35, 31], [36, 31], [36, 32], [35, 32], [35, 31]]],
        }
        zones = [{"id": "inside", "geometry": geometry}]
        features = [
            {"type": "node", "id": 1, "lat": 31.5, "lon": 35.5, "tags": {"power": "substation"}},
            {"type": "node", "id": 2, "lat": 33, "lon": 35.5, "tags": {"power": "substation"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            zones_path = Path(directory) / "zones.json"
            out_path = Path(directory) / "cache.json"
            zones_path.write_text(json.dumps({"zones": zones}), encoding="utf-8")
            with patch("scripts.build_osm_cache.fetch_overpass", return_value=features):
                cache = build_cache(zones_path, out_path)
            self.assertEqual(cache["schema_version"], "2.0")
            self.assertEqual(cache["zones"]["inside"], {"geometry": geometry, "elements": ["node/1"]})
            self.assertEqual(json.loads(out_path.read_text(encoding="utf-8")), cache)

    def test_incomplete_overpass_response_is_not_cached_as_empty(self):
        for payload in ({}, {"elements": [], "remark": "runtime error: timeout"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _overpass_elements(payload)
        self.assertEqual(_overpass_elements({"elements": []}), [])


if __name__ == "__main__":
    unittest.main()
