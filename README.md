# NASA FIRMS Russia/Asia → GeoParquet

This repository downloads four NASA FIRMS Russia/Asia 24-hour fire-footprint KMZ products, extracts centroid and footprint geometries, merges the four sensors, filters them with a user-supplied shapefile, and publishes six GeoParquet files.

## Output

- `parquet/centroids_0_6.parquet`
- `parquet/centroids_6_12.parquet`
- `parquet/centroids_12_24.parquet`
- `parquet/footprints_0_6.parquet`
- `parquet/footprints_6_12.parquet`
- `parquet/footprints_12_24.parquet`

All data are written as GeoParquet in EPSG:4326 with Zstandard compression.

## Filter shapefile

Add these files manually to `data/filter/`:

```text
data/filter/ukraine_russia.shp
data/filter/ukraine_russia.shx
data/filter/ukraine_russia.dbf
data/filter/ukraine_russia.prj
```

The workflow checks all four components. Change `FILTER_SHAPEFILE` in the workflow if you use another basename.

Centroids use `within(filter_geometry)`.
Footprints use `intersects(filter_geometry)` so a footprint crossing the boundary is retained.

## Local run

Install uv, then:

```bash
uv sync
uv run python src/convert_firms.py \
  --filter data/filter/ukraine_russia.shp \
  --output parquet
```

## GitHub Actions (triggered manually)

The workflow is automatically disabled, but triggered externally every 10 minutes and can also be started manually from **Actions → Update FIRMS GeoParquet → Run workflow**.

Every run regenerates all six files from the current FIRMS 24-hour products. It does not append to previous data.

## GitHub Pages (currently disabled)

Enable GitHub Pages for the repository using the repository's `main` branch and `/ (root)` as the publishing source. `index.html` then provides links to the six Parquet files and displays `metadata.json`.

The corresponding GitHub Pages URLs will be:

```text
https://YOUR-USER.github.io/YOUR-REPOSITORY/parquet/centroids_0_6.parquet
https://YOUR-USER.github.io/YOUR-REPOSITORY/parquet/centroids_6_12.parquet
https://YOUR-USER.github.io/YOUR-REPOSITORY/parquet/centroids_12_24.parquet
https://YOUR-USER.github.io/YOUR-REPOSITORY/parquet/footprints_0_6.parquet
https://YOUR-USER.github.io/YOUR-REPOSITORY/parquet/footprints_6_12.parquet
https://YOUR-USER.github.io/YOUR-REPOSITORY/parquet/footprints_12_24.parquet
```

For GeoLibre, the raw GitHub URLs are also suitable:

```text
https://raw.githubusercontent.com/YOUR-USER/YOUR-REPOSITORY/main/parquet/centroids_0_6.parquet
```

Replace `YOUR-USER/YOUR-REPOSITORY` with the actual repository path.

## Sensors

- MODIS Collection 6.1
- Suomi-NPP VIIRS Collection 2
- NOAA-20 VIIRS Collection 2
- NOAA-21 VIIRS Collection 2

Each feature also receives `sensor`, `time_range`, `geometry_type`, and `layer` fields.


