#!/usr/bin/env python3
"""Search and subset NISAR GUNW through NASA CMR + Harmony.

Dead Sea coordinates: 35.30 30.95 35.54 31.80

Examples (Windows PowerShell):
  python fetch_nisar_gunw.py --search-only
  python fetch_nisar_gunw.py --bbox 35.30 30.95 35.54 31.80
  python fetch_nisar_gunw.py --center 31.46 35.39 --size-km 8
  python fetch_nisar_gunw.py --bbox 35.30 30.95 35.54 31.80 --scene 2
  python fetch_nisar_gunw.py --resume YOUR_HARMONY_JOB_ID

Requires: pip install requests harmony-py
An Earthdata Login account is required to submit/download Harmony jobs.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

CMR = "https://cmr.earthdata.nasa.gov/search"
SHORT_NAMES = {
    "validated": "NISAR_L2_GUNW_V1",
    "provisional": "NISAR_L2_GUNW_PROVISIONAL_V1",
    "beta": "NISAR_L2_GUNW_BETA_V1",
}
DEFAULT_BBOX = (35.35, 31.43, 35.45, 31.50)  # Ein Gedi: W, S, E, N


def bbox_from_center(lat: float, lon: float, size_km: float) -> tuple[float, ...]:
    """Approximate square centered on (lat, lon), intended for small AOIs."""
    if size_km <= 0 or abs(lat) >= 89.0:
        raise ValueError("size-km must be positive and latitude must be below 89 degrees")
    half_lat = size_km / (2 * 111.32)
    half_lon = size_km / (2 * 111.32 * math.cos(math.radians(lat)))
    return (lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat)


def check_bbox(bbox: tuple[float, ...]) -> None:
    w, s, e, n = bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError("Invalid bbox. Expected west south east north in degrees (no antimeridian crossing).")


def cmr_get(session: requests.Session, endpoint: str, params: dict) -> dict:
    response = session.get(f"{CMR}/{endpoint}.json", params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def get_collection(session: requests.Session, short_name: str) -> str | None:
    entries = cmr_get(session, "collections", {"short_name": short_name, "page_size": 10})["feed"]["entry"]
    matching = [e for e in entries if e.get("short_name") == short_name]
    return matching[0]["id"] if matching else None


def get_granules(
    session: requests.Session,
    collection_id: str,
    bbox: tuple[float, ...],
    start: str,
    end: str | None,
    limit: int,
) -> list[dict]:
    interval = f"{start}T00:00:00Z,{end + 'T23:59:59Z' if end else ''}"
    response = cmr_get(session, "granules", {
        "collection_concept_id": collection_id,
        "bounding_box": ",".join(str(v) for v in bbox),
        "temporal": interval,
        "page_size": limit,
        "sort_key[]": "-start_date",
    })
    return response.get("feed", {}).get("entry", [])


def choose_scene(session, bbox, start, end, release, limit, scene_index):
    releases = ("validated", "provisional", "beta") if release == "auto" else (release,)
    for tier in releases:
        short_name = SHORT_NAMES[tier]
        collection_id = get_collection(session, short_name)
        if not collection_id:
            print(f"No CMR collection found: {short_name}")
            continue
        scenes = get_granules(session, collection_id, bbox, start, end, limit)
        print(f"{short_name}: {len(scenes)} result(s) returned for this search")
        if not scenes:
            continue
        for index, scene in enumerate(scenes, start=1):
            print(f"  {index:>2}. {scene.get('time_start','unknown date')}  "
                  f"{scene.get('title', scene.get('producer_granule_id', '<unnamed>'))}")
            print(f"      CMR granule concept ID: {scene['id']}")
        if scene_index > len(scenes):
            raise ValueError(f"--scene {scene_index} is not among the {len(scenes)} shown; increase --limit")
        return tier, short_name, collection_id, scenes[scene_index - 1]
    return None


def variables_for(layers: list[str], pol: str) -> list[str]:
    pols = ("HH", "VV") if pol == "both" else (pol,)
    paths = []
    for polarization in pols:
        base = f"/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/{polarization}"
        if "phase" in layers:
            paths.append(f"{base}/unwrappedPhase")
        if "coherence" in layers:
            paths.append(f"{base}/coherenceMagnitude")
    return paths


def connect_harmony():
    try:
        from harmony import BBox, Client, Collection, Request
    except ImportError as exc:
        raise RuntimeError("Missing library. Install: python -m pip install harmony-py requests") from exc
    return BBox, Client, Collection, Request


def download_job(client, job_id: str, output_dir: Path) -> list[str]:
    print(f"Waiting for Harmony job {job_id} ...")
    client.wait_for_processing(job_id, show_progress=True)
    print(f"Downloading finished COGs to {output_dir.resolve()} ...")
    futures = client.download_all(job_id, directory=str(output_dir), overwrite=False)
    files = [str(future.result()) for future in futures]
    print(f"Downloaded {len(files)} file(s):")
    for file in files:
        print("  ", file)
    if not files:
        print("No output files returned. Inspect the Harmony job status for details.")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    aoi = parser.add_mutually_exclusive_group()
    aoi.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                     help="WGS84 geographic bounding box, in degrees")
    aoi.add_argument("--center", type=float, nargs=2, metavar=("LAT", "LON"),
                     help="WGS84 center; combine with --size-km")
    parser.add_argument("--size-km", type=float, default=8.0, help="Square width around --center (default 8 km)")
    parser.add_argument("--start", default="2025-10-01", help="Start date YYYY-MM-DD (default 2025-10-01)")
    parser.add_argument("--end", help="End date YYYY-MM-DD (default: open-ended)")
    parser.add_argument("--release", choices=["auto", *SHORT_NAMES], default="auto",
                        help="Search release tier, auto=validated first, then provisional/beta")
    parser.add_argument("--limit", type=int, default=10, help="Maximum scenes listed per collection (default 10)")
    parser.add_argument("--scene", type=int, default=1, help="1-based scene choice from displayed results (default 1)")
    parser.add_argument("--layers", nargs="+", choices=["phase", "coherence"],
                        default=["phase", "coherence"], help="GUNW layers to extract")
    parser.add_argument("--polarization", choices=["HH", "VV", "both"], default="both",
                        help="Select channels; missing ones are skipped by Harmony")
    parser.add_argument("--epsg", type=int, help="Optional output projection, e.g. 4326; default preserves source CRS")
    parser.add_argument("--out", type=Path, default=Path("data/nisar_gunw"), help="Output folder")
    parser.add_argument("--search-only", action="store_true", help="Only list matching scenes; no login or download")
    parser.add_argument("--resume", metavar="JOB_ID", help="Resume an existing Harmony job by ID")
    args = parser.parse_args(argv)

    if args.limit < 1 or args.scene < 1:
        parser.error("--limit and --scene must be positive")
    if args.scene > args.limit:
        parser.error("--scene cannot be larger than --limit")
    if args.end and args.start > args.end:
        parser.error("--start must be before --end")

    args.out.mkdir(parents=True, exist_ok=True)

    if args.resume:
        _, Client, _, _ = connect_harmony()
        download_job(Client(), args.resume, args.out)
        return 0

    bbox = tuple(args.bbox) if args.bbox else (
        bbox_from_center(args.center[0], args.center[1], args.size_km)
        if args.center else DEFAULT_BBOX
    )
    check_bbox(bbox)
    print("Requested bbox (west, south, east, north):", bbox)
    with requests.Session() as session:
        selected = choose_scene(session, bbox, args.start, args.end, args.release, args.limit, args.scene)
    if selected is None:
        print("No matching GUNW granules found. Try a larger bbox, a wider date range, or --release beta.")
        return 2
    tier, short_name, collection_id, scene = selected
    if args.search_only:
        print("Search-only mode: no Harmony job submitted.")
        return 0

    BBox, Client, Collection, Request = connect_harmony()
    variables = variables_for(args.layers, args.polarization)
    request_opts = {
        "collection": Collection(id=collection_id),
        "granule_id": [scene["id"]],
        "spatial": BBox(*bbox),
        "variables": variables,
        "format": "image/tiff",
    }
    if args.epsg is not None:
        request_opts["crs"] = f"EPSG:{args.epsg}"
    request = Request(**request_opts)
    print("\nExtracting variables:")
    for var in variables:
        print("  ", var)
    client = Client()  # Earthdata Login credentials are requested/used by harmony-py.
    job_id = client.submit(request)
    print(f"Submitted Harmony job: {job_id}")
    print("To resume later: python fetch_nisar_gunw_harmony.py --resume", job_id)

    metadata = {
        "requested_utc": datetime.now(timezone.utc).isoformat(),
        "release": tier,
        "short_name": short_name,
        "collection_id": collection_id,
        "granule_id": scene["id"],
        "granule_title": scene.get("title"),
        "acquisition_start": scene.get("time_start"),
        "bbox_wsen": bbox,
        "variables_requested": variables,
        "job_id": job_id,
    }
    metadata_file = args.out / f"harmony_{job_id}.json"
    metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print("Metadata saved:", metadata_file)
    download_job(client, job_id, args.out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
