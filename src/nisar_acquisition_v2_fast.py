"""FAST OFFLINE NISAR GUNW acquisition-v2 adapter (existing Harmony GeoTIFFs only).

Run from the project root:
    python src/nisar_acquisition_v2_fast.py

This script makes the acquisition-v2 .npz structure from COMPLETED local
Harmony acquisitions under data/raw/**/manifest.json. No CMR, Harmony jobs,
NASA DEM, or HDF5 access is performed.

Absent atmospheric/tide corrections are saved as NaN, NEVER zero. For the
resulting .npz, disable wet/dry/tide correction in nisar_processing_v2.py.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json
import logging

import numpy as np
import rasterio
from rasterio import Affine
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.warp import reproject, transform_bounds, transform_geom

# ======================== EDIT CONFIGURATION ===========================
GEOJSON_FILE = Path("data/geography/dead_sea.geojson")
RAW_ROOT = Path("data/raw")
START_DATE = "2026-06-01"
END_DATE = "2026-11-15"
TRACK = 100
FRAME = 17
POLARIZATION = "HH"
EPSG = 32636
PIXEL_M = 80.0
MAX_PAIRS = None     # None = all completed connected pairs; 1 or 2 for quick test
OUTPUT = Path("data/raw/dead_sea_fast.npz")
# =======================================================================

LOG = logging.getLogger("nisar.fast")
SCIENTIFIC_LAYERS = ("phase", "coherence", "ionosphere", "connected_components")
OPTIONAL_LAYERS = ("tropo_wet", "tropo_dry", "solid_earth_tides")


def read_geometries(path):
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj["type"] in ("Polygon", "MultiPolygon"):
        geoms = [obj]
    elif obj["type"] == "Feature":
        geoms = [obj["geometry"]]
    elif obj["type"] == "FeatureCollection":
        geoms = [f["geometry"] for f in obj["features"]]
    else:
        raise ValueError("Expected GeoJSON Polygon, MultiPolygon, Feature, or FeatureCollection")
    geoms = [g for g in geoms if g and g["type"] in ("Polygon", "MultiPolygon")]
    if not geoms:
        raise ValueError("No polygon geometries")
    pts=[]
    for g in geoms:
        polygons = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polygons:
            for ring in poly:
                pts.extend(ring)
    west, south = min(p[0] for p in pts), min(p[1] for p in pts)
    east, north = max(p[0] for p in pts), max(p[1] for p in pts)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError(f"Invalid polygon W,S,E,N: {(west,south,east,north)}")
    return geoms, (west, south, east, north)


def grid_for_polygon(geoms, bbox):
    crs=f"EPSG:{EPSG}"
    l,b,r,t=transform_bounds("EPSG:4326",crs,*bbox,densify_pts=21)
    left=np.floor(l/PIXEL_M)*PIXEL_M
    top=np.ceil(t/PIXEL_M)*PIXEL_M
    width=int(np.ceil((r-left)/PIXEL_M))
    height=int(np.ceil((top-b)/PIXEL_M))
    tf=Affine(PIXEL_M,0,left,0,-PIXEL_M,top)
    geom_proj=[transform_geom("EPSG:4326",crs,g) for g in geoms]
    inside=geometry_mask(geom_proj, (height,width),tf,invert=True)
    if not np.any(inside):
        raise ValueError("No pixel centers within polygon")
    x=left+PIXEL_M*(np.arange(width)+0.5)
    y=top-PIXEL_M*(np.arange(height)+0.5)
    return tf,x,y,inside


def find_file(manifest_path, asset):
    src=Path(asset["path"])
    if src.is_file():
        return src
    nearby=manifest_path.parent/src.name
    if nearby.is_file():
        return nearby
    raise FileNotFoundError(f"Manifest asset absent: {src}; also tried {nearby}")


def discover_manifests():
    candidates=[]
    for filename in RAW_ROOT.rglob("manifest.json"):
        try:
            m=json.loads(filename.read_text(encoding="utf-8"))
            scene=m["scene"]
            if (m.get("status")!="complete" or int(scene.get("track",-1)) != TRACK
                or int(scene.get("frame",-1)) != FRAME):
                continue
            if m.get("requested",{}).get("polarization", "HH") != POLARIZATION:
                continue
            a,b=scene["reference_time"],scene["secondary_time"]
            if not (START_DATE <= a[:10] <= END_DATE and START_DATE <= b[:10] <= END_DATE):
                continue
            for layer in SCIENTIFIC_LAYERS:
                if layer not in m.get("assets",{}):
                    raise KeyError(f"{filename}: missing {layer}")
                find_file(filename,m["assets"][layer])
            candidates.append((m,filename))
        except (KeyError,ValueError,FileNotFoundError) as exc:
            LOG.debug("Skipping %s: %s",filename,exc)
    if not candidates:
        raise RuntimeError(
            f"No completed Harmony scenes for track {TRACK}, frame {FRAME} in {RAW_ROOT}. "
            "This offline shortcut only works if those GeoTIFFs have already been downloaded."
        )
    # Keep a single family (track/frame/direction/mode/release), and its largest connected component.
    fam=defaultdict(list)
    for m,filename in candidates:
        scene=m["scene"]
        key=(scene.get("track"),scene.get("frame"),scene.get("direction"),
             scene.get("mode"),scene.get("release"),scene.get("pol_code"))
        fam[key].append((m,filename))
    components=[]
    for key,items in fam.items():
        remaining=set(range(len(items)))
        while remaining:
            i=next(iter(remaining));remaining.remove(i);chosen={i}
            dates=set()
            for field in ("reference_time","secondary_time"):
                dates.add(items[i][0]["scene"][field][:10])
            changed=True
            while changed:
                changed=False
                for j in list(remaining):
                    a,b=(items[j][0]["scene"][f][:10] for f in ("reference_time","secondary_time"))
                    if a in dates or b in dates:
                        chosen.add(j); remaining.remove(j);dates.update((a,b));changed=True
            subset=sorted((items[j] for j in chosen),key=lambda it:it[0]["scene"]["secondary_time"])
            components.append((len(subset),len(dates),key,subset))
    count,_,key,items=max(components,key=lambda q:q[:2])
    if MAX_PAIRS is not None:
        if MAX_PAIRS<1:
            raise ValueError("MAX_PAIRS must be >=1 or None")
        items=items[:MAX_PAIRS]
    LOG.info("Selected family %s: %d available connected pair(s) (out of %d)",key,len(items),count)
    return items


def warped_layer(filename, layer, tf, shape):
    dst=np.full(shape,np.nan,dtype=np.float32)
    with rasterio.open(filename) as ds:
        if ds.crs is None:
            raise ValueError(f"Missing CRS in {filename}")
        # Important: no phase or integer-label interpolation across connected components.
        reproject(source=rasterio.band(ds,1),destination=dst,
                  src_transform=ds.transform,src_crs=ds.crs,src_nodata=ds.nodata,
                  dst_transform=tf,dst_crs=f"EPSG:{EPSG}",dst_nodata=np.nan,
                  resampling=Resampling.nearest)
    return dst


def main():
    logging.basicConfig(level=logging.INFO,format="%(levelname)s: %(message)s")
    geoms,bbox=read_geometries(GEOJSON_FILE)
    tf,x,y,inside=grid_for_polygon(geoms,bbox)
    LOG.info("Grid %d x %d pixels; polygon cells: %d",len(x),len(y),inside.sum())
    items=discover_manifests()
    all_arrays={layer:[] for layer in (*SCIENTIFIC_LAYERS,*OPTIONAL_LAYERS)}
    records=[]
    for i,(manifest,filepath) in enumerate(items,1):
        scene=manifest["scene"]
        LOG.info("Scene %d/%d: %s -> %s",i,len(items),scene["reference_time"][:10],scene["secondary_time"][:10])
        for name in (*SCIENTIFIC_LAYERS,*OPTIONAL_LAYERS):
            if name in manifest.get("assets",{}):
                try:
                    path=find_file(filepath,manifest["assets"][name])
                    a=warped_layer(path,name,tf,inside.shape)
                except (FileNotFoundError,ValueError) as exc:
                    if name in SCIENTIFIC_LAYERS:
                        raise
                    LOG.warning("Optional %s unavailable: %s",name,exc)
                    a=np.full(inside.shape,np.nan,dtype=np.float32)
            else:
                if name in SCIENTIFIC_LAYERS:
                    raise ValueError(f"Missing required layer {name} in {filepath}")
                a=np.full(inside.shape,np.nan,dtype=np.float32)
            a[~inside]=np.nan
            all_arrays[name].append(a)
        if not np.isfinite(all_arrays["phase"][-1]).any():
            raise RuntimeError(f"No valid phase inside GeoJSON polygon in {filepath}. Check geographic coverage.")
        records.append({"name":scene.get("granule_name", ""),"track":scene.get("track"),
                        "frame":scene.get("frame"),"mode":scene.get("mode"),
                        "direction":scene.get("direction"),"release":scene.get("release"),
                        "reference_time":scene["reference_time"],
                        "secondary_time":scene["secondary_time"],
                        "manifest_path":str(filepath)})
    arrays={k:np.stack(v).astype(np.float32) for k,v in all_arrays.items()}
    layers_present={k:bool(np.any(np.isfinite(arrays[k]))) for k in all_arrays}
    LOG.info("Available layers: %s",[k for k,v in layers_present.items() if v])
    LOG.warning("Wet/dry corrections in these manifests, if present, may use a fixed height, not a DEM height")
    LOG.warning("No DEM read in fast mode; elevation is NaN (not fabricated).")
    arrays.update(elevation=np.full(inside.shape,np.nan,dtype=np.float32),
                  x=x.astype(np.float64),y=y.astype(np.float64),polygon_mask=inside,
                  reference_times=np.array([r["reference_time"] for r in records]),
                  secondary_times=np.array([r["secondary_time"] for r in records]),
                  granule_ids=np.array([r["name"] for r in records]))
    meta={"description":"FAST OFFLINE alignment of previously downloaded Harmony GeoTIFFs",
          "created_utc":datetime.now(timezone.utc).isoformat(),
          "source":"local Harmony GeoTIFFs; NO new download",
          "grid":{"epsg":EPSG,"pixel_size_m":PIXEL_M,"shape":list(inside.shape),
                  "affine":list(tuple(tf)[:6]),"axis_order":["pair","row_north_to_south","col_west_to_east"],
                  "polygon_bbox_lonlat":list(bbox)},
          "method":{"corrections_applied":False,"elevation_available":False,
                    "missing_layers_are_nan":True,"time_axis":"interferogram pair"},
          "available_layers":layers_present,"scenes":records}
    arrays["metadata_json"]=np.asarray(json.dumps(meta,allow_nan=False))
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    tmp=OUTPUT.with_name(OUTPUT.stem+".partial.npz")
    np.savez_compressed(tmp,**arrays)
    tmp.replace(OUTPUT)
    LOG.info("Saved %s: %s",OUTPUT,arrays["phase"].shape)
    LOG.info("For processing-v2 disable any correction with available_layers=false")

if __name__=="__main__":
    main()
