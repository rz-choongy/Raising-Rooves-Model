"""
One-time tool: convert a large building footprint GeoJSONL (Overture / Microsoft AU)
into a spatially-indexed GeoPackage so that per-suburb queries run in ~0.1 s instead
of scanning the full file (~23 s for a 1 GB GeoJSONL).

Run once after downloading the source file:
    python -m tools.build_footprint_index

Or point at a custom source:
    python -m tools.build_footprint_index --input data/raw/footprints/melbourne_overture.geojsonl
    python -m tools.build_footprint_index --input data/raw/footprints/australia.geojson

Output:
    data/raw/footprints/buildings_index.gpkg   (default)

After this runs, Stage 1 will automatically use the index for --merge-footprint-file
queries without any extra flags.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import RAW_DIR
from shared.logging_config import setup_logging

logger = setup_logging("build_footprint_index")

DEFAULT_INPUT  = RAW_DIR / "footprints" / "melbourne_overture.geojsonl"
DEFAULT_OUTPUT = RAW_DIR / "footprints" / "buildings_index.gpkg"

# Minimum polygon area to include (filters slivers / mapping noise)
_MIN_AREA_M2 = 10.0


def build_index(source: Path, output: Path, chunk_size: int = 50_000) -> None:
    """
    Stream source GeoJSONL → GeoPackage in chunks to stay memory-efficient.

    Each chunk is appended to the GeoPackage so peak RAM stays around
    chunk_size * ~2 kB ≈ 100 MB regardless of total file size.

    Args:
        source:     Path to GeoJSONL (line-delimited GeoJSON) input file.
        output:     Destination GeoPackage (.gpkg) path.
        chunk_size: Records processed per geopandas write batch.
    """
    try:
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import shape, Polygon, MultiPolygon
    except ImportError as exc:
        raise ImportError(
            "geopandas and shapely are required. "
            "Install with: pip install geopandas shapely"
        ) from exc

    import json

    if not source.exists():
        raise FileNotFoundError(
            f"Source file not found: {source}\n"
            "Fetch it from Overture Maps' public S3 bucket (no key needed):\n"
            "  pip install overturemaps\n"
            "  python -m overturemaps download --bbox=144.8110,-38.1590,145.2350,-37.6310 \\\n"
            "    -f geojsonseq -t building -o data/raw/footprints/melbourne_overture.geojsonl\n"
            "See README.md 'Data Needed' for details."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        logger.info("Removing existing index: %s", output)
        output.unlink()

    logger.info("Source : %s (%.0f MB)", source.name, source.stat().st_size / 1_048_576)
    logger.info("Output : %s", output)
    logger.info("Building spatial index (this runs once, ~2–5 min)...")

    t0 = time.time()
    total_written = 0
    total_skipped = 0
    chunk_records: list[dict] = []
    first_write = True

    def _flush(records: list[dict]) -> None:
        nonlocal first_write, total_written
        if not records:
            return
        gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
        mode = "w" if first_write else "a"
        gdf.to_file(str(output), driver="GPKG", layer="buildings", mode=mode)
        first_write = False
        total_written += len(records)

    def _feature_to_record(feat: dict) -> dict | None:
        """Extract geometry + key properties from one GeoJSON feature."""
        geom_raw = feat.get("geometry")
        if not geom_raw:
            return None
        try:
            geom = shape(geom_raw)
        except Exception:
            return None

        # Normalise MultiPolygon → largest Polygon
        if isinstance(geom, MultiPolygon):
            geom = max(geom.geoms, key=lambda g: g.area)
        if not isinstance(geom, Polygon) or geom.is_empty:
            return None

        # Rough area filter — 1 deg² ≈ 111320² m² at Melbourne lat
        # A 10 m² roof ≈ 8e-10 deg² — use a tiny threshold, not exact
        if geom.area < 1e-10:
            return None

        props = feat.get("properties") or {}

        # Detect primary source dataset
        sources = props.get("sources", [])
        if isinstance(sources, list) and sources:
            dataset = sources[0].get("dataset", "unknown")
        else:
            dataset = "unknown"

        # Common property names across Overture / Microsoft formats
        height = props.get("height") or props.get("num_floors")
        feat_id = (
            props.get("id") or props.get("ID")
            or props.get("UFI") or props.get("ufi")
            or props.get("OBJECTID")
        )

        return {
            "geometry": geom,
            "feat_id":  str(feat_id) if feat_id else "",
            "dataset":  dataset,
            "height":   float(height) if height is not None else None,
        }

    with open(source, encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                total_skipped += 1
                continue

            # Handle both bare Feature and FeatureCollection lines
            if obj.get("type") == "FeatureCollection":
                features = obj.get("features", [])
            elif obj.get("type") == "Feature":
                features = [obj]
            else:
                continue

            for feat in features:
                rec = _feature_to_record(feat)
                if rec is None:
                    total_skipped += 1
                    continue
                chunk_records.append(rec)

            if len(chunk_records) >= chunk_size:
                _flush(chunk_records)
                chunk_records = []
                elapsed = time.time() - t0
                logger.info(
                    "  written %7d | skipped %5d | lines %7d | %.0f s elapsed",
                    total_written, total_skipped, line_no, elapsed,
                )

    # Final flush
    _flush(chunk_records)

    elapsed = time.time() - t0
    size_mb = output.stat().st_size / 1_048_576 if output.exists() else 0
    logger.info("=" * 55)
    logger.info("Done in %.0f s", elapsed)
    logger.info("Buildings written : %d", total_written)
    logger.info("Records skipped   : %d", total_skipped)
    logger.info("Output size       : %.0f MB — %s", size_mb, output)
    logger.info("=" * 55)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert building footprint GeoJSONL → spatially-indexed GeoPackage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input", type=Path, default=DEFAULT_INPUT,
        help=f"Source GeoJSONL file (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"Destination GeoPackage (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=50_000,
        help="Records per write batch (default: 50000)",
    )
    args = parser.parse_args()

    try:
        build_index(args.input, args.output, args.chunk_size)
    except (FileNotFoundError, ImportError, RuntimeError) as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
