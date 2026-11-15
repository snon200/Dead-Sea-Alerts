#!/usr/bin/env python3
"""Build a small OSM place-name dataset around the current zones."""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .build_osm_cache import _zone_bbox, _zone_polygons, fetch_overpass_query
else:
    from build_osm_cache import _zone_bbox, _zone_polygons, fetch_overpass_query


def build_places(zones_path: Path, out_path: Path) -> dict:
    zones = json.loads(zones_path.read_text(encoding="utf-8"))["zones"]
    if not zones:
        raise ValueError("Cannot build place names without zones")
    boxes = [_zone_bbox(_zone_polygons(zone)) for zone in zones]
    bounds = [
        math.floor((min(box[0] for box in boxes) - 0.1) * 1e6) / 1e6,
        math.floor((min(box[1] for box in boxes) - 0.12) * 1e6) / 1e6,
        math.ceil((max(box[2] for box in boxes) + 0.1) * 1e6) / 1e6,
        math.ceil((max(box[3] for box in boxes) + 0.12) * 1e6) / 1e6,
    ]
    bbox = ",".join(f"{coordinate:.6f}" for coordinate in bounds)
    query = (
        '[out:json][timeout:60];'
        f'node["place"~"^(city|town|village|hamlet|suburb|neighbourhood|quarter|locality)$"]'
        f'["name"]({bbox});out body;'
    )
    elements = fetch_overpass_query(query)
    places = []
    for element in elements:
        tags = element.get("tags", {})
        name = tags.get("name:en") or tags.get("int_name") or tags.get("name")
        if element.get("type") == "node" and isinstance(name, str) and name.strip():
            places.append({"id": element["id"], "name": name.strip(), "lat": element["lat"], "lon": element["lon"]})
    if not places:
        raise ValueError("Overpass returned no named places")
    places.sort(key=lambda place: place["id"])
    result = {
        "schema_version": "1.0",
        "source": "OpenStreetMap",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bounds": bounds,
        "places": places,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(places)} OSM places to {out_path}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zones", default="data/api/zones.json")
    parser.add_argument("--out", default="data/api/osm-places.json")
    args = parser.parse_args()
    build_places(Path(args.zones), Path(args.out))


if __name__ == "__main__":
    main()
