import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_osm_cache import _overpass_elements, build_cache


class OsmCacheTests(unittest.TestCase):
    def test_build_keeps_features_in_the_region_independent_of_zones(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[[35, 31], [36, 31], [36, 32], [35, 32], [35, 31]]],
        }
        features = [
            {"type": "node", "id": 1, "lat": 31.5, "lon": 35.5, "tags": {"power": "substation"}},
            {"type": "node", "id": 2, "lat": 33, "lon": 35.5, "tags": {"power": "substation"}},
            {"type": "way", "id": 3, "geometry": [{"lat": 31.5, "lon": 34}, {"lat": 31.5, "lon": 37}], "tags": {"highway": "road"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            area_path = Path(directory) / "area.geojson"
            out_path = Path(directory) / "cache.json"
            area_path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"geometry": geometry}]}), encoding="utf-8")
            with patch("scripts.build_osm_cache.fetch_overpass", return_value=features):
                cache = build_cache(area_path, out_path)
            self.assertEqual(cache["schema_version"], "3.0")
            self.assertEqual(cache["coverage"], geometry)
            self.assertEqual(set(cache["elements"]), {"node/1", "way/3"})
            self.assertEqual(json.loads(out_path.read_text(encoding="utf-8")), cache)

    def test_invalid_source_does_not_replace_cache(self):
        area = {"type": "FeatureCollection", "features": [{"geometry": {
            "type": "Polygon", "coordinates": [[[35, 31], [36, 31], [36, 32], [35, 31]]]
        }}]}
        with tempfile.TemporaryDirectory() as directory:
            area_path, out_path = Path(directory) / "area.geojson", Path(directory) / "cache.json"
            area_path.write_text(json.dumps(area), encoding="utf-8")
            out_path.write_text("existing", encoding="utf-8")
            with patch("scripts.build_osm_cache.fetch_overpass", return_value=[
                {"type": "way", "id": 1, "tags": {"highway": "road"}, "geometry": []}
            ]), self.assertRaises(ValueError):
                build_cache(area_path, out_path)
            self.assertEqual(out_path.read_text(encoding="utf-8"), "existing")

    def test_incomplete_overpass_response_is_not_cached_as_empty(self):
        for payload in ({}, {"elements": [], "remark": "runtime error: timeout"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _overpass_elements(payload)
        self.assertEqual(_overpass_elements({"elements": []}), [])


if __name__ == "__main__":
    unittest.main()
