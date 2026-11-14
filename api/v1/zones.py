"""Vercel HTTP endpoint for calculated zones."""

import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from src.zone_service import ZoneCalculationUnavailable, get_zones

LOG = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/api/v1/zones":
            self._send_json(404, {"error": {"code": "not_found", "message": "Endpoint not found."}})
            return

        try:
            zones = get_zones()
            if not isinstance(zones, list):
                raise TypeError("Zone calculations must return a list")
            payload = {"schema_version": "1.0", "zones": zones}
            json.dumps(payload, allow_nan=False)
        except ZoneCalculationUnavailable:
            LOG.warning("Zone calculations are not connected to the API")
            self._send_json(503, {"error": {
                "code": "calculations_unavailable",
                "message": "Zone calculations are not available yet.",
            }})
            return
        except Exception:
            LOG.exception("Zone calculation failed")
            self._send_json(500, {"error": {
                "code": "internal_error",
                "message": "Zone calculations failed.",
            }})
            return

        self._send_json(200, payload)
