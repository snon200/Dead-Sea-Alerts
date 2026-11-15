#!/usr/bin/env python3
"""Build an OSM place-name dataset around the fixed regional study area."""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .build_osm_cache import _zone_bbox, _zone_polygons, fetch_overpass_query, load_study_area
else:
    from build_osm_cache import _zone_bbox, _zone_polygons, fetch_overpass_query, load_study_area


def read_extract_places(paths: list[Path], bounds: list[float]) -> list[dict]:
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError("Reading PBF extracts requires the 'osmium' Python package") from exc

    class PlaceHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.places = {}

        def node(self, node):
            if not node.tags or not node.location.valid():
                return
            lat, lon = node.location.lat, node.location.lon
            if not (bounds[0] <= lat <= bounds[2] and bounds[1] <= lon <= bounds[3]):
                return
            tags = dict(node.tags)
            if tags.get("place") not in ("city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "locality"):
                return
            self.places[node.id] = {"type": "node", "id": node.id, "lat": lat, "lon": lon, "tags": tags}

    reader = PlaceHandler()
    for path in paths:
        print(f"Reading OSM place names from {path}...")
        reader.apply_file(str(path))
    return list(reader.places.values())


def build_places(area_path: Path, out_path: Path, pbf_paths: list[Path] | None = None) -> dict:
    geometry = load_study_area(area_path)
    box = _zone_bbox(_zone_polygons({"id": "study area", "geometry": geometry}))
    bounds = [
        math.floor((box[0] - 0.1) * 1e6) / 1e6,
        math.floor((box[1] - 0.12) * 1e6) / 1e6,
        math.ceil((box[2] + 0.1) * 1e6) / 1e6,
        math.ceil((box[3] + 0.12) * 1e6) / 1e6,
    ]
    bbox = ",".join(f"{coordinate:.6f}" for coordinate in bounds)
    query = (
        '[out:json][timeout:60];'
        f'node["place"~"^(city|town|village|hamlet|suburb|neighbourhood|quarter|locality)$"]'
        f'["name"]({bbox});out body;'
    )
    elements = read_extract_places(pbf_paths, bounds) if pbf_paths else fetch_overpass_query(query)
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
    parser.add_argument("--area", default="data/api/osm-study-area.geojson")
    parser.add_argument("--out", default="data/api/osm-places.json")
    parser.add_argument("--pbf", action="append", default=[], help="OSM PBF extract; repeat for each covering extract")
    args = parser.parse_args()
    build_places(Path(args.area), Path(args.out), [Path(path) for path in args.pbf])


if __name__ == "__main__":
    main()
