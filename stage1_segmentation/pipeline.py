"""
Stage 1 pipeline orchestrator for the Raising Rooves pipeline.

Chains together: tile download -> OSM building footprint query -> area aggregation.

Data source: OpenStreetMap Overpass API (no GPU, no API key, no large download).
One Overpass query covers the entire suburb bbox, so suburb-scale runs
issue a single HTTP request rather than one per tile.

Optional local source: Microsoft Australia Building Footprints GeoJSON
(pass footprint_file= to run_stage1).
"""

import json
import math
from pathlib import Path
from typing import Optional

import pandas as pd
from tqdm import tqdm

from config.settings import (
    DEFAULT_TILE_SIZE, DEFAULT_ZOOM, FLAT_PITCH_THRESHOLD_DEG, OUTPUT_DIR, TILES_DIR,
)
from config.suburbs import get_suburb
from shared.file_io import ensure_dir, save_parquet
from shared.geo_utils import compute_tile_grid, latlon_to_tile, tile_centre_latlon
from shared.logging_config import setup_logging
import cv2
import numpy as np

from stage1_segmentation.building_footprint_segmenter import (
    BuildingFootprint,
    _latlon_to_pixel,
    merge_footprints,
    query_buildings_in_bbox,
)
from stage1_segmentation.roof_classifier import classify_roof
from stage1_segmentation.stage1_visualiser import save_visualisation
from stage1_segmentation.tile_downloader import download_tiles

logger = setup_logging("stage1_pipeline")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _tile_extended_bbox(
    suburb_bbox: tuple[float, float, float, float],
    zoom: int,
    tile_size: int = DEFAULT_TILE_SIZE,
) -> tuple[float, float, float, float]:
    """
    Expand the suburb bbox to match the actual area covered by downloaded tiles.

    Each 640px tile is centred on a standard web-mercator tile, but covers
    tile_size/256 = 2.5 standard tile widths in each direction. The outermost
    tiles therefore extend ~75m beyond the suburb bbox corners. Querying only
    within the suburb bbox leaves buildings in this buffer zone without overlays
    in the annotated image. This function returns the expanded bbox so the
    Overpass/shapefile query covers everything visible in the imagery.
    """
    south, west, north, east = suburb_bbox
    tiles = compute_tile_grid(suburb_bbox, zoom)
    if not tiles:
        return suburb_bbox

    centres = [tile_centre_latlon(x, y, zoom) for x, y in tiles]
    lats = [c[0] for c in centres]
    lons = [c[1] for c in centres]
    centre_lat = sum(lats) / len(lats)

    C = 40075016.686
    mpp = C * math.cos(math.radians(centre_lat)) / (2 ** (zoom + 8))
    half_tile_m = (tile_size / 2) * mpp

    dlat = half_tile_m / 111320.0
    dlon = half_tile_m / (111320.0 * math.cos(math.radians(centre_lat)))

    return (
        min(lats) - dlat,
        min(lons) - dlon,
        max(lats) + dlat,
        max(lons) + dlon,
    )


def _classify_buildings_from_tiles(
    buildings: list[BuildingFootprint],
    suburb_key: str,
    zoom: int,
) -> dict[str, float]:
    """
    Run the HSV pixel classifier on buildings that have no roof_material from OSM.

    For each unclassified building:
      1. Locate the tile containing its centroid
      2. Load that tile image from disk
      3. Re-project the building polygon onto that tile
      4. Build a pixel mask and call classify_roof()
      5. Write material/colour back onto the BuildingFootprint in-place

    Returns a dict mapping building_id -> classifier confidence (0 if not classified).
    Only buildings missing roof_material are processed; OSM tags are never overwritten.
    """
    tile_dir = TILES_DIR / suburb_key
    tile_cache: dict[tuple[int, int], np.ndarray | None] = {}
    confidences: dict[str, float] = {}

    n_osm_tagged = 0
    n_no_polygon = 0
    n_no_tile = 0
    n_degenerate = 0
    n_too_few_pixels = 0
    n_classified = 0

    for bldg in buildings:
        # Skip if OSM already has a material tag
        if bldg.roof_material is not None:
            confidences[bldg.building_id] = 1.0
            n_osm_tagged += 1
            continue

        if not bldg.polygon_latlon or len(bldg.polygon_latlon) < 3:
            confidences[bldg.building_id] = 0.0
            n_no_polygon += 1
            continue

        lons = [c[0] for c in bldg.polygon_latlon]
        lats = [c[1] for c in bldg.polygon_latlon]
        centroid_lat = sum(lats) / len(lats)
        centroid_lon = sum(lons) / len(lons)

        # Find tile containing this building's centroid
        tx, ty = latlon_to_tile(centroid_lat, centroid_lon, zoom)
        tile_lat, tile_lon = tile_centre_latlon(tx, ty, zoom)

        # Load tile image (cached)
        if (tx, ty) not in tile_cache:
            tile_path = tile_dir / f"{suburb_key}_{zoom}_{tx}_{ty}.png"
            if tile_path.exists():
                img_bgr = cv2.imread(str(tile_path))
                tile_cache[(tx, ty)] = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB) if img_bgr is not None else None
            else:
                tile_cache[(tx, ty)] = None

        tile_img = tile_cache[(tx, ty)]
        if tile_img is None:
            confidences[bldg.building_id] = 0.0
            n_no_tile += 1
            logger.debug(
                "Building %s: centroid tile (%d,%d) not downloaded — skipping classifier",
                bldg.building_id, tx, ty,
            )
            continue

        h, w = tile_img.shape[:2]

        # Project polygon onto this tile — _latlon_to_pixel clamps vertices to
        # [0, tile_size-1], so a building that projects mostly outside the tile
        # will collapse to a line or point. Guard against this before masking.
        pts = np.array([
            _latlon_to_pixel(lat, lon, tile_lat, tile_lon, zoom, w)
            for lon, lat in bldg.polygon_latlon
        ], dtype=np.int32)

        # Detect degenerate projection: all x-coords equal or all y-coords equal
        if pts[:, 0].max() == pts[:, 0].min() or pts[:, 1].max() == pts[:, 1].min():
            confidences[bldg.building_id] = 0.0
            n_degenerate += 1
            logger.debug(
                "Building %s: polygon projects to a line on tile (%d,%d) — "
                "building likely lies mostly outside this tile; skipping classifier",
                bldg.building_id, tx, ty,
            )
            continue

        # Build mask
        mask_img = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(mask_img, [pts], 255)
        mask = mask_img > 0

        if mask.sum() < 5:  # too few pixels to classify reliably
            confidences[bldg.building_id] = 0.0
            n_too_few_pixels += 1
            logger.debug(
                "Building %s: only %d pixels inside projected polygon on tile (%d,%d) — "
                "skipping classifier (building may straddle tile boundary)",
                bldg.building_id, int(mask.sum()), tx, ty,
            )
            continue

        result = classify_roof(tile_img, mask, segment_id=int(bldg.building_id.lstrip("r")) if bldg.building_id.lstrip("r").isdigit() else 0)

        # Apply per-suburb image quality calibration (Gemini-validated, 2026-08-11)
        from config.settings import SUBURB_CLASSIFIER_QUALITY
        quality_mult = SUBURB_CLASSIFIER_QUALITY.get(suburb_key, 0.85)

        bldg.roof_material = result.material.value
        bldg.roof_colour = result.colour.value
        bldg.absorptance_estimate = result.absorptance_estimate
        bldg.absorptance_uncertainty = min(0.25, result.absorptance_uncertainty / quality_mult)
        confidences[bldg.building_id] = round(result.confidence * quality_mult, 2)
        n_classified += 1

    logger.info(
        "Pixel classifier: %d classified | %d had OSM tags (skipped) | "
        "%d no centroid tile | %d degenerate projection | %d too few pixels | "
        "%d no polygon | %d total",
        n_classified, n_osm_tagged, n_no_tile, n_degenerate,
        n_too_few_pixels, n_no_polygon, len(buildings),
    )
    return confidences


def _assumed_pitch_deg(
    building_type: str | None,
    roof_shape: str | None,
    levels: int | None,
) -> tuple[float, str]:
    """
    Return an assumed roof pitch (degrees) and the basis for that assumption,
    based on available building attributes.

    Priority: explicit roof_shape tag > multi-storey override > building_type lookup
    > residential default. Default is 22.5° for residential/generic "yes" buildings.
    Gemini 2.5 Flash validation on 507 Melbourne buildings (Clayton mean 5.7°,
    Carlton mean 3.7°) had previously calibrated this down to 12°; reverted per Ryan.

    We do not measure pitch from LiDAR/DSM — trialled and dropped, see
    DECISION_LOG.md 2026-08-17 (elevation data wasn't precise enough for
    reliable per-building plane fits). Pitch is assumed for every building;
    the returned basis string (written to the pitch_basis column) records
    which rule produced the value so assumptions stay auditable.

    Returns:
        (pitch_deg, pitch_basis) — pitch_basis is one of:
            "roof_shape:<tag>"   — OSM roof:shape tag matched directly
            "levels>=4"          — multi-storey override
            "building_type:<tag>" — OSM building type/class lookup
            "residential_default" — fallback for residential / untyped buildings
    """
    # If OSM has an explicit roof shape, use that directly
    if roof_shape:
        shape = roof_shape.lower()
        basis = f"roof_shape:{shape}"
        if shape == "flat":
            return 0.0, basis
        if shape in ("gabled", "hipped", "half-hipped"):
            return 15.0, basis
        if shape == "pyramidal":
            return 20.0, basis
        if shape == "skillion":
            return 10.0, basis
        if shape in ("dome", "onion"):
            return 25.0, basis

    # Multi-storey buildings (4+ floors) are almost always flat-roofed
    if levels is not None and levels >= 4:
        return 0.0, "levels>=4"

    # Building type lookup
    _FLAT = 0.0
    _LOW = 5.0
    _SHALLOW = 10.0
    _TYPICAL = 22.5
    _STEEP = 22.5

    flat_types = {"commercial", "retail", "office", "shop", "supermarket", "hotel",
                  "hospital", "civic", "public", "service", "transportation"}
    low_types = {"industrial", "warehouse", "factory", "hangar", "storage",
                 "sports_hall", "stadium"}
    shallow_types = {"garage", "carport", "shed", "greenhouse", "roof",
                     "school", "university", "college", "kindergarten"}
    steep_types = {"church", "cathedral", "chapel", "temple", "mosque", "synagogue"}

    if building_type:
        bt = building_type.lower()
        basis = f"building_type:{bt}"
        if bt in flat_types:
            return _FLAT, basis
        if bt in low_types:
            return _LOW, basis
        if bt in shallow_types:
            return _SHALLOW, basis
        if bt in steep_types:
            return _STEEP, basis

    # Residential types and generic "yes"
    return _TYPICAL, "residential_default"


def _orientation_from_polygon(polygon_latlon: list[list[float]]) -> Optional[float]:
    """
    Compute orientation as the bearing of the normal to the longest footprint wall.

    The longest wall approximates the building's primary axis. Its outward normal
    gives the dominant direction a roof face slopes toward.

    Returns bearing in degrees clockwise from North (0–360), or None for
    degenerate polygons (fewer than 2 distinct vertices).
    """
    if len(polygon_latlon) < 2:
        return None

    lat_scale = 111320.0
    lon_scale = 111320.0 * math.cos(math.radians(
        sum(c[1] for c in polygon_latlon) / len(polygon_latlon)
    ))

    best_len = -1.0
    best_dx = 0.0
    best_dy = 0.0

    pts = polygon_latlon
    n = len(pts)
    for i in range(n):
        lon0, lat0 = pts[i][0], pts[i][1]
        lon1, lat1 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        dx = (lon1 - lon0) * lon_scale   # east component (m)
        dy = (lat1 - lat0) * lat_scale   # north component (m)
        length = math.hypot(dx, dy)
        if length > best_len:
            best_len = length
            best_dx = dx
            best_dy = dy

    if best_len < 1e-6:
        return None

    # Wall bearing: angle of the edge itself from North (atan2 east, north)
    wall_bearing = math.degrees(math.atan2(best_dx, best_dy)) % 360.0
    # Normal to the wall (rotated 90° clockwise)
    return round((wall_bearing + 90.0) % 360.0, 1)


def _building_to_row(
    building: BuildingFootprint,
    suburb_name: str,
    idx: int,
    classifier_confidence: float = 0.0,
) -> dict:
    """Convert a BuildingFootprint to a DataFrame-ready dict."""
    lons = [c[0] for c in building.polygon_latlon]
    lats = [c[1] for c in building.polygon_latlon]
    centroid_lat = sum(lats) / len(lats) if lats else 0.0
    centroid_lon = sum(lons) / len(lons) if lons else 0.0

    pitch_deg, pitch_basis = _assumed_pitch_deg(
        building.building_type, building.roof_shape, building.levels
    )
    is_flat = pitch_deg < FLAT_PITCH_THRESHOLD_DEG

    # Actual roof surface area accounts for pitch — for flat roofs it equals footprint area.
    if pitch_deg > 0.5:
        roof_surface_area_m2 = round(building.area_m2 / math.cos(math.radians(pitch_deg)), 1)
    else:
        roof_surface_area_m2 = building.area_m2

    return {
        "suburb": suburb_name,
        "building_id": building.building_id,
        "roof_id": f"{suburb_name.lower().replace(' ', '_')}_{building.building_id}",
        "area_m2": building.area_m2,
        "roof_surface_area_m2": roof_surface_area_m2,
        "lat": round(centroid_lat, 6),
        "lon": round(centroid_lon, 6),
        "source": building.source,
        "building_type": building.building_type,
        "levels": building.levels,
        "roof_material": building.roof_material,
        "roof_colour": building.roof_colour,
        "roof_shape": building.roof_shape,
        "pitch_deg": pitch_deg,
        "pitch_basis": pitch_basis,
        "pitch_source": "assumed",
        "is_flat": is_flat,
        "orientation_deg": _orientation_from_polygon(building.polygon_latlon),
        "classifier_confidence": round(classifier_confidence, 2),
        "absorptance_estimate": round(building.absorptance_estimate, 3) if building.absorptance_estimate is not None else None,
        "absorptance_uncertainty": round(building.absorptance_uncertainty, 3) if building.absorptance_uncertainty is not None else None,
    }


def _query_pipeline_footprints(
    south: float,
    west: float,
    north: float,
    east: float,
    footprint_file: Path | None = None,
    merge_footprint_file: Path | None = None,
) -> list[BuildingFootprint]:
    """Query Stage 1 footprints with a local fallback when OSM is unavailable."""
    if footprint_file:
        return query_buildings_in_bbox(
            south=south,
            west=west,
            north=north,
            east=east,
            local_file=footprint_file,
        )

    if not merge_footprint_file:
        return query_buildings_in_bbox(
            south=south,
            west=west,
            north=north,
            east=east,
        )

    try:
        buildings = query_buildings_in_bbox(
            south=south,
            west=west,
            north=north,
            east=east,
        )
    except RuntimeError as exc:
        logger.warning(
            "OSM footprint query failed: %s. Falling back to local footprint file: %s",
            exc,
            merge_footprint_file.name,
        )
        return query_buildings_in_bbox(
            south=south,
            west=west,
            north=north,
            east=east,
            local_file=merge_footprint_file,
        )

    logger.info("Merging with local file: %s...", merge_footprint_file.name)
    secondary = query_buildings_in_bbox(
        south=south,
        west=west,
        north=north,
        east=east,
        local_file=merge_footprint_file,
    )
    return merge_footprints(buildings, secondary)


# ── Main pipeline ─────────────────────────────────────────────────────────────


def run_stage1(
    suburb_name: str,
    zoom: int = DEFAULT_ZOOM,
    skip_download: bool = False,
    max_tiles: int | None = None,
    footprint_file: Path | None = None,
    merge_footprint_file: Path | None = None,
) -> pd.DataFrame:
    """
    Run the full Stage 1 pipeline for a single suburb.

    Steps:
        1. Look up suburb config (bbox)
        2. Download satellite tiles for visual reference (or skip)
        3. Query building footprints (OSM and/or local file)
        4. Aggregate and save results to Parquet
        5. Generate annotated visualisation PNG

    Args:
        suburb_name: Suburb to process (must be in config/suburbs.py).
        zoom: Zoom level for tile download (default 19).
        skip_download: Skip tile download (tiles only used as visual reference).
        max_tiles: Cap tiles downloaded (smoke-test mode).
        footprint_file: Use ONLY this local file (SHP/GeoJSON) — skips OSM.
        merge_footprint_file: Merge this local file WITH OSM. OSM buildings are
            kept as primary; local buildings not overlapping OSM are added.

    Returns:
        DataFrame with columns: suburb, building_id, roof_id, area_m2, lat, lon, source,
        building_type, levels, roof_material, roof_colour, roof_shape, pitch_deg,
        pitch_basis, pitch_source, classifier_confidence.
    """
    suburb = get_suburb(suburb_name)
    suburb_key = suburb.key
    south, west, north, east = suburb.bbox

    logger.info("=" * 60)
    logger.info("Stage 1 Pipeline: %s (zoom=%d)", suburb.name, zoom)
    logger.info("Suburb bbox: (%.5f, %.5f) -> (%.5f, %.5f)", south, west, north, east)
    logger.info("=" * 60)

    # ── Step 1: Download satellite tiles (visual reference) ───────────────
    if skip_download:
        logger.info("Step 1/6: Skipping tile download (--skip-download).")
    else:
        logger.info("Step 1/6: Downloading satellite tiles...")
        tile_paths = download_tiles(suburb.name, suburb.bbox, zoom)
        if max_tiles and len(tile_paths) > max_tiles:
            logger.info("Smoke-test: capping at %d/%d tiles.", max_tiles, len(tile_paths))
            tile_paths = tile_paths[:max_tiles]
        logger.info("Downloaded %d tiles.", len(tile_paths))

    # Expand query bbox to match actual tile imagery coverage (~75m beyond suburb bbox)
    # so buildings visible at the edge of tiles get polygon overlays too.
    qs, qw, qn, qe = _tile_extended_bbox(suburb.bbox, zoom)
    logger.info("Query bbox (tile-extended): (%.5f, %.5f) -> (%.5f, %.5f)", qs, qw, qn, qe)

    # ── Step 2: Query building footprints for the whole suburb ────────────
    if merge_footprint_file:
        source_label = f"OSM + {merge_footprint_file.name}"
    elif footprint_file:
        source_label = f"local file: {footprint_file.name}"
    else:
        source_label = "OSM Overpass API"
    logger.info("Step 2/6: Querying building footprints via %s...", source_label)

    try:
        buildings = _query_pipeline_footprints(
            south=qs,
            west=qw,
            north=qn,
            east=qe,
            footprint_file=footprint_file,
            merge_footprint_file=merge_footprint_file,
        )
    except (RuntimeError, FileNotFoundError) as exc:
        logger.error("Footprint query failed: %s", exc)
        return pd.DataFrame()

    if not buildings:
        logger.warning("No buildings found in %s. Check suburb bbox.", suburb_name)
        return pd.DataFrame()

    logger.info("Found %d buildings in %s.", len(buildings), suburb_name)

    # ── Step 3: Pixel classifier — fill missing roof_material/colour ──────
    logger.info("Step 3/6: Running pixel classifier for buildings without OSM roof tags...")
    confidences = _classify_buildings_from_tiles(buildings, suburb_key, zoom)

    # ── Step 4: Build DataFrame ───────────────────────────────────────────
    logger.info("Step 4/6: Aggregating results...")

    rows = []
    for i, bldg in enumerate(tqdm(buildings, desc="Processing buildings")):
        rows.append(_building_to_row(bldg, suburb.name, i, confidences.get(bldg.building_id, 0.0)))

    df = pd.DataFrame(rows)

    # Summary stats
    total_area = df["area_m2"].sum()
    mean_area = df["area_m2"].mean()
    osm_tagged = df["classifier_confidence"].eq(1.0).sum()
    classifier_tagged = df[(df["classifier_confidence"] > 0) & (df["classifier_confidence"] < 1.0)].shape[0]
    logger.info(
        "Suburb %s: %d buildings | total %.0f m2 | mean %.0f m2/building",
        suburb.name, len(df), total_area, mean_area,
    )
    logger.info(
        "Roof material coverage: %d from OSM tags, %d from pixel classifier, %d unclassified",
        osm_tagged, classifier_tagged, len(df) - osm_tagged - classifier_tagged,
    )

    # ── Step 5: Save Parquet + CSV + polygon sidecar ─────────────────────
    logger.info("Step 5/6: Saving outputs...")
    out_dir = ensure_dir(OUTPUT_DIR)
    parquet_path = out_dir / f"stage1_{suburb_key}.parquet"
    csv_path = out_dir / f"stage1_{suburb_key}.csv"
    polygons_path = out_dir / f"stage1_{suburb_key}_polygons.json"

    save_parquet(df, parquet_path)
    df.to_csv(csv_path, index=False)
    logger.info("Parquet: %s", parquet_path)
    logger.info("CSV:     %s", csv_path)

    # Polygon sidecar — list of [[lon, lat], ...] in the same row order as the
    # parquet. Used by tools/visualise_results.py to draw building outlines.
    polygon_list = [b.polygon_latlon for b in buildings]
    with open(polygons_path, "w") as fh:
        json.dump(polygon_list, fh, separators=(",", ":"))
    logger.info("Polygons sidecar: %s", polygons_path)

    # GeoJSON — one feature per building; all attributed columns as properties.
    geojson_path = out_dir / f"stage1_{suburb_key}.geojson"
    features = []
    for row, bldg in zip(rows, buildings):
        props = {k: v for k, v in row.items()}
        geom = {
            "type": "Polygon",
            "coordinates": [bldg.polygon_latlon],
        }
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    geojson_doc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    with open(geojson_path, "w") as fh:
        json.dump(geojson_doc, fh, separators=(",", ":"))
    logger.info("GeoJSON: %s  (%d features)", geojson_path, len(features))

    # ── Step 6: Visualise ─────────────────────────────────────────────────
    logger.info("Step 6/6: Generating annotated visualisation...")
    img_path = save_visualisation(suburb.name, buildings, zoom)
    if img_path:
        logger.info("Annotated image: %s", img_path)

    logger.info("=" * 60)
    logger.info("Stage 1 complete for %s.", suburb.name)
    logger.info("=" * 60)

    return df
