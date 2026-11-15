#!/usr/bin/env python3
"""Precompute OpenStreetMap infrastructure cache for all zones in data/api/zones.json.

Fetches matching OSM nodes and ways once from Overpass API for the study area
bounding box, intersects them with each zone polygon, and writes a deduplicated
cache file at data/api/osm-cache.json so the dashboard does not get rate-limited
by live OSM API calls.
"""
from __future__ import annotations

import argparse
import json
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


def build_cache(zones_path: Path, out_path: Path) -> dict:
    zones_payload = json.loads(zones_path.read_text(encoding="utf-8"))
    zones = zones_payload["zones"]
    if not zones:
        cache = {
            "schema_version": "2.0",
            "source": "OpenStreetMap (Overpass API precomputed cache)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elements": {},
            "zones": {},
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        return cache

    parsed_zones = []
    seen_ids = set()
    min_lat, min_lon, max_lat, max_lon = 90.0, 180.0, -90.0, -180.0
    for zone in zones:
        if not isinstance(zone.get("id"), str) or not zone["id"].strip() or zone["id"] in seen_ids:
            raise ValueError("Invalid or duplicate zone identifier")
        seen_ids.add(zone["id"])
        polys = _zone_polygons(zone)
        zb = _zone_bbox(polys)
        min_lat = min(min_lat, zb[0])
        min_lon = min(min_lon, zb[1])
        max_lat = max(max_lat, zb[2])
        max_lon = max(max_lon, zb[3])
        parsed_zones.append((zone["id"], zone["geometry"], polys, zb))

    pad = 0.002
    print(
        f"Fetching OSM features for {len(zones)} zones in bbox "
        f"({min_lat - pad:.4f}, {min_lon - pad:.4f}, {max_lat + pad:.4f}, {max_lon + pad:.4f})..."
    )
    raw_elements = fetch_overpass(min_lat - pad, min_lon - pad, max_lat + pad, max_lon + pad)
    print(f"Downloaded {len(raw_elements)} raw OSM elements")

    filtered_elements = []
    for el in raw_elements:
        if el.get("type") not in ("node", "way"):
            continue
        if not _matches_supported_layer(el):
            continue
        if el["type"] == "node" and (el.get("lat") is None or el.get("lon") is None):
            continue
        if el["type"] == "way" and not el.get("geometry"):
            continue
        compact = {
            "type": el["type"],
            "id": el["id"],
            "tags": el.get("tags", {}),
        }
        if el["type"] == "node":
            compact["lat"] = el["lat"]
            compact["lon"] = el["lon"]
        else:
            compact["geometry"] = el["geometry"]
        filtered_elements.append(compact)

    print(f"Filtered to {len(filtered_elements)} layer-matching elements")

    used_elements: dict[str, dict] = {}
    zone_map: dict[str, dict] = {}
    for zone_id, geometry, polys, zb in parsed_zones:
        keys = []
        for el in filtered_elements:
            if _feature_intersects_zone(el, polys, zb):
                key = f"{el['type']}/{el['id']}"
                used_elements[key] = el
                keys.append(key)
        zone_map[zone_id] = {"geometry": geometry, "elements": keys}

    cache = {
        "schema_version": "2.0",
        "source": "OpenStreetMap (Overpass API precomputed cache)",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elements": used_elements,
        "zones": zone_map,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB): "
        f"{len(used_elements)} intersecting elements across {len(zone_map)} zones"
    )
    return cache


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zones", default="data/api/zones.json")
    parser.add_argument("--out", default="data/api/osm-cache.json")
    args = parser.parse_args()
    build_cache(Path(args.zones), Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
