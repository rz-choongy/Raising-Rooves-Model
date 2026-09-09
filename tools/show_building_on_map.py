"""
Render a single building's footprint on an interactive folium map.

Loads the highest available stage output (Stage 3 > Stage 2 > Stage 1) so the
popup shows whatever physics results exist for that building, plus its polygon
from the Stage 1 sidecar.

Usage:
    python -m tools.show_building_on_map --suburb Carlton --building-id 22820458
"""

from __future__ import annotations

import argparse
import json
import sys

import folium
import pandas as pd

from config.settings import OUTPUT_DIR
from config.suburbs import get_suburb
from shared.logging_config import setup_logging

logger = setup_logging("show_building_on_map")


def _load_best_stage(suburb_key: str) -> tuple[pd.DataFrame | None, int]:
    for stage in (3, 2, 1):
        path = OUTPUT_DIR / f"stage{stage}_{suburb_key}.csv"
        if path.exists():
            return pd.read_csv(path, dtype={"building_id": str}), stage
    return None, 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Show a single building on a folium map.")
    parser.add_argument("--suburb", type=str, required=True)
    parser.add_argument("--building-id", type=str, required=True)
    args = parser.parse_args()

    suburb = get_suburb(args.suburb)
    suburb_key = suburb.key

    df, stage = _load_best_stage(suburb_key)
    if df is None:
        logger.error("No Stage 1/2/3 output found for %s. Run Stage 1 first.", suburb.name)
        sys.exit(1)

    matches = df.index[df["building_id"] == args.building_id]
    if len(matches) == 0:
        logger.error("Building %s not found in stage%d_%s.csv", args.building_id, stage, suburb_key)
        sys.exit(1)
    idx = matches[0]
    row = df.loc[idx]

    sidecar = OUTPUT_DIR / f"stage1_{suburb_key}_polygons.json"
    if not sidecar.exists():
        logger.error("No polygon sidecar found at %s.", sidecar)
        sys.exit(1)
    polygons = json.load(open(sidecar, encoding="utf-8"))
    if idx >= len(polygons) or not polygons[idx]:
        logger.error("No polygon available for building %s (index %d).", args.building_id, idx)
        sys.exit(1)
    poly_lonlat = polygons[idx]
    poly_latlon = [[lat, lon] for lon, lat in poly_lonlat]

    centre_lat = float(row["lat"])
    centre_lon = float(row["lon"])

    fmap = folium.Map(location=[centre_lat, centre_lon], zoom_start=19, tiles=None)
    folium.TileLayer(
        tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="OpenStreetMap contributors",
        name="Street",
    ).add_to(fmap)
    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        attr="Esri World Imagery",
        name="Satellite",
    ).add_to(fmap)

    def _fmt(key: str, suffix: str = "") -> str:
        val = row.get(key)
        if val is None or str(val) == "nan":
            return "n/a"
        if isinstance(val, float):
            return f"{val:,.1f}{suffix}"
        return f"{val}{suffix}"

    popup_lines = [
        f"<b>Building {row['building_id']}</b> ({suburb.name})",
        f"Footprint area: {_fmt('area_m2', ' m²')}",
        f"Roof surface area: {_fmt('roof_surface_area_m2', ' m²')}",
        f"Type: {_fmt('building_type')} · Levels: {_fmt('levels')}",
        f"Roof: {_fmt('roof_material')} / {_fmt('roof_colour')} · Pitch: {_fmt('pitch_deg', '°')} ({_fmt('pitch_source')})",
    ]
    if "energy_saved_kwh_yr" in row:
        popup_lines.append(f"Energy saved: {_fmt('energy_saved_kwh_yr', ' kWh/yr')}")
    if "electricity_saved_kwh_yr" in row:
        popup_lines.append(f"Electricity saved: {_fmt('electricity_saved_kwh_yr', ' kWh/yr')}")
    if "co2_saved_kg_yr" in row:
        popup_lines.append(f"CO2 saved: {_fmt('co2_saved_kg_yr', ' kg/yr')}")
    popup_html = "<br>".join(popup_lines)

    folium.Polygon(
        locations=poly_latlon,
        color="#e63946",
        weight=3,
        fill=True,
        fill_color="#e63946",
        fill_opacity=0.35,
        popup=folium.Popup(popup_html, max_width=320),
        tooltip=f"Building {row['building_id']}",
    ).add_to(fmap)

    folium.Marker(
        location=[centre_lat, centre_lon],
        icon=folium.Icon(color="red", icon="home"),
    ).add_to(fmap)

    folium.LayerControl().add_to(fmap)

    out_path = OUTPUT_DIR / f"{suburb_key}_building_{args.building_id}_map.html"
    fmap.save(str(out_path))
    logger.info("Map saved to %s", out_path)
    print(str(out_path))


if __name__ == "__main__":
    main()
