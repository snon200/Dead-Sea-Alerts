"""Serve precomputed OpenStreetMap infrastructure and place names."""

import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

from api.v1.zones import handler as ZonesHandler

LOG = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "api"


def get_osm_data() -> dict:
    with (DATA_DIR / "osm-cache.json").open(encoding="utf-8") as file:
        infrastructure = json.load(file)
    with (DATA_DIR / "osm-places.json").open(encoding="utf-8") as file:
        places = json.load(file)
    if (
        not isinstance(infrastructure, dict)
        or infrastructure.get("schema_version") != "2.0"
        or not isinstance(infrastructure.get("elements"), dict)
        or not isinstance(infrastructure.get("zones"), dict)
        or not isinstance(places, dict)
        or places.get("schema_version") != "1.0"
        or not isinstance(places.get("bounds"), list)
        or len(places["bounds"]) != 4
        or not isinstance(places.get("places"), list)
    ):
        raise ValueError("Invalid precomputed OSM data")
    return {"schema_version": "1.0", "infrastructure": infrastructure, "places": places}


class handler(ZonesHandler):
    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/api/v1/osm":
            self._send_json(404, {"error": {"code": "not_found", "message": "Endpoint not found."}})
            return
        try:
            payload = get_osm_data()
            json.dumps(payload, allow_nan=False)
        except FileNotFoundError:
            LOG.warning("Precomputed OSM data is not deployed")
            self._send_json(503, {"error": {
                "code": "osm_data_unavailable",
                "message": "Precomputed OSM data is not available.",
            }})
            return
        except Exception:
            LOG.exception("Precomputed OSM data could not be loaded")
            self._send_json(500, {"error": {
                "code": "internal_error",
                "message": "Precomputed OSM data could not be loaded.",
            }})
            return
        self._send_json(200, payload)
