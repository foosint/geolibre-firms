from __future__ import annotations

import argparse
import io
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
import urllib3.util.connection as urllib3_cn
from shapely.geometry import GeometryCollection, Point, Polygon
from shapely.ops import unary_union

# Force IPv4 resolution globally for urllib3/requests
urllib3_cn.HAS_IPV6 = False

LOG = logging.getLogger("firms")

URLS = {
    "MODIS_C6.1": "https://firms.modaps.eosdis.nasa.gov/api/kml_fire_footprints/russia_asia/24h/c6.1/FirespotArea_russia_asia_c6.1_24h.kmz",
    "SUOMI-NPP": "https://firms.modaps.eosdis.nasa.gov/api/kml_fire_footprints/russia_asia/24h/suomi-npp-viirs-c2/FirespotArea_russia_asia_suomi-npp-viirs-c2_24h.kmz",
    "NOAA-20": "https://firms.modaps.eosdis.nasa.gov/api/kml_fire_footprints/russia_asia/24h/noaa-20-viirs-c2/FirespotArea_russia_asia_noaa-20-viirs-c2_24h.kmz",
    "NOAA-21": "https://firms.modaps.eosdis.nasa.gov/api/kml_fire_footprints/russia_asia/24h/noaa-21-viirs-c2/FirespotArea_russia_asia_noaa-21-viirs-c2_24h.kmz",
}

OUTPUTS = {
    ("centroid", "0_6"): "centroids_0_6",
    ("centroid", "6_12"): "centroids_6_12",
    ("centroid", "12_24"): "centroids_12_24",
    ("footprint", "0_6"): "footprints_0_6",
    ("footprint", "6_12"): "footprints_6_12",
    ("footprint", "12_24"): "footprints_12_24",
}

ROOT_OUT = Path(".")
PARQUET_OUT = Path("parquet")
GEOJSON_OUT = Path("geojson")

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


def download_kmz(url: str, retries: int = 3, backoff_factor: int = 5) -> bytes | None:
    for attempt in range(1, retries + 1):
        try:
            LOG.info("Downloading %s (attempt %d/%d)", url, attempt, retries)
            response = requests.get(
                url,
                timeout=(30, 180),
                headers={"User-Agent": "firms-fire-parquet/1.0"},
            )
            response.raise_for_status()
            if not response.content.startswith(b"PK"):
                raise RuntimeError(f"Response is not a KMZ/ZIP file: {url}")
            return response.content
        except (requests.exceptions.RequestException, OSError, RuntimeError) as e:
            LOG.warning("Attempt %d failed for %s: %s", attempt, url, e)
            if attempt == retries:
                LOG.error("Max retries reached for %s. Giving up.", url)
                return None
            time.sleep(backoff_factor * attempt)
    return None


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


def write_outputs(
    all_rows: list[dict[str, Any]],
    updated_at: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    """
    Write all Parquet/GeoJSON files and a small sidecar metadata file for each.

    Sidecar metadata looks like:
        {
            "features": 1234,
            "updated_at": "2026-09-04T10:00:00+00:00"
        }

    The returned dictionary is used only for generating index.html. The
    per-file statistics are deliberately not included in the global
    metadata.json files.
    """
    PARQUET_OUT.mkdir(parents=True, exist_ok=True)
    GEOJSON_OUT.mkdir(parents=True, exist_ok=True)

    file_metadata: dict[str, dict[str, dict[str, Any]]] = {
        "parquet": {},
        "geojson": {},
    }

    for (geometry_type, bucket), base_name in OUTPUTS.items():
        parquet_filename = f"{base_name}.parquet"
        geojson_filename = f"{base_name}.geojson"

        parquet_path = PARQUET_OUT / parquet_filename
        geojson_path = GEOJSON_OUT / geojson_filename

        parquet_meta_path = PARQUET_OUT / f"{base_name}.metadata.json"
        geojson_meta_path = GEOJSON_OUT / f"{base_name}.metadata.json"

        rows = [
            row
            for row in all_rows
            if row["geometry_type"] == geometry_type
            and row["time_range"] == bucket
        ]
        gdf = make_gdf(rows)

        feature_count = len(gdf)
        mini_metadata = {
            "features": feature_count,
            "updated_at": updated_at,
        }

        if len(gdf) == 0:
            LOG.info("No features for %s; removing old output if present.", base_name)

            for output_path in (parquet_path, geojson_path):
                if output_path.exists():
                    output_path.unlink()

            # Keep the sidecar metadata so a successful run with zero
            # features is distinguishable from a stale/missing output.
            parquet_meta_path.write_text(
                json.dumps(mini_metadata, indent=2) + "\n",
                encoding="utf-8",
            )
            geojson_meta_path.write_text(
                json.dumps(mini_metadata, indent=2) + "\n",
                encoding="utf-8",
            )

            file_metadata["parquet"][parquet_filename] = mini_metadata
            file_metadata["geojson"][geojson_filename] = mini_metadata
            continue

        columns_to_keep = ["sensor", "geometry", "description"]
        existing_cols = [col for col in columns_to_keep if col in gdf.columns]
        gdf_clean = gdf[existing_cols]

        # Write Parquet atomically.
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_parquet = parquet_path.with_suffix(parquet_path.suffix + ".tmp")
        try:
            gdf_clean.to_parquet(
                tmp_parquet,
                index=False,
                compression="snappy",
            )
            tmp_parquet.replace(parquet_path)
        except Exception:
            if tmp_parquet.exists():
                tmp_parquet.unlink()
            raise

        # Write GeoJSON atomically.
        geojson_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_geojson = Path(str(geojson_path) + ".tmp")
        try:
            gdf_clean.to_file(tmp_geojson, driver="GeoJSON")
            tmp_geojson.replace(geojson_path)
        except Exception:
            if tmp_geojson.exists():
                tmp_geojson.unlink()
            raise

        # Only update the sidecar metadata after the corresponding output
        # files were written successfully.
        parquet_meta_path.write_text(
            json.dumps(mini_metadata, indent=2) + "\n",
            encoding="utf-8",
        )
        geojson_meta_path.write_text(
            json.dumps(mini_metadata, indent=2) + "\n",
            encoding="utf-8",
        )

        file_metadata["parquet"][parquet_filename] = mini_metadata
        file_metadata["geojson"][geojson_filename] = mini_metadata

        LOG.info(
            "Wrote %-24s & GeoJSON (%8d features)",
            base_name,
            feature_count,
        )

    return file_metadata

def format_utc_timestamp(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        # Parse ISO string and convert to UTC
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_utc = dt.astimezone(timezone.utc)
        return dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return iso_str  # Fallback to original string if parsing fails

def format_utc_timestamp(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        # Parse ISO string and convert to UTC
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_utc = dt.astimezone(timezone.utc)
        return dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return iso_str  # Fallback to original string if parsing fails


def generate_index_html(
    parquet_metadata: dict,
    geojson_metadata: dict,
    file_metadata: dict[str, dict[str, dict[str, Any]]],
) -> None:
    formatted_generated_at = format_utc_timestamp(parquet_metadata.get('generated_at', ''))

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>FIRMS Fire Data Portal</title>
    <style>
        body {{ font-family: sans-serif; margin: 40px; background: #f4f4f9; color: #333; }}
        h1, h2 {{ color: #111; }}
        .card {{ background: white; padding: 20px; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        pre {{ background: #eee; padding: 10px; border-radius: 4px; overflow-x: auto; }}
        ul {{ line-height: 1.6; }}
        .updated {{ color: #666; font-size: 0.9em; }}
    </style>
</head>
<body>
    <h1>FIRMS Fire Data Portal</h1>
    <p>Generated at: <strong>{formatted_generated_at}</strong></p>
    <p>Region: <strong>{parquet_metadata['region']}</strong> | Source: <strong>{parquet_metadata['source']}</strong></p>

    <div class="card">
        <h2>GeoParquet Files</h2>
        <ul>
"""

    for fname, meta in file_metadata["parquet"].items():
        file_updated = format_utc_timestamp(meta.get("updated_at", ""))
        html_content += (
            f'            <li><a href="{PARQUET_OUT.name}/{fname}">{fname}</a> '
            f'({meta["features"]} features, updated {file_updated})</li>\n'
        )

    html_content += f"""        </ul>
        <h3>Parquet Metadata</h3>
        <pre>{json.dumps(parquet_metadata, indent=2)}</pre>
    </div>

    <div class="card">
        <h2>GeoJSON Files</h2>
        <ul>
"""

    for fname, meta in file_metadata["geojson"].items():
        file_updated = format_utc_timestamp(meta.get("updated_at", ""))
        html_content += (
            f'            <li><a href="{GEOJSON_OUT.name}/{fname}">{fname}</a> '
            f'({meta["features"]} features, updated {file_updated})</li>\n'
        )

    html_content += f"""        </ul>
        <h3>GeoJSON Metadata</h3>
        <pre>{json.dumps(geojson_metadata, indent=2)}</pre>
    </div>
</body>
</html>
"""

    (ROOT_OUT / "index.html").write_text(html_content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", type=Path, required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    generated_at = datetime.now(timezone.utc).isoformat()
    all_rows: list[dict[str, Any]] = []

    for sensor, url in URLS.items():
        data = download_kmz(url)
        if data is None:
            LOG.warning("Stopping further processing gracefully due to persistent download failure.")
            return 0
        rows = parse_kmz(data, sensor)
        LOG.info("%s: extracted %d usable geometries", sensor, len(rows))
        all_rows.extend(rows)

    before = len(all_rows)
    all_rows = filter_rows(all_rows, load_filter(args.filter))
    LOG.info("Spatial filter: %d -> %d geometries", before, len(all_rows))
    
    file_metadata = write_outputs(all_rows, generated_at)

    parquet_metadata = {
        "generated_at": generated_at,
        "source": "NASA FIRMS",
        "region": "russia_asia",
        "date_span": "24h",
        "sensors": list(URLS),
        "filter": str(args.filter),
        "filter_centroids": "within",
        "filter_footprints": "intersects",
    }
    (PARQUET_OUT / "metadata.json").write_text(
        json.dumps(parquet_metadata, indent=2) + "\n",
        encoding="utf-8",
    )

    geojson_metadata = {
        "generated_at": generated_at,
        "source": "NASA FIRMS",
        "region": "russia_asia",
        "date_span": "24h",
        "sensors": list(URLS),
        "filter": str(args.filter),
        "filter_centroids": "within",
        "filter_footprints": "intersects",
    }
    (GEOJSON_OUT / "metadata.json").write_text(
        json.dumps(geojson_metadata, indent=2) + "\n",
        encoding="utf-8",
    )

    generate_index_html(parquet_metadata, geojson_metadata, file_metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())