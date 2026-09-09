"""
Building orientation (heading) estimator for the Raising Rooves pipeline.

Fits a minimum-area bounding rectangle to each Stage 1 footprint polygon,
i.e. assumes the building is a rectangle (a simplifying assumption for FYP
scope -- most Melbourne residential footprints are close to rectangular,
and this is more robust than picking a single "longest edge" the way
Stage 1's orientation_deg does, since an L-shaped or notched footprint can
have its longest single edge running the wrong way).

From that rectangle it derives two things:
  - azimuth_deg: bearing (0-360 deg clockwise from North) of the outward
    normal to the long side of the rectangle -- the dominant heading the
    house presents to the street/sun. Like Stage 1's orientation_deg, this
    is only meaningful modulo 180 deg: footprint shape alone can't tell
    which of the two directions perpendicular to the long wall is the
    "front" of the house.
  - wall_ratio: long-side length / short-side length (>= 1). A value near 1
    means a roughly square footprint; a high value means a long, narrow
    building whose envelope is dominated by the walls facing the main
    heading (and its opposite), rather than the perpendicular end walls.

Consumed by Stage 2 to enrich the per-building table for later wall-facing
solar exposure work (see README Roadmap).
"""

import math

import pandas as pd
import shapely.geometry as sg

from shared.logging_config import setup_logging

logger = setup_logging("building_orientation")

_EMPTY_RESULT: dict = {
    "azimuth_deg": None,
    "wall_length_main_m": None,
    "wall_length_perp_m": None,
    "wall_ratio": None,
}


def compute_building_orientation(polygon_latlon: list[list[float]]) -> dict:
    """
    Estimate building heading and wall aspect ratio from a footprint polygon.

    Fits the minimum-area bounding rectangle to the polygon (projected to
    local metres), treats that rectangle as the building's true footprint,
    and reports its long/short side lengths plus the bearing of the outward
    normal to the long side.

    Args:
        polygon_latlon: [[lon, lat], ...] footprint ring, as stored in the
            Stage 1 polygon sidecar (stage1_{suburb}_polygons.json).

    Returns:
        Dict with keys: azimuth_deg, wall_length_main_m, wall_length_perp_m,
        wall_ratio. All None when the polygon is degenerate (<3 vertices)
        or rectangle fitting fails.
    """
    if not polygon_latlon or len(polygon_latlon) < 3:
        return dict(_EMPTY_RESULT)

    try:
        poly = sg.Polygon(polygon_latlon)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            return dict(_EMPTY_RESULT)
    except Exception as exc:
        logger.debug("Could not build polygon for orientation: %s", exc)
        return dict(_EMPTY_RESULT)

    # Project to local metres (equirectangular, consistent with the area/
    # orientation helpers in stage1_segmentation) so rectangle sides are metres.
    centroid_lat = poly.centroid.y
    lat_scale = 111320.0
    lon_scale = 111320.0 * math.cos(math.radians(centroid_lat))

    try:
        coords_m = [(lon * lon_scale, lat * lat_scale) for lon, lat in polygon_latlon]
        poly_m = sg.Polygon(coords_m)
        if not poly_m.is_valid:
            poly_m = poly_m.buffer(0)
        rect = poly_m.minimum_rotated_rectangle
    except Exception as exc:
        logger.debug("Could not fit minimum rotated rectangle: %s", exc)
        return dict(_EMPTY_RESULT)

    rect_coords = list(rect.exterior.coords)
    if len(rect_coords) < 4:
        return dict(_EMPTY_RESULT)

    # A rectangle's first two edges are perpendicular by construction, so
    # comparing their lengths is enough to find the long (main) side.
    (x0, y0), (x1, y1), (x2, y2) = rect_coords[0], rect_coords[1], rect_coords[2]
    edge1 = math.hypot(x1 - x0, y1 - y0)
    edge2 = math.hypot(x2 - x1, y2 - y1)

    if edge1 <= 1e-6 or edge2 <= 1e-6:
        return dict(_EMPTY_RESULT)

    if edge1 >= edge2:
        main_len, perp_len = edge1, edge2
        dx, dy = x1 - x0, y1 - y0
    else:
        main_len, perp_len = edge2, edge1
        dx, dy = x2 - x1, y2 - y1

    # Bearing of the long edge itself, then rotate 90 deg to get the
    # outward-facing normal -- same convention as Stage 1's orientation_deg.
    wall_bearing = math.degrees(math.atan2(dx, dy)) % 360.0
    azimuth_deg = round((wall_bearing + 90.0) % 360.0, 1)

    return {
        "azimuth_deg": azimuth_deg,
        "wall_length_main_m": round(main_len, 1),
        "wall_length_perp_m": round(perp_len, 1),
        "wall_ratio": round(main_len / perp_len, 2),
    }


def compute_orientations_for_polygons(
    polygon_latlons: list[list[list[float]]],
) -> pd.DataFrame:
    """
    Batch version of compute_building_orientation for a list of polygons.

    Returns a DataFrame with one row per polygon, in the same order, with
    columns azimuth_deg, wall_length_main_m, wall_length_perp_m, wall_ratio.
    """
    rows = [compute_building_orientation(p) for p in polygon_latlons]
    return pd.DataFrame(rows, columns=list(_EMPTY_RESULT.keys()))
