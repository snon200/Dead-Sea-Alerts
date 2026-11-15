#!/usr/bin/env python3
"""Build a regional OpenStreetMap infrastructure dataset independent of zones."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def _zone_polygons(zone: dict) -> list[list[list[tuple[float, float]]]]:
    """Return zone polygons as [(lat, lon), ...] rings, matching index.html."""
    geom = zone["geometry"]
    if geom["type"] not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"Unsupported geometry for zone {zone['id']}")
    raw_polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return [
        [[(pt[1], pt[0]) for pt in ring] for ring in rings]
        for rings in raw_polys
    ]


def _zone_bbox(polygons: list[list[list[tuple[float, float]]]]) -> tuple[float, float, float, float]:
    lats = [pt[0] for rings in polygons for ring in rings for pt in ring]
    lons = [pt[1] for rings in polygons for ring in rings for pt in ring]
    return min(lats), min(lons), max(lats), max(lons)


def _point_on_segment(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    cross = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
    return (
        abs(cross) < 1e-10
        and min(a[0], b[0]) - 1e-10 <= point[0] <= max(a[0], b[0]) + 1e-10
        and min(a[1], b[1]) - 1e-10 <= point[1] <= max(a[1], b[1]) + 1e-10
    )


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i, b in enumerate(polygon):
        a = polygon[j]
        if _point_on_segment(point, a, b):
            return True
        if (a[0] > point[0]) != (b[0] > point[0]) and point[1] < (b[1] - a[1]) * (point[0] - a[0]) / (b[0] - a[0]) + a[1]:
            inside = not inside
        j = i
    return inside


def _segments_intersect(
    a: tuple[float, float], b: tuple[float, float],
    c: tuple[float, float], d: tuple[float, float],
) -> bool:
    def direction(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    if (
        _point_on_segment(c, a, b)
        or _point_on_segment(d, a, b)
        or _point_on_segment(a, c, d)
        or _point_on_segment(b, c, d)
    ):
        return True
    ab_c = direction(a, b, c)
    ab_d = direction(a, b, d)
    cd_a = direction(c, d, a)
    cd_b = direction(c, d, b)
    return ((ab_c > 0 and ab_d < 0) or (ab_c < 0 and ab_d > 0)) and (
        (cd_a > 0 and cd_b < 0) or (cd_a < 0 and cd_b > 0)
    )


def _point_in_zone(point: tuple[float, float], polygons: list[list[list[tuple[float, float]]]]) -> bool:
    for rings in polygons:
        if not _point_in_polygon(point, rings[0]):
            continue
        in_hole = False
        for ring in rings[1:]:
            on_edge = any(
                _point_on_segment(point, ring[i], ring[(i + 1) % len(ring)])
                for i in range(len(ring))
            )
            if not on_edge and _point_in_polygon(point, ring):
                in_hole = True
                break
        if not in_hole:
            return True
    return False


def _feature_intersects_zone(
    element: dict,
    polygons: list[list[list[tuple[float, float]]]],
    z_bbox: tuple[float, float, float, float],
) -> bool:
    if element["type"] == "node":
        pt = (element["lat"], element["lon"])
        if not (z_bbox[0] <= pt[0] <= z_bbox[2] and z_bbox[1] <= pt[1] <= z_bbox[3]):
            return False
        return _point_in_zone(pt, polygons)

    pts = [(p["lat"], p["lon"]) for p in element["geometry"]]
    if not pts:
        return False
    # Fast bbox reject
    el_s = min(p[0] for p in pts)
    el_n = max(p[0] for p in pts)
    el_w = min(p[1] for p in pts)
    el_e = max(p[1] for p in pts)
    if el_n < z_bbox[0] or el_s > z_bbox[2] or el_e < z_bbox[1] or el_w > z_bbox[3]:
        return False

    if any(_point_in_zone(p, polygons) for p in pts):
        return True
    for i in range(1, len(pts)):
        p0, p1 = pts[i - 1], pts[i]
        for rings in polygons:
            for ring in rings:
                for j in range(len(ring)):
                    if _segments_intersect(p0, p1, ring[j], ring[(j + 1) % len(ring)]):
                        return True
    if len(pts) >= 4 and pts[0] == pts[-1]:
        if any(_point_in_polygon(rings[0][0], pts) for rings in polygons):
            return True
    return False


def _matches_supported_layer(element: dict) -> bool:
    tags = element.get("tags") or {}
    if element["type"] == "way" and tags.get("highway"):
        return True
    if tags.get("man_made") == "pumping_station":
        return True
    if element["type"] == "way" and tags.get("building"):
        return True
    if element["type"] == "way" and tags.get("bridge") and tags.get("bridge") != "no":
        return True
    if tags.get("power") in ("line", "minor_line", "substation", "plant", "generator"):
        return True
    if (
        tags.get("man_made") in ("pipeline", "water_well", "water_works", "water_tower", "wastewater_plant")
        or tags.get("water") == "reservoir"
        or tags.get("landuse") == "reservoir"
    ):
        return True
    if (
        tags.get("tourism") in ("hotel", "guest_house")
        or tags.get("amenity") in ("school", "kindergarten", "hospital", "clinic")
        or tags.get("building") in ("hotel", "school", "hospital")
    ):
        return True
    # Critical & sensitive infrastructure layers
    if tags.get("waterway") == "dam" or tags.get("man_made") in ("dyke", "embankment"):
        return True
    if (
        tags.get("landuse") in ("industrial", "salt_pond", "quarry")
        or tags.get("man_made") in ("works", "storage_tank")
        or tags.get("amenity") == "fuel"
    ):
        return True
    if (
        tags.get("amenity") in ("fire_station", "police")
        or tags.get("aeroway") == "helipad"
        or tags.get("man_made") in ("mast", "tower")
    ):
        return True
    if tags.get("natural") == "sinkhole" or bool(tags.get("hazard")):
        return True
    return False


def _overpass_elements(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("remark") or not isinstance(payload.get("elements"), list):
        raise ValueError("Incomplete or invalid Overpass response")
    return payload["elements"]


def fetch_overpass_query(query: str) -> list[dict]:
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_err = None
    for endpoint in OVERPASS_ENDPOINTS:
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={"User-Agent": "DeadSeaAlertsCacheBuilder/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=75) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return _overpass_elements(payload)
        except Exception as exc:
            print(f"Overpass urllib {endpoint} failed: {exc}", file=sys.stderr)
            last_err = exc
        try:
            proc = subprocess.run(
                [
                    "curl", "-sS", "-f", "--max-time", "75",
                    "-A", "DeadSeaAlertsCacheBuilder/1.0",
                    "--data-urlencode", f"data={query}",
                    endpoint,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(proc.stdout)
            return _overpass_elements(payload)
        except Exception as exc:
            print(f"Overpass curl {endpoint} failed: {exc}", file=sys.stderr)
            last_err = exc
    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


def fetch_overpass(south: float, west: float, north: float, east: float) -> list[dict]:
    bbox = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    base_query = f"""[out:json][timeout:60];
(
  way["highway"]({bbox});
  way["building"]({bbox});
  node["building"~"^(hotel|school|hospital)$"]({bbox});
  way["bridge"]["bridge"!="no"]({bbox});
  node["man_made"~"^(pumping_station|pipeline|water_well|water_works|water_tower|wastewater_plant)$"]({bbox});
  way["man_made"~"^(pumping_station|pipeline|water_well|water_works|water_tower|wastewater_plant)$"]({bbox});
  node["power"~"^(line|minor_line|substation)$"]({bbox});
  way["power"~"^(line|minor_line|substation)$"]({bbox});
  node["water"="reservoir"]({bbox});
  way["water"="reservoir"]({bbox});
  way["landuse"="reservoir"]({bbox});
  node["tourism"~"^(hotel|guest_house)$"]({bbox});
  way["tourism"~"^(hotel|guest_house)$"]({bbox});
  node["amenity"~"^(school|kindergarten|hospital|clinic)$"]({bbox});
  way["amenity"~"^(school|kindergarten|hospital|clinic)$"]({bbox});
);
out body geom;"""
    critical_query = f"""[out:json][timeout:45];
(
  nw["man_made"~"^(dyke|embankment|works|storage_tank|mast|tower)$"]({bbox});
  nw["waterway"="dam"]({bbox});
  nw["power"~"^(plant|generator)$"]({bbox});
  way["landuse"~"^(industrial|salt_pond|quarry)$"]({bbox});
  nw["amenity"~"^(fire_station|police|fuel)$"]({bbox});
  nw["aeroway"="helipad"]({bbox});
  nw["natural"="sinkhole"]({bbox});
  nw["hazard"]({bbox});
);
out body geom;"""
    merged: dict[tuple[str, int], dict] = {}
    for q in (base_query, critical_query):
        for el in fetch_overpass_query(q):
            merged[(el["type"], el["id"])] = el
    return list(merged.values())


def load_study_area(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or not isinstance(features, list) or len(features) != 1:
        raise ValueError("Study area must contain one GeoJSON feature")
    geometry = features[0].get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        raise ValueError("Study area must be a GeoJSON Polygon")
    rings = geometry.get("coordinates")
    if not isinstance(rings, list) or len(rings) != 1:
        raise ValueError("Study area must have one exterior ring and no holes")
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
            raise ValueError("Study area has an invalid ring")
        for point in ring:
            if (not isinstance(point, list) or len(point) != 2 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point) or
                abs(point[0]) > 180 or abs(point[1]) > 90):
                raise ValueError("Study area has an invalid coordinate")
    return geometry


def read_extracts(paths: list[Path], bbox: tuple[float, float, float, float]) -> list[dict]:
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError("Reading PBF extracts requires the 'osmium' Python package") from exc

    class ExtractHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.elements = {}

        def node(self, node):
            if not node.tags or not node.location.valid():
                return
            lat, lon = node.location.lat, node.location.lon
            if not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
                return
            el = {"type": "node", "id": node.id, "lat": lat, "lon": lon, "tags": dict(node.tags)}
            if _matches_supported_layer(el):
                self.elements[("node", node.id)] = el

        def way(self, way):
            if not way.tags:
                return
            el = {"type": "way", "id": way.id, "tags": dict(way.tags)}
            if not _matches_supported_layer(el):
                return
            points = [{"lat": n.location.lat, "lon": n.location.lon} for n in way.nodes if n.location.valid()]
            if not points:
                return
            box = _zone_bbox([[[(p["lat"], p["lon"]) for p in points]]])
            if box[2] < bbox[0] or box[0] > bbox[2] or box[3] < bbox[1] or box[1] > bbox[3]:
                return
            if len(points) != len(way.nodes):
                raise ValueError(f"Extract has missing node locations for way {way.id} near the study area")
            el["geometry"] = points
            self.elements[("way", way.id)] = el

    reader = ExtractHandler()
    for path in paths:
        print(f"Reading OSM extract {path}...")
        reader.apply_file(str(path), locations=True)
    return list(reader.elements.values())


def build_cache(study_area_path: Path, out_path: Path, pbf_paths: list[Path] | None = None) -> dict:
    geometry = load_study_area(study_area_path)
    polygons = _zone_polygons({"id": "study area", "geometry": geometry})
    bbox = _zone_bbox(polygons)
    if pbf_paths:
        raw_elements = read_extracts(pbf_paths, bbox)
        source = "OpenStreetMap (regional PBF extracts)"
    else:
        pad = 0.002
        raw_elements = fetch_overpass(bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
        source = "OpenStreetMap (Overpass API)"
    print(f"Read {len(raw_elements)} candidate OSM elements")
    elements = {}
    for el in raw_elements:
        if el.get("type") not in ("node", "way") or not _matches_supported_layer(el):
            continue
        if el["type"] == "node":
            valid = all(isinstance(el.get(k), (int, float)) and math.isfinite(el[k]) for k in ("lat", "lon"))
        else:
            valid = isinstance(el.get("geometry"), list) and len(el["geometry"]) >= 2 and all(
                isinstance(p, dict) and all(isinstance(p.get(k), (int, float)) and math.isfinite(p[k]) for k in ("lat", "lon"))
                for p in el["geometry"]
            )
        if not valid or not isinstance(el.get("id"), int):
            raise ValueError("Incomplete OSM element in source data")
        if _feature_intersects_zone(el, polygons, bbox):
            compact = {key: el[key] for key in ("type", "id", "tags")}
            if el["type"] == "node":
                compact.update(lat=el["lat"], lon=el["lon"])
            else:
                compact["geometry"] = el["geometry"]
            elements[f"{el['type']}/{el['id']}"] = compact
    cache = {
        "schema_version": "3.0",
        "source": source,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": geometry,
        "elements": elements,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB): "
        f"{len(elements)} elements intersecting the study area"
    )
    return cache


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="data/api/osm-study-area.geojson")
    parser.add_argument("--out", default="data/api/osm-cache.json")
    parser.add_argument("--pbf", action="append", default=[], help="OSM PBF extract; repeat for each covering extract")
    args = parser.parse_args()
    build_cache(Path(args.area), Path(args.out), [Path(path) for path in args.pbf])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
