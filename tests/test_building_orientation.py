"""Tests for stage2_irradiance/building_orientation.py — heading + wall ratio."""

import json
import math

import pandas as pd

from stage2_irradiance.building_orientation import (
    compute_building_orientation,
    compute_orientations_for_polygons,
)
from stage2_irradiance.pipeline import _load_polygon_latlons

MELBOURNE_LAT = -37.8


def _rect_polygon(lat0: float, lon0: float, width_m: float, height_m: float) -> list[list[float]]:
    """Build a lon/lat rectangle with a given east-west width and north-south height."""
    lon_scale = 111320.0 * math.cos(math.radians(lat0))
    dlon = width_m / lon_scale
    dlat = height_m / 111320.0
    return [
        [lon0, lat0],
        [lon0 + dlon, lat0],
        [lon0 + dlon, lat0 + dlat],
        [lon0, lat0 + dlat],
    ]


class TestComputeBuildingOrientation:
    def test_wide_rectangle_faces_south(self):
        # 20m east-west x 10m north-south: long wall runs east-west, so its
        # outward normal (the "heading") points north or south -- the fixed
        # winding order used here resolves to south (180 deg).
        poly = _rect_polygon(MELBOURNE_LAT, 144.9, width_m=20.0, height_m=10.0)
        result = compute_building_orientation(poly)
        assert result["azimuth_deg"] == 180.0
        assert result["wall_length_main_m"] == 20.0
        assert result["wall_length_perp_m"] == 10.0
        assert result["wall_ratio"] == 2.0

    def test_tall_rectangle_faces_east(self):
        # 10m east-west x 20m north-south: long wall now runs north-south.
        poly = _rect_polygon(MELBOURNE_LAT, 144.9, width_m=10.0, height_m=20.0)
        result = compute_building_orientation(poly)
        assert result["azimuth_deg"] == 90.0
        assert result["wall_length_main_m"] == 20.0
        assert result["wall_length_perp_m"] == 10.0
        assert result["wall_ratio"] == 2.0

    def test_square_ratio_is_one(self):
        poly = _rect_polygon(MELBOURNE_LAT, 144.9, width_m=15.0, height_m=15.0)
        result = compute_building_orientation(poly)
        assert result["wall_ratio"] == 1.0

    def test_empty_polygon_returns_none(self):
        result = compute_building_orientation([])
        assert result == {
            "azimuth_deg": None,
            "wall_length_main_m": None,
            "wall_length_perp_m": None,
            "wall_ratio": None,
        }

    def test_degenerate_polygon_returns_none(self):
        result = compute_building_orientation([[0.0, 0.0], [1.0, 1.0]])
        assert result["azimuth_deg"] is None


class TestComputeOrientationsForPolygons:
    def test_batch_preserves_order_and_row_count(self):
        wide = _rect_polygon(MELBOURNE_LAT, 144.9, width_m=20.0, height_m=10.0)
        tall = _rect_polygon(MELBOURNE_LAT, 144.9, width_m=10.0, height_m=20.0)
        df = compute_orientations_for_polygons([wide, [], tall])

        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == [
            "azimuth_deg", "wall_length_main_m", "wall_length_perp_m", "wall_ratio",
        ]
        assert len(df) == 3
        assert df.loc[0, "azimuth_deg"] == 180.0
        assert pd.isna(df.loc[1, "azimuth_deg"])
        assert df.loc[2, "azimuth_deg"] == 90.0


class TestLoadPolygonLatlons:
    def test_missing_sidecar_pads_with_empty_polygons(self, tmp_path, monkeypatch):
        monkeypatch.setattr("stage2_irradiance.pipeline.OUTPUT_DIR", tmp_path)
        result = _load_polygon_latlons("nosuchsuburb", 3)
        assert result == [[], [], []]

    def test_short_sidecar_is_padded(self, tmp_path, monkeypatch):
        monkeypatch.setattr("stage2_irradiance.pipeline.OUTPUT_DIR", tmp_path)
        sidecar = tmp_path / "stage1_testsuburb_polygons.json"
        poly = _rect_polygon(MELBOURNE_LAT, 144.9, width_m=20.0, height_m=10.0)
        with open(sidecar, "w") as fh:
            json.dump([poly], fh)

        result = _load_polygon_latlons("testsuburb", 3)
        assert len(result) == 3
        assert result[0] == poly
        assert result[1] == []
        assert result[2] == []

    def test_matching_sidecar_passes_through(self, tmp_path, monkeypatch):
        monkeypatch.setattr("stage2_irradiance.pipeline.OUTPUT_DIR", tmp_path)
        sidecar = tmp_path / "stage1_testsuburb_polygons.json"
        poly = _rect_polygon(MELBOURNE_LAT, 144.9, width_m=20.0, height_m=10.0)
        with open(sidecar, "w") as fh:
            json.dump([poly, poly], fh)

        result = _load_polygon_latlons("testsuburb", 2)
        assert result == [poly, poly]
