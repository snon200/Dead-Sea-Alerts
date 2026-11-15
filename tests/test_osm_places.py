import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_osm_places import build_places


class OsmPlacesTests(unittest.TestCase):
    def test_build_places_prefers_english_and_preserves_coordinates(self):
        zone = {
            "id": "test",
            "geometry": {"type": "Polygon", "coordinates": [[[35, 31], [36, 31], [36, 32], [35, 31]]]},
        }
        element = {
            "type": "node", "id": 42, "lat": 31.5, "lon": 35.5,
            "tags": {"name": "Local", "name:en": "English", "place": "village"},
        }
        with tempfile.TemporaryDirectory() as directory:
            zones_path, out_path = Path(directory) / "zones.json", Path(directory) / "places.json"
            zones_path.write_text(json.dumps({"zones": [zone]}), encoding="utf-8")
            with patch("scripts.build_osm_places.fetch_overpass_query", return_value=[element]) as fetch:
                result = build_places(zones_path, out_path)
            self.assertEqual(result["places"], [{"id": 42, "name": "English", "lat": 31.5, "lon": 35.5}])
            self.assertEqual(json.loads(out_path.read_text(encoding="utf-8")), result)
            self.assertIn('node["place"', fetch.call_args.args[0])

    def test_empty_response_does_not_replace_existing_dataset(self):
        zone = {
            "id": "test",
            "geometry": {"type": "Polygon", "coordinates": [[[35, 31], [36, 31], [36, 32], [35, 31]]]},
        }
        with tempfile.TemporaryDirectory() as directory:
            zones_path, out_path = Path(directory) / "zones.json", Path(directory) / "places.json"
            zones_path.write_text(json.dumps({"zones": [zone]}), encoding="utf-8")
            out_path.write_text("existing", encoding="utf-8")
            with patch("scripts.build_osm_places.fetch_overpass_query", return_value=[]), self.assertRaises(ValueError):
                build_places(zones_path, out_path)
            self.assertEqual(out_path.read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
    unittest.main()
