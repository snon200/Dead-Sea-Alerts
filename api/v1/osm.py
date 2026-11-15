"""Serve precomputed OpenStreetMap infrastructure and place names."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from api.v1.zones import handler as ZonesHandler

LOG = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "api"
MAX_RESPONSE_BYTES = 3 * 1024 * 1024


def get_osm_data(offset: int = 0, expected_revision: str | None = None) -> dict:
    infrastructure_bytes = (DATA_DIR / "osm-cache.json").read_bytes()
    places_bytes = (DATA_DIR / "osm-places.json").read_bytes()
    revision = hashlib.sha256(infrastructure_bytes + places_bytes).hexdigest()
    if expected_revision is not None and expected_revision != revision:
        raise DatasetChanged("OSM datasets changed during pagination")
    infrastructure = json.loads(infrastructure_bytes)
    places = json.loads(places_bytes)
    if (
        not isinstance(infrastructure, dict)
        or infrastructure.get("schema_version") != "3.0"
        or not isinstance(infrastructure.get("elements"), dict)
        or not isinstance(infrastructure.get("coverage"), dict)
        or infrastructure["coverage"].get("type") != "Polygon"
        or not isinstance(places, dict)
        or places.get("schema_version") != "1.0"
        or not isinstance(places.get("bounds"), list)
        or len(places["bounds"]) != 4
        or not isinstance(places.get("places"), list)
    ):
        raise ValueError("Invalid precomputed OSM data")
    elements = list(infrastructure["elements"].items())
    if offset > len(elements):
        raise InvalidPage("OSM page offset exceeds the dataset")
    page = dict(infrastructure)
    page["elements"] = {}
    payload = {
        "schema_version": "1.0", "infrastructure": page, "places": places,
        "pagination": {"offset": offset, "next_offset": None, "total": len(elements), "revision": revision},
    }
    size = len(json.dumps(payload, allow_nan=False).encode("utf-8")) + 32
    for key, element in elements[offset:]:
        element_size = len(json.dumps({key: element}, allow_nan=False).encode("utf-8"))
        if size + element_size > MAX_RESPONSE_BYTES:
            break
        page["elements"][key] = element
        size += element_size
    next_offset = offset + len(page["elements"])
    if size > MAX_RESPONSE_BYTES or (next_offset == offset and offset < len(elements)):
        raise ValueError("OSM data cannot fit within the response size limit")
    if next_offset < len(elements):
        payload["pagination"]["next_offset"] = next_offset
    return payload


class DatasetChanged(Exception):
    pass


class InvalidPage(Exception):
    pass


class handler(ZonesHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path != "/api/v1/osm":
            self._send_json(404, {"error": {"code": "not_found", "message": "Endpoint not found."}})
            return
        query = parse_qs(url.query, keep_blank_values=True)
        offsets = query.get("offset", ["0"])
        revisions = query.get("revision", [])
        if (len(offsets) != 1 or len(offsets[0]) > 12 or not offsets[0].isascii() or not offsets[0].isdigit() or
            len(revisions) > 1 or (offsets[0] != "0" and not revisions)):
            self._send_json(400, {"error": {"code": "invalid_page", "message": "Invalid OSM page parameters."}})
            return
        try:
            payload = get_osm_data(int(offsets[0]), revisions[0] if revisions else None)
            json.dumps(payload, allow_nan=False)
        except DatasetChanged:
            self._send_json(409, {"error": {
                "code": "osm_data_changed", "message": "OSM data changed; restart from the first page.",
            }})
            return
        except InvalidPage:
            self._send_json(400, {"error": {"code": "invalid_page", "message": "Invalid OSM page parameters."}})
            return
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
