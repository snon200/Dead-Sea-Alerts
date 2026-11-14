import http.client
import json
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

from api.v1.zones import handler


class ZonesApiTests(unittest.TestCase):
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

    def request(self, path="/api/v1/zones"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), json.loads(response.read())
        finally:
            connection.close()

    def test_unimplemented_calculations_report_unavailable(self):
        status, content_type, payload = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        self.assertEqual(payload["error"]["code"], "calculations_unavailable")

    def test_returns_calculated_zones_in_documented_envelope(self):
        spec = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text(encoding="utf-8"))
        example = spec["paths"]["/api/v1/zones"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
        with patch("api.v1.zones.get_zones", return_value=example["zones"]):
            status, _, payload = self.request("/api/v1/zones?test=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload, example)

    def test_empty_result_is_not_unavailable(self):
        with patch("api.v1.zones.get_zones", return_value=[]):
            status, _, payload = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"schema_version": "1.0", "zones": []})

    def test_calculation_failure_does_not_expose_details(self):
        with patch("api.v1.zones.get_zones", side_effect=ValueError("private detail")):
            status, _, payload = self.request()
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertNotIn("private detail", json.dumps(payload))

    def test_non_finite_data_is_an_error_not_invalid_json(self):
        with patch("api.v1.zones.get_zones", return_value=[{"value": float("nan")}]):
            status, _, payload = self.request()
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "internal_error")

    def test_invalid_result_is_not_reported_as_success(self):
        with patch("api.v1.zones.get_zones", return_value=None):
            status, _, payload = self.request()
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "internal_error")

    def test_other_path_is_not_the_zones_endpoint(self):
        status, _, payload = self.request("/api/v1/other")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
