import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_osm_places import build_places


class OsmPlacesTests(unittest.TestCase):
    def test_build_places_prefers_english_and_preserves_coordinates(self):
        area = {"type": "FeatureCollection", "features": [{
            "geometry": {"type": "Polygon", "coordinates": [[[35, 31], [36, 31], [36, 32], [35, 31]]]}
        }]}
        element = {
            "type": "node", "id": 42, "lat": 31.5, "lon": 35.5,
            "tags": {"name": "Local", "name:en": "English", "place": "village"},
        }
        river = {
            "type": "way", "id": 42, "tags": {"name": "River", "waterway": "river"},
            "geometry": [{"lat": 31.4, "lon": 35.4}, {"lat": 31.6, "lon": 35.6}],
        }
        with tempfile.TemporaryDirectory() as directory:
            zones_path, out_path = Path(directory) / "area.geojson", Path(directory) / "places.json"
            zones_path.write_text(json.dumps(area), encoding="utf-8")
            with patch("scripts.build_osm_places.fetch_overpass_query", return_value=[element, river]) as fetch:
                result = build_places(zones_path, out_path)
            self.assertEqual(result["places"], [
                {"id": 42, "name": "English", "lat": 31.5, "lon": 35.5},
                {"id": 42, "name": "River", "geometry": [[31.4, 35.4], [31.6, 35.6]]},
            ])
            self.assertEqual(json.loads(out_path.read_text(encoding="utf-8")), result)
            self.assertIn('nw["place"', fetch.call_args.args[0])
            self.assertIn('nw["waterway"', fetch.call_args.args[0])
            self.assertIn('nw["natural"', fetch.call_args.args[0])
            self.assertLessEqual(result["bounds"][0], 30.9)
            self.assertGreaterEqual(result["bounds"][2], 32.1)

    def test_empty_response_does_not_replace_existing_dataset(self):
        area = {"type": "FeatureCollection", "features": [{
            "geometry": {"type": "Polygon", "coordinates": [[[35, 31], [36, 31], [36, 32], [35, 31]]]}
        }]}
        with tempfile.TemporaryDirectory() as directory:
            zones_path, out_path = Path(directory) / "area.geojson", Path(directory) / "places.json"
            zones_path.write_text(json.dumps(area), encoding="utf-8")
            out_path.write_text("existing", encoding="utf-8")
            with patch("scripts.build_osm_places.fetch_overpass_query", return_value=[]), self.assertRaises(ValueError):
                build_places(zones_path, out_path)
            self.assertEqual(out_path.read_text(encoding="utf-8"), "existing")

    def test_incomplete_named_way_does_not_replace_dataset(self):
        area = {"type": "FeatureCollection", "features": [{
            "geometry": {"type": "Polygon", "coordinates": [[[35, 31], [36, 31], [36, 32], [35, 31]]]}
        }]}
        way = {"type": "way", "id": 1, "tags": {"name": "Canal", "waterway": "canal"}}
        with tempfile.TemporaryDirectory() as directory:
            area_path, out_path = Path(directory) / "area.geojson", Path(directory) / "places.json"
            area_path.write_text(json.dumps(area), encoding="utf-8")
            out_path.write_text("existing", encoding="utf-8")
            with patch("scripts.build_osm_places.fetch_overpass_query", return_value=[way]), self.assertRaises(ValueError):
                build_places(area_path, out_path)
            self.assertEqual(out_path.read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
    unittest.main()
