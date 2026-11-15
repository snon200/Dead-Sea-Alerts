import http.client
import json
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from api.v1.osm import handler


class OsmApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def request(self, path="/api/v1/osm"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.getheader("Cache-Control"), json.loads(response.read())
        finally:
            connection.close()

    def test_reads_both_precomputed_files(self):
        infrastructure = {"schema_version": "3.0", "elements": {}, "coverage": {"type": "Polygon", "coordinates": []}}
        places = {"schema_version": "1.0", "bounds": [30, 34, 33, 37], "places": []}
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "osm-cache.json").write_text(json.dumps(infrastructure), encoding="utf-8")
            (root / "osm-places.json").write_text(json.dumps(places), encoding="utf-8")
            with patch("api.v1.osm.DATA_DIR", root):
                status, cache_control, payload = self.request("/api/v1/osm?test=1")
        self.assertEqual(status, 200)
        self.assertEqual(cache_control, "no-store")
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["infrastructure"], infrastructure)
        self.assertEqual(payload["places"], places)
        self.assertEqual(payload["pagination"]["offset"], 0)
        self.assertIsNone(payload["pagination"]["next_offset"])
        self.assertEqual(payload["pagination"]["total"], 0)

    def test_paginates_without_losing_elements_and_detects_changes(self):
        elements = {f"node/{i}": {"type": "node", "id": i, "tags": {"name": "x" * 100}}
                    for i in range(10)}
        infrastructure = {"schema_version": "3.0", "elements": elements, "coverage": {"type": "Polygon"}}
        places = {"schema_version": "1.0", "bounds": [30, 34, 33, 37], "places": []}
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "osm-cache.json").write_text(json.dumps(infrastructure), encoding="utf-8")
            (root / "osm-places.json").write_text(json.dumps(places), encoding="utf-8")
            with patch("api.v1.osm.DATA_DIR", root), patch("api.v1.osm.MAX_RESPONSE_BYTES", 800):
                status, _, first = self.request()
                self.assertEqual(status, 200)
                self.assertIsNotNone(first["pagination"]["next_offset"])
                self.assertLessEqual(len(json.dumps(first).encode()), 800)
                revision = first["pagination"]["revision"]
                combined = dict(first["infrastructure"]["elements"])
                offset = first["pagination"]["next_offset"]
                while offset is not None:
                    status, _, page = self.request(f"/api/v1/osm?offset={offset}&revision={revision}")
                    self.assertEqual(status, 200)
                    self.assertLessEqual(len(json.dumps(page).encode()), 800)
                    combined.update(page["infrastructure"]["elements"])
                    offset = page["pagination"]["next_offset"]
                self.assertEqual(combined, elements)
                (root / "osm-cache.json").write_text(json.dumps(infrastructure) + " ", encoding="utf-8")
                status, _, _ = self.request(f"/api/v1/osm?offset=1&revision={revision}")
                self.assertEqual(status, 409)
                for query in ("offset=-1", "offset=1", "offset=999&revision=invalid"):
                    status, _, _ = self.request(f"/api/v1/osm?{query}")
                    self.assertIn(status, (400, 409))

    def test_missing_file_returns_unavailable(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("api.v1.osm.DATA_DIR", root):
                for existing in (None, "osm-cache.json"):
                    if existing:
                        (root / existing).write_text("{}", encoding="utf-8")
                    status, _, payload = self.request()
                    self.assertEqual(status, 503)
                    self.assertEqual(payload["error"]["code"], "osm_data_unavailable")

    def test_invalid_or_nonfinite_data_returns_error(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "osm-cache.json"
            places = root / "osm-places.json"
            places.write_text(json.dumps({"schema_version": "1.0", "bounds": [30, 34, 33, 37], "places": []}), encoding="utf-8")
            with patch("api.v1.osm.DATA_DIR", root):
                for content in ('{"schema_version":"wrong"}', '{"schema_version":"3.0","elements":{},"coverage":{"type":"Polygon"},"value":NaN}'):
                    cache.write_text(content, encoding="utf-8")
                    status, _, payload = self.request()
                    self.assertEqual(status, 500)
                    self.assertEqual(payload["error"]["code"], "internal_error")

    def test_wrong_path_returns_not_found(self):
        status, _, payload = self.request("/api/v1/other")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
