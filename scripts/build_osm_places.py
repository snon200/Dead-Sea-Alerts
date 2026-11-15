#!/usr/bin/env python3
"""Build an OSM place-name dataset around the fixed regional study area."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .build_osm_cache import _zone_bbox, _zone_polygons, fetch_overpass_query, load_study_area
else:
    from build_osm_cache import _zone_bbox, _zone_polygons, fetch_overpass_query, load_study_area

PLACE_TYPES = ("city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "locality", "island", "islet")
WATERWAY_TYPES = ("river", "stream", "canal", "drain", "wadi")
NATURAL_TYPES = ("peak", "hill", "valley", "wadi", "ridge", "cliff", "spring", "beach", "bay",
                 "water", "strait", "cape", "saddle", "wetland", "cave_entrance", "sinkhole")

ENGLISH_PLACE_OVERRIDES = {
    "תקוע ב": "Tekoa B",
    "מקור ברוך": "Mekor Baruch",
    "מצפה חגית": "Mitzpe Hagit",
    "תקוע ד": "Tekoa D",
    "חוות העיטם": "Havat HaEitam",
    "חוות הדיה": "Havat HaDaya",
    "חוות חנינה": "Havat Hanina",
    "משכנות הראל": "Mishkenot Harel",
    "תקוע ו": "Tekoa F",
    "חוות הראל": "Havat Harel",
    "שכונת ברקאי": "Barkai Neighborhood",
    "יחיש ציון": "Yahish Zion",
    "כוכב יהודה": "Kochav Yehuda",
    "נחל מורג": "Nahal Morag",
    "חוף עין בוקק": "Ein Bokek Beach",
    "נחל מצפה ארנון": "Nahal Mitzpe Arnon",
    "נחל צנין": "Nahal Tznin",
    "נחל חצצון": "Nahal Hatzatzon",
    "נחל פ'שחה": "Nahal Fashkha",
    "ואדי ג'ופת זבן": "Wadi Juft Zaban",
    "שיעב א-דיר": "Shi'ab Al-Dir",
    '"הנחל החם"': "HaNahal HaHam",
    "הנחל החם": "HaNahal HaHam",
    "מלחת סדום": "Sodom Salt Marsh",
    "נחל המלט": "Nahal HaMelet",
    "נחל בולען": "Nahal Bolan",
    "תעלת ההזנה": "Feeder Canal",
    "נחל קינמון": "Nahal Kinamon",
    "מערת האמוניט": "Ammonite Cave",
    "בריכת מים מתוקים": "Freshwater Pool",
    "נחל יוריק": "Nahal Yorik",
    "מערת בקתה": "Bikta Cave",
    "בריכות נווית": "Nevit Pools",
}

_HEBREW_WORDS = {
    "נחל": "Nahal", "עין": "Ein", "ואדי": "Wadi", "הר": "Har", "חוף": "Beach",
    "מצפה": "Mitzpe", "גבעת": "Givat", "רמת": "Ramat", "בקעת": "Bikat",
    "מעלה": "Ma'ale", "חורבת": "Horvat", "מערת": "Me'arat", "באר": "Be'er",
    "מעיין": "Ma'ayan", "גשר": "Gesher", "מפלי": "Mapalei", "מפל": "Mapal",
    "שמורת": "Shmurat", "צומת": "Tzomet", "חניון": "Hanyon", "חוות": "Havat",
}

_HEBREW_MAP = {
    "א": "a", "ב": "b", "ג": "g", "ד": "d", "ה": "h", "ו": "v", "ז": "z", "ח": "h",
    "ט": "t", "י": "y", "כ": "k", "ך": "k", "ל": "l", "מ": "m", "ם": "m", "נ": "n",
    "ן": "n", "ס": "s", "ע": "a", "פ": "p", "ף": "f", "צ": "tz", "ץ": "tz", "ק": "k",
    "ר": "r", "ש": "sh", "ת": "t",
}
_ARABIC_MAP = {
    "ا": "a", "أ": "a", "إ": "i", "آ": "a", "ب": "b", "ت": "t", "ث": "th", "ج": "j",
    "ح": "h", "خ": "kh", "د": "d", "ذ": "dh", "ر": "r", "ز": "z", "س": "s",
    "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f",
    "ق": "q", "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h", "ة": "a", "و": "w",
    "ي": "y", "ى": "a", "ؤ": "u", "ئ": "i", "ء": "'",
}


def to_english_place_name(name: str) -> str:
    """Return an English/Latin place name, translating or transliterating Hebrew/Arabic."""
    if name in ENGLISH_PLACE_OVERRIDES:
        return ENGLISH_PLACE_OVERRIDES[name]
    if all(ord(ch) < 0x0590 for ch in name):
        return name
    words = []
    for word in name.split():
        if word in _HEBREW_WORDS:
            words.append(_HEBREW_WORDS[word])
            continue
        out = []
        for ch in word:
            if ch in _HEBREW_MAP:
                out.append(_HEBREW_MAP[ch])
            elif ch in _ARABIC_MAP:
                out.append(_ARABIC_MAP[ch])
            elif "\u0591" <= ch <= "\u05C7" or "\u064B" <= ch <= "\u065F":
                continue
            else:
                out.append(ch)
        words.append("".join(out).capitalize())
    return " ".join(w for w in words if w)


def is_named_feature(tags: dict) -> bool:
    return bool(tags.get("name:en") or tags.get("int_name") or tags.get("name")) and (
        tags.get("place") in PLACE_TYPES
        or tags.get("waterway") in WATERWAY_TYPES
        or tags.get("natural") in NATURAL_TYPES
    )


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
            if not is_named_feature(tags):
                return
            self.places[("node", node.id)] = {"type": "node", "id": node.id, "lat": lat, "lon": lon, "tags": tags}

        def way(self, way):
            tags = dict(way.tags)
            if not is_named_feature(tags):
                return
            points = [{"lat": node.location.lat, "lon": node.location.lon}
                      for node in way.nodes if node.location.valid()]
            if not points:
                return
            box = _zone_bbox([[[(point["lat"], point["lon"]) for point in points]]])
            if box[2] < bounds[0] or box[0] > bounds[2] or box[3] < bounds[1] or box[1] > bounds[3]:
                return
            if len(points) != len(way.nodes):
                raise ValueError(f"Extract has missing node locations for named way {way.id} near the study area")
            self.places[("way", way.id)] = {"type": "way", "id": way.id, "geometry": points, "tags": tags}

    reader = PlaceHandler()
    for path in paths:
        print(f"Reading OSM place names from {path}...")
        reader.apply_file(str(path), locations=True)
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
    selectors = (("place", PLACE_TYPES), ("waterway", WATERWAY_TYPES), ("natural", NATURAL_TYPES))
    query = '[out:json][timeout:60];(' + ''.join(
        f'nw["{key}"~"^({"|".join(values)})$"]["name"]({bbox});' for key, values in selectors
    ) + ');out body geom;'
    elements = read_extract_places(pbf_paths, bounds) if pbf_paths else fetch_overpass_query(query)
    places = []
    for element in elements:
        tags = element.get("tags", {})
        name = tags.get("name:en") or tags.get("int_name") or tags.get("name")
        if not is_named_feature(tags) or not isinstance(name, str) or not name.strip():
            continue
        english_name = to_english_place_name(name.strip())
        if element.get("type") == "node":
            places.append({"id": element["id"], "name": english_name, "lat": element["lat"], "lon": element["lon"]})
        elif element.get("type") == "way":
            geometry = element.get("geometry")
            if not isinstance(geometry, list) or len(geometry) < 2:
                raise ValueError(f"Named way {element['id']} has missing geometry")
            place = {"id": element["id"], "name": english_name,
                     "geometry": [[point["lat"], point["lon"]] for point in geometry]}
            if tags.get("place") in PLACE_TYPES or tags.get("natural") in NATURAL_TYPES:
                place["area"] = tags.get("area") != "no"
            places.append(place)
    if not places:
        raise ValueError("Overpass returned no named places")
    places.sort(key=lambda place: (place["id"], "geometry" in place))
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
