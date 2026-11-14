import http.client
import json
import os
import threading
import unittest
from unittest.mock import patch

from scripts.dashboard_server import create_server


class DashboardServerTests(unittest.TestCase):
    def request(self, source=None):
        server = create_server(0, source)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request("GET", "/api/v1/zones")
                response = connection.getresponse()
                return response.status, response.getheader("X-Zone-Source"), json.loads(response.read())
            finally:
                connection.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_default_uses_api(self):
        with patch.dict(os.environ, {}, clear=True), patch("api.v1.zones.get_zones", return_value=[]):
            status, source, payload = self.request()
        self.assertEqual((status, source, payload), (200, None, {"schema_version": "1.0", "zones": []}))

    def test_local_demo_returns_labeled_synthetic_zones(self):
        with patch.dict(os.environ, {"ZONE_DATA_SOURCE": "demo"}, clear=True):
            status, source, payload = self.request()
        self.assertEqual((status, source), (200, "demo"))
        self.assertEqual(len(payload["zones"]), 9)
        self.assertEqual(payload["zones"][0]["id"], "ein-gedi")
        self.assertEqual(payload["zones"][0]["summary"]["cumulative_shift_mm"], -18.4)

    def test_invalid_source_fails_explicitly(self):
        with patch.dict(os.environ, {"ZONE_DATA_SOURCE": "other"}, clear=True):
            with self.assertRaisesRegex(ValueError, "ZONE_DATA_SOURCE"):
                create_server(0)

    def test_deployed_endpoint_ignores_local_demo_switch(self):
        from api.v1.zones import handler
        from http.server import HTTPServer

        with patch.dict(os.environ, {"ZONE_DATA_SOURCE": "demo", "VERCEL_ENV": "production"}, clear=True), \
                patch("api.v1.zones.get_zones", return_value=[]):
            server = HTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    connection.request("GET", "/api/v1/zones")
                    response = connection.getresponse()
                    self.assertIsNone(response.getheader("X-Zone-Source"))
                    self.assertEqual(json.loads(response.read())["zones"], [])
                finally:
                    connection.close()
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
