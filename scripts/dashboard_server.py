"""Serve the dashboard and its API locally, optionally using synthetic zones."""

import argparse
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.v1.zones import handler as ZonesHandler


DEMO_DATA = [
    ("ein-gedi", "high", 31.41790, 35.38035, -42.6, -18.4, -3.2, 0.81, [0, -1.7, -4.0, -6.8, -10.0, -13.7, -18.4]),
    ("mineral-beach", "elevated", 31.71585, 35.45403, -26.8, -11.2, -1.4, 0.73, [0, -0.8, -2.0, -3.7, -5.7, -8.2, -11.2]),
    ("zeelim-south", "watch", 31.23514, 35.36689, -16.3, -6.9, -0.5, 0.68, [0, -0.4, -1.1, -2.0, -3.2, -4.8, -6.9]),
    ("south-basin", "low", 31.06641, 35.37508, -3.1, -1.3, 0.1, 0.77, [0, -0.2, -0.4, -0.6, -0.8, -1.0, -1.3]),
    ("ein-gedi-kibbutz", "elevated", 31.45250, 35.38600, -23.4, -10.1, -1.2, 0.79, [0, -0.7, -1.9, -3.3, -5.2, -7.4, -10.1]),
    ("ein-bokek", "watch", 31.19844, 35.36365, -14.8, -6.4, -0.6, 0.75, [0, -0.4, -1.0, -1.9, -3.1, -4.6, -6.4]),
    ("lisan-pump", "elevated", 31.32910, 35.40290, -21.2, -9.1, -1.0, 0.76, [0, -0.6, -1.7, -3.0, -4.7, -6.8, -9.1]),
    ("ein-gedi-reservoir", "watch", 31.44490, 35.38720, -12.4, -5.3, -0.4, 0.75, [0, -0.3, -0.8, -1.5, -2.5, -3.8, -5.3]),
    ("north-well", "watch", 31.74222, 35.41704, -10.2, -4.5, -0.3, 0.74, [0, -0.3, -0.7, -1.3, -2.1, -3.2, -4.5]),
]
DEMO_DATES = [f"2026-{month:02d}-01T00:00:00Z" for month in range(1, 8)]


def demo_zones():
    zones = []
    for zone_id, risk, lat, lon, velocity, shift, acceleration, coherence, series in DEMO_DATA:
        ring = [
            [lon, lat + 0.0025], [lon + 0.0028, lat], [lon, lat - 0.0025],
            [lon - 0.0028, lat], [lon, lat + 0.0025],
        ]
        zones.append({
            "id": zone_id,
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "risk_level": risk,
            "summary": {
                "los_velocity_mm_per_year": velocity,
                "cumulative_shift_mm": shift,
                "acceleration_mm_per_year2": acceleration,
                "latest_pair_mean_coherence": coherence,
                "latest_pair_reference_time": DEMO_DATES[-2],
                "latest_pair_secondary_time": DEMO_DATES[-1],
            },
            "cumulative_series": [
                {"time": date, "displacement_mm": value}
                for date, value in zip(DEMO_DATES, series)
            ],
        })
    return zones


class DashboardHandler(SimpleHTTPRequestHandler, ZonesHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if urlsplit(self.path).path == "/api/v1/zones":
            if self.server.zone_source == "demo":
                self._send_json(200, {"schema_version": "1.0", "zones": demo_zones()}, source="demo")
            else:
                ZonesHandler.do_GET(self)
            return
        super().do_GET()


def create_server(port, source=None):
    if source is None:
        source = os.environ.get("ZONE_DATA_SOURCE", "api")
    if source not in ("api", "demo"):
        raise ValueError(f"Invalid ZONE_DATA_SOURCE {source!r}; use 'api' or 'demo'")
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    server.zone_source = source
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    with create_server(args.port) as server:
        print(f"Dashboard: http://127.0.0.1:{server.server_port}/ (zone source: {server.zone_source})", flush=True)
        server.serve_forever()
