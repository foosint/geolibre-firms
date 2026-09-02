from __future__ import annotations

import argparse
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import GeometryCollection, Point, Polygon
from shapely.ops import unary_union

LOG = logging.getLogger("firms")

URLS = {
    "MODIS_C6.1": "https://firms.modaps.eosdis.nasa.gov/api/kml_fire_footprints/russia_asia/24h/c6.1/FirespotArea_russia_asia_c6.1_24h.kmz",
    "SUOMI-NPP": "https://firms.modaps.eosdis.nasa.gov/api/kml_fire_footprints/russia_asia/24h/suomi-npp-viirs-c2/FirespotArea_russia_asia_suomi-npp-viirs-c2_24h.kmz",
    "NOAA-20": "https://firms.modaps.eosdis.nasa.gov/api/kml_fire_footprints/russia_asia/24h/noaa-20-viirs-c2/FirespotArea_russia_asia_noaa-20-viirs-c2_24h.kmz",
    "NOAA-21": "https://firms.modaps.eosdis.nasa.gov/api/kml_fire_footprints/russia_asia/24h/noaa-21-viirs-c2/FirespotArea_russia_asia_noaa-21-viirs-c2_24h.kmz",
}

OUTPUTS = {
    ("centroid", "0_6"): "centroids_0_6.parquet",
    ("centroid", "6_12"): "centroids_6_12.parquet",
    ("centroid", "12_24"): "centroids_12_24.parquet",
    ("footprint", "0_6"): "footprints_0_6.parquet",
    ("footprint", "6_12"): "footprints_6_12.parquet",
    ("footprint", "12_24"): "footprints_12_24.parquet",
}

NS = {"kml": "http://earth.google.com/kml/2.1"}
TIME_PATTERNS = [
    (re.compile(r"0\s*(?:to|-|–|—)\s*<?\s*6\s*(?:hrs)?", re.I), "0_6"),
    (re.compile(r"6\s*(?:to|-|–|—)\s*<?\s*12\s*(?:hrs)?", re.I), "6_12"),
    (re.compile(r"12\s*(?:to|-|–|—)\s*<?\s*24\s*(?:hrs)?", re.I), "12_24"),
]


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def find_time(names: list[str]) -> str | None:
    joined = " / ".join(n for n in names if n)
    for pattern, bucket in TIME_PATTERNS:
        if pattern.search(joined):
            return bucket
    return None


def parse_extended_data(placemark: ET.Element) -> dict[str, Any]:
    result: dict[str, Any] = {}
    extended = placemark.find("kml:ExtendedData", NS)
    if extended is None:
        return result

    for data in extended.findall(".//kml:Data", NS):
        name = data.get("name")
        if name:
            result[name] = text(data.find("kml:value", NS))

    for data in extended.findall(".//kml:SimpleData", NS):
        name = data.get("name")
        if name:
            result[name] = text(data)

    return result


def parse_coordinates(value: str) -> list[tuple[float, float]]:
    coordinates: list[tuple[float, float]] = []
    for item in value.split():
        parts = item.split(",")
        if len(parts) < 2:
            continue
        try:
            coordinates.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return coordinates


def geometry_from_element(element: ET.Element):
    tag = local(element.tag)

    if tag == "Point":
        values = parse_coordinates(text(element.find("kml:coordinates", NS)))
        return Point(values[0]) if values else None

    if tag == "Polygon":
        outer = element.find(".//kml:outerBoundaryIs//kml:coordinates", NS)
        if outer is None:
            return None
        shell = parse_coordinates(text(outer))
        if len(shell) < 4:
            return None

        holes: list[list[tuple[float, float]]] = []
        for ring in element.findall(".//kml:innerBoundaryIs//kml:coordinates", NS):
            coords = parse_coordinates(text(ring))
            if len(coords) >= 4:
                holes.append(coords)
        return Polygon(shell, holes)

    if tag == "MultiGeometry":
        geometries = []
        for child in list(element):
            geometry = geometry_from_element(child)
            if geometry is not None:
                geometries.extend(
                    geometry.geoms
                    if geometry.geom_type == "GeometryCollection"
                    else [geometry]
                )
        if not geometries:
            return None

        types = {g.geom_type for g in geometries}
        if types == {"Point"}:
            from shapely.geometry import MultiPoint
            return MultiPoint(geometries)
        if types == {"Polygon"}:
            from shapely.geometry import MultiPolygon
            return MultiPolygon(geometries)
        return GeometryCollection(geometries)

    return None


def extract_geometries(placemark: ET.Element) -> list:
    geometries = []
    for child in placemark:
        if local(child.tag) not in {"Point", "Polygon", "MultiGeometry"}:
            continue
        geometry = geometry_from_element(child)
        if geometry is None:
            continue
        if geometry.geom_type == "GeometryCollection":
            geometries.extend(g for g in geometry.geoms if not g.is_empty)
        else:
            geometries.append(geometry)
    return geometries


def classify_geometry(geometry) -> str | None:
    if geometry.geom_type in {"Point", "MultiPoint"}:
        return "centroid"
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return "footprint"
    return None


def walk(parent: ET.Element, folders: list[str], sensor: str, rows: list[dict[str, Any]]) -> None:
    for child in list(parent):
        tag = local(child.tag)
        if tag == "Folder":
            name = text(child.find("kml:name", NS))
            print(name)
            walk(child, folders + ([name] if name else []), sensor, rows)
        elif tag in {"Document", "kml:Document"}:
                walk(child, folders, sensor, rows)
        elif tag == "Placemark":
            name = text(child.find("kml:name", NS))
            bucket = find_time(folders + [name])
            if bucket is None:
                continue
            attributes = parse_extended_data(child)
            attributes.update({
                "name": name,
                "description": text(child.find("kml:description", NS)),
                "style_url": text(child.find("kml:styleUrl", NS)),
            })
            for geometry in extract_geometries(child):
                geometry_type = classify_geometry(geometry)
                if geometry_type:
                    rows.append({
                        **attributes,
                        "sensor": sensor,
                        "time_range": bucket,
                        "geometry_type": geometry_type,
                        "layer": " / ".join(folders),
                        "geometry": geometry,
                    })
        elif len(child):
            walk(child, folders, sensor, rows)


def parse_kml(data: bytes, sensor: str) -> list[dict[str, Any]]:
    root = ET.fromstring(data)
    rows: list[dict[str, Any]] = []
    walk(root, [], sensor, rows)
    return rows


def download_kmz(url: str) -> bytes:
    LOG.info("Downloading %s", url)
    response = requests.get(
        url,
        timeout=(30, 180),
        headers={"User-Agent": "firms-fire-parquet/1.0"},
    )
    response.raise_for_status()
    if not response.content.startswith(b"PK"):
        raise RuntimeError(f"Response is not a KMZ/ZIP file: {url}")
    return response.content


def parse_kmz(data: bytes, sensor: str) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        kml_names = sorted(
            (n for n in archive.namelist() if n.lower().endswith(".kml")),
            key=lambda n: (Path(n).name.lower() != "doc.kml", n),
        )
        if not kml_names:
            raise RuntimeError(f"{sensor}: KMZ contains no KML")
        rows: list[dict[str, Any]] = []
        for name in kml_names:
            LOG.info("  parsing %s", name)
            rows.extend(parse_kml(archive.read(name), sensor))
        return rows


def load_filter(path: Path) -> gpd.GeoDataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Filter shapefile not found: {path}")
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise RuntimeError(f"Filter shapefile is empty: {path}")
    if gdf.crs is None:
        raise RuntimeError(f"Filter shapefile has no CRS (.prj missing?): {path}")
    return gdf


def filter_rows(rows: list[dict[str, Any]], filter_gdf: gpd.GeoDataFrame) -> list[dict[str, Any]]:
    if not rows:
        return rows

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    filter_geometry = unary_union(filter_gdf.to_crs("EPSG:4326").geometry)
    if filter_geometry.is_empty:
        raise RuntimeError("Filter shapefile geometry is empty")

    keep = pd.Series(False, index=gdf.index)
    centroids = gdf["geometry_type"] == "centroid"
    footprints = gdf["geometry_type"] == "footprint"
    keep.loc[centroids] = gdf.loc[centroids, "geometry"].within(filter_geometry)
    keep.loc[footprints] = gdf.loc[footprints, "geometry"].intersects(filter_geometry)
    return gdf.loc[keep].to_dict("records")


def make_gdf(rows: list[dict[str, Any]]) -> gpd.GeoDataFrame:
    if not rows:
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326"), crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def write_outputs(all_rows: list[dict[str, Any]], output_dir: Path) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stats: dict[str, int] = {}

    for filename in OUTPUTS.values():
        path = output_dir / filename
        if path.exists():
            path.unlink()

    for (geometry_type, bucket), filename in OUTPUTS.items():
        rows = [
            row for row in all_rows
            if row["geometry_type"] == geometry_type and row["time_range"] == bucket
        ]
        gdf = make_gdf(rows)
        path = output_dir / filename
        gdf.to_parquet(path, index=False, compression="snappy")
        stats[filename] = len(gdf)
        LOG.info("Wrote %-24s %8d features", filename, len(gdf))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("parquet"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    generated_at = datetime.now(timezone.utc).isoformat()
    all_rows: list[dict[str, Any]] = []

    for sensor, url in URLS.items():
        rows = parse_kmz(download_kmz(url), sensor)
        LOG.info("%s: extracted %d usable geometries", sensor, len(rows))
        all_rows.extend(rows)

    before = len(all_rows)
    all_rows = filter_rows(all_rows, load_filter(args.filter))
    LOG.info("Spatial filter: %d -> %d geometries", before, len(all_rows))
    stats = write_outputs(all_rows, args.output)

    metadata = {
        "generated_at": generated_at,
        "source": "NASA FIRMS",
        "region": "russia_asia",
        "date_span": "24h",
        "sensors": list(URLS),
        "filter": str(args.filter),
        "filter_centroids": "within",
        "filter_footprints": "intersects",
        "outputs": stats,
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
