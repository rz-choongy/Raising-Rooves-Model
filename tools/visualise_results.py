"""
Stage 2 results visualisation tool for Raising Rooves.

Produces three outputs for a given suburb:
  1. Building-level choropleth map (folium HTML) — energy_saved_kwh_yr coloured polygons
  2. Summary charts (matplotlib 2×2 PNG) — distributions and stats
  3. Simple HTML report — key numbers, embedded chart, links to map

Usage:
    python -m tools.visualise_results --suburb Carlton
    python -m tools.visualise_results --suburb Carlton --stage2-file data/output/stage2_carlton.parquet
    python -m tools.visualise_results --suburb Carlton --debug
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import folium
import matplotlib
matplotlib.use("Agg")  # non-interactive backend — must be set before pyplot import
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd
from matplotlib.patches import FancyBboxPatch

from config.settings import OUTPUT_DIR
from shared.logging_config import setup_logging

logger = setup_logging("visualise_results")

# ── Constants ─────────────────────────────────────────────────────────────────

REQUIRED_STAGE2_COLS = {
    "building_id",
    "lat",
    "lon",
    "area_m2",
    "roof_material",
    "energy_saved_kwh_yr",
    "co2_saved_kg_yr",
}

REQUIRED_STAGE3_COLS = {
    "electricity_saved_kwh_yr",
    "co2_electricity_saved_kg_yr",
    "net_electricity_saved_kwh_yr",
    "heating_penalty_electricity_kwh_yr",
}

# AER State of the Energy Market 2023 — Victorian residential average
HOUSEHOLDS_KWH_YR = 4_200

# Sequential colormap for choropleth: low savings (light) → high (dark)
CHOROPLETH_CMAP = "YlOrRd"


# ── Data loading ──────────────────────────────────────────────────────────────


def load_stage2(suburb_key: str, stage2_file: Optional[Path] = None) -> pd.DataFrame:
    """
    Load Stage 2 output for the suburb.

    Raises SystemExit with a clear message if the file is missing or columns
    are absent.
    """
    if stage2_file is None:
        # Try parquet first, CSV as fallback
        parquet_path = OUTPUT_DIR / f"stage2_{suburb_key}.parquet"
        csv_path = OUTPUT_DIR / f"stage2_{suburb_key}.csv"
        if parquet_path.exists():
            stage2_file = parquet_path
        elif csv_path.exists():
            stage2_file = csv_path
        else:
            logger.error(
                "Stage 2 output not found for suburb '%s'. "
                "Run Stage 2 first: python -m stage2_irradiance.run_stage2 --suburb %s",
                suburb_key, suburb_key.replace("_", " ").title(),
            )
            sys.exit(1)

    if not stage2_file.exists():
        logger.error(
            "Stage 2 file not found: %s. "
            "Run Stage 2 first: python -m stage2_irradiance.run_stage2 --suburb %s",
            stage2_file, suburb_key.replace("_", " ").title(),
        )
        sys.exit(1)

    logger.info("Loading Stage 2 data from %s", stage2_file)
    if stage2_file.suffix == ".parquet":
        df = pd.read_parquet(stage2_file)
    else:
        df = pd.read_csv(stage2_file)

    missing = REQUIRED_STAGE2_COLS - set(df.columns)
    if missing:
        logger.error(
            "Stage 2 file is missing required columns: %s. "
            "Re-run Stage 2 to regenerate the output.",
            sorted(missing),
        )
        sys.exit(1)

    logger.info("Loaded %d buildings from Stage 2 output.", len(df))
    return df


def load_stage3(suburb_key: str) -> pd.DataFrame | None:
    """
    Load Stage 3 output for the suburb, or return None if not yet generated.

    Stage 3 parquet contains all Stage 2 columns plus the transient
    heat-ingress model's thermal columns. When present it supersedes Stage 2
    as the primary data source.
    """
    parquet_path = OUTPUT_DIR / f"stage3_{suburb_key}.parquet"
    csv_path = OUTPUT_DIR / f"stage3_{suburb_key}.csv"
    if parquet_path.exists():
        df = pd.read_parquet(parquet_path)
    elif csv_path.exists():
        df = pd.read_csv(csv_path)
    else:
        return None

    missing = REQUIRED_STAGE3_COLS - set(df.columns)
    if missing:
        logger.warning("Stage 3 file missing columns %s — ignoring Stage 3 data.", sorted(missing))
        return None

    logger.info("Loaded Stage 3 data (%d buildings) from %s", len(df), parquet_path.name)
    return df


def load_polygons(suburb_key: str) -> dict[str, list[list[float]]] | None:
    """
    Load the Stage 1 polygon sidecar JSON and return a building_id → polygon dict.

    The sidecar is a positionally-ordered list aligned with Stage 1 parquet rows.
    We load the Stage 1 parquet to recover building_id keys, then build a mapping
    so that the choropleth can handle Stage 2 rows that are a subset (or superset
    with VicMap additions) of Stage 1.

    Returns None if the sidecar is missing.
    """
    sidecar = OUTPUT_DIR / f"stage1_{suburb_key}_polygons.json"
    if not sidecar.exists():
        logger.warning(
            "Polygon sidecar not found at %s — map will use circle markers instead of polygons.",
            sidecar,
        )
        return None

    stage1_parquet = OUTPUT_DIR / f"stage1_{suburb_key}.parquet"
    stage1_csv = OUTPUT_DIR / f"stage1_{suburb_key}.csv"
    if stage1_parquet.exists():
        s1_df = pd.read_parquet(stage1_parquet, columns=["building_id"])
    elif stage1_csv.exists():
        s1_df = pd.read_csv(stage1_csv, usecols=["building_id"])
    else:
        logger.warning(
            "Stage 1 parquet not found for %s — cannot key polygons by building_id; "
            "falling back to circle markers.",
            suburb_key,
        )
        return None

    with open(sidecar, encoding="utf-8") as fh:
        polygon_list: list[list[list[float]]] = json.load(fh)

    if len(polygon_list) != len(s1_df):
        logger.warning(
            "Polygon sidecar has %d entries but Stage 1 parquet has %d rows — "
            "falling back to circle markers.",
            len(polygon_list), len(s1_df),
        )
        return None

    polygon_map: dict[str, list[list[float]]] = {
        str(bid): poly
        for bid, poly in zip(s1_df["building_id"].astype(str), polygon_list)
    }
    logger.info("Built polygon map for %d buildings.", len(polygon_map))
    return polygon_map


# ── Output 1: Folium choropleth map ──────────────────────────────────────────


def _energy_to_hex(value: float, vmin: float, vmax: float, cmap_name: str = CHOROPLETH_CMAP) -> str:
    """Map a scalar energy value to a hex colour string."""
    cmap = plt.get_cmap(cmap_name)
    norm_val = (value - vmin) / (vmax - vmin) if vmax > vmin else 0.5
    norm_val = float(np.clip(norm_val, 0.0, 1.0))
    r, g, b, _ = cmap(norm_val)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def build_choropleth_map(
    df: pd.DataFrame,
    polygons: dict[str, list[list[float]]] | None,
    suburb_key: str,
    suburb_name: str,
    use_polygons: bool = False,
) -> Path:
    """
    Build a folium HTML map with per-building choropleth colouring.

    Default mode (use_polygons=False): renders each building as a sized circle at
    its centroid. One Point per building → ~2 coordinates in GeoJSON vs 10-20 for
    a polygon. File stays under ~1 MB and the browser holds much less memory.
    Circle radius is scaled by the primary metric so relative magnitude is visible.

    Polygon mode (use_polygons=True): renders full building outlines. Only practical
    for suburbs with <1000 buildings; pass --polygons on the CLI.
    """
    has_stage3 = "electricity_saved_kwh_yr" in df.columns
    colour_col = "electricity_saved_kwh_yr" if has_stage3 else "energy_saved_kwh_yr"
    colour_label = "Electricity saved (kWh/yr)" if has_stage3 else "Energy saved (kWh/yr)"

    centre_lat = float(df["lat"].mean())
    centre_lon = float(df["lon"].mean())

    fmap = folium.Map(
        location=[centre_lat, centre_lon],
        zoom_start=15,
        tiles="CartoDB positron",
        prefer_canvas=True,
    )

    values = df[colour_col].fillna(0)
    vmin = float(values.min())
    vmax = float(values.max())
    vp95 = float(values.quantile(0.95)) if vmax > vmin else vmax  # cap colour scale at p95

    # Circle radius: log-scaled between 3 and 14 px so outliers don't swamp small buildings
    v_log_min = np.log1p(max(vmin, 0))
    v_log_max = np.log1p(vp95) if vp95 > 0 else 1.0

    def _radius(v: float) -> float:
        v_log = np.log1p(max(v, 0))
        frac = (v_log - v_log_min) / (v_log_max - v_log_min) if v_log_max > v_log_min else 0.5
        return round(3 + float(np.clip(frac, 0, 1)) * 11, 1)

    features = []
    # popup_rows: compact list [bid, area_m2, mat, energy, elec_or_null, co2]
    # Stored outside GeoJSON so geometry payload stays small; bound via JS after render.
    popup_rows: list[list] = []
    n_polygons = 0

    for _, row in df.iterrows():
        bid = str(row["building_id"])
        energy = float(row["energy_saved_kwh_yr"])
        co2 = float(row["co2_saved_kg_yr"])
        area = float(row["area_m2"])
        mat = str(row.get("roof_material", "unknown"))
        colour_val = float(values.loc[row.name]) if row.name in values.index else 0.0
        fill_color = _energy_to_hex(min(colour_val, vp95), vmin, vp95)
        idx = len(features)

        elec = row.get("electricity_saved_kwh_yr")
        elec_val = round(float(elec), 0) if elec is not None and str(elec) != "nan" and has_stage3 else None
        popup_rows.append([bid, round(area, 0), mat, round(energy, 0), elec_val, round(co2, 0)])

        if use_polygons:
            poly = polygons.get(bid) if polygons is not None else None
            if poly and len(poly) >= 3:
                coords = [[round(c[0], 5), round(c[1], 5)] for c in poly]
                geometry = {"type": "Polygon", "coordinates": [coords]}
                n_polygons += 1
            else:
                geometry = {"type": "Point",
                            "coordinates": [round(float(row["lon"]), 5),
                                            round(float(row["lat"]), 5)]}
        else:
            geometry = {"type": "Point",
                        "coordinates": [round(float(row["lon"]), 5),
                                        round(float(row["lat"]), 5)]}

        # Only store colour and index — no popup HTML — to keep GeoJSON payload small
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {"c": fill_color, "r": _radius(colour_val), "i": idx},
        })

    mode_label = f"{n_polygons} polygons" if use_polygons else f"{len(features)} circles"
    logger.info("Map: %s", mode_label)

    geojson_data = {"type": "FeatureCollection", "features": features}

    geo_layer = folium.GeoJson(
        geojson_data,
        style_function=lambda f: {
            "fillColor": f["properties"]["c"],
            "color": f["properties"]["c"],
            "weight": 0 if not use_polygons else 1,
            "fillOpacity": 0.75,
        },
        marker=folium.CircleMarker(radius=6),
    )
    geo_layer.add_to(fmap)

    # ── Inject compact popup data as JS array, bind on click after layer loads ──
    # This keeps popup content out of the GeoJSON entirely: ~3× smaller per entry
    # ([bid, area, mat, energy, elec, co2] vs full HTML string).
    import json as _json
    layer_var = geo_layer.get_name()
    elec_label = "Elec" if has_stage3 else ""
    popup_js = f"""
    <script>
    (function() {{
        var d = {_json.dumps(popup_rows)};
        var lyr = window["{layer_var}"];
        function bind(lyr) {{
            lyr.eachLayer(function(f) {{
                var i = f.feature && f.feature.properties && f.feature.properties.i;
                if (i === undefined) return;
                var r = d[i];
                var html = '<b>' + r[0] + '</b><br>' + r[1] + ' m² · ' + r[2] +
                    '<br>Energy: ' + r[3].toLocaleString() + ' kWh/yr' +
                    (r[4] !== null ? '<br>{elec_label}: ' + r[4].toLocaleString() + ' kWh/yr' : '') +
                    '<br>CO₂: ' + r[5].toLocaleString() + ' kg/yr';
                f.bindPopup(html, {{maxWidth: 280}});
            }});
        }}
        if (window["{layer_var}"]) {{ bind(window["{layer_var}"]); }}
        else {{ document.addEventListener("DOMContentLoaded", function() {{ bind(window["{layer_var}"]); }}); }}
    }})();
    </script>
    """
    fmap.get_root().html.add_child(folium.Element(popup_js))

    legend_html = _make_legend_html(vmin, vmax, colour_label)
    fmap.get_root().html.add_child(folium.Element(legend_html))

    stage_prefix = "stage3" if has_stage3 else "stage2"
    out_path = OUTPUT_DIR / f"{stage_prefix}_{suburb_key}_map.html"
    fmap.save(str(out_path))
    logger.info("Choropleth map saved to %s", out_path)
    return out_path


def _make_legend_html(vmin: float, vmax: float, label: str = "Energy saved (kWh/yr)") -> str:
    """Generate an HTML colour-scale legend for the folium map."""
    cmap = plt.get_cmap(CHOROPLETH_CMAP)
    stops = []
    for i in range(6):
        frac = i / 5.0
        r, g, b, _ = cmap(frac)
        hex_col = "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
        stops.append(f"{hex_col} {int(frac * 100)}%")
    gradient = ", ".join(stops)

    return f"""
    <div style="
        position: fixed;
        bottom: 30px; right: 30px;
        z-index: 9999;
        background: white;
        padding: 10px 14px;
        border-radius: 6px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.3);
        font-family: Arial, sans-serif;
        font-size: 12px;
        min-width: 160px;
    ">
        <b>{label}</b><br>
        <div style="
            height: 14px;
            width: 140px;
            background: linear-gradient(to right, {gradient});
            border-radius: 3px;
            margin: 5px 0;
        "></div>
        <div style="display: flex; justify-content: space-between; width: 140px;">
            <span>{vmin:,.0f}</span>
            <span>{(vmin + vmax) / 2:,.0f}</span>
            <span>{vmax:,.0f}</span>
        </div>
    </div>
    """


# ── Output 2: Summary charts ──────────────────────────────────────────────────


def build_summary_charts(df: pd.DataFrame, suburb_key: str, suburb_name: str) -> Path:
    """
    Build a 2×2 matplotlib summary figure and save as PNG.

    When Stage 3 columns are present (electricity_saved_kwh_yr), the primary
    metric switches from absorbed solar (Stage 2) to cooling electricity savings
    (Stage 3).

    Panels:
      1. Histogram — primary energy metric distribution
      2. Bar chart — mean primary metric by roof_material
      3. Bar chart — building count by roof_material
      4. Summary stats text box (shows both Stage 2 and Stage 3 when available)
    """
    has_stage3 = "electricity_saved_kwh_yr" in df.columns

    if has_stage3:
        primary_col = "electricity_saved_kwh_yr"
        primary_label = "Electricity saved (kWh/yr)"
        stage_label = "Stage 3 Thermal + Cool Roof Summary"
        co2_col = "co2_electricity_saved_kg_yr"
    else:
        primary_col = "energy_saved_kwh_yr"
        primary_label = "Energy saved (kWh/yr)"
        stage_label = "Stage 2 Cool Roof Summary"
        co2_col = "co2_saved_kg_yr"

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    fig.suptitle(
        f"{stage_label} — {suburb_name}",
        fontsize=14, fontweight="bold", y=0.98,
    )

    primary = df[primary_col].dropna()
    co2 = df[co2_col].dropna() if co2_col in df.columns else pd.Series(dtype=float)
    energy = df["energy_saved_kwh_yr"].dropna()

    # ── Panel 1: Histogram of primary metric ──────────────────────────────────
    ax1 = axes[0, 0]
    ax1.hist(primary, bins=40, color="#E05A2B", edgecolor="white", linewidth=0.4)
    ax1.set_title(f"Distribution of {primary_label.split('(')[0].strip()} per Building")
    ax1.set_xlabel(primary_label)
    ax1.set_ylabel("Number of buildings")
    ax1.yaxis.get_major_formatter().set_scientific(False)
    ax1.xaxis.get_major_formatter().set_scientific(False)
    median_val = float(primary.median())
    ax1.axvline(median_val, color="#333333", linestyle="--", linewidth=1.2,
                label=f"Median: {median_val:,.0f}")
    ax1.legend(fontsize=9)

    # ── Panel 2: Mean primary metric by roof material ─────────────────────────
    ax2 = axes[0, 1]
    mat_energy = (
        df.groupby("roof_material")[primary_col]
        .mean()
        .sort_values(ascending=False)
    )
    colors_bar = plt.get_cmap("tab10")(np.linspace(0, 0.7, len(mat_energy)))
    bars = ax2.bar(mat_energy.index, mat_energy.values, color=colors_bar)
    ax2.set_title(f"Mean {primary_label.split('(')[0].strip()} by Roof Material")
    ax2.set_xlabel("Roof material")
    ax2.set_ylabel(f"Mean {primary_label}")
    ax2.tick_params(axis="x", rotation=30)
    for bar in bars:
        h = bar.get_height()
        ax2.text(
            bar.get_x() + bar.get_width() / 2, h * 1.01,
            f"{h:,.0f}", ha="center", va="bottom", fontsize=8,
        )

    # ── Panel 3: Building count by roof material ──────────────────────────────
    ax3 = axes[1, 0]
    mat_count = df["roof_material"].value_counts()
    colors_count = plt.get_cmap("tab10")(np.linspace(0, 0.7, len(mat_count)))
    bars3 = ax3.bar(mat_count.index, mat_count.values, color=colors_count)
    ax3.set_title("Building Count by Roof Material")
    ax3.set_xlabel("Roof material")
    ax3.set_ylabel("Number of buildings")
    ax3.tick_params(axis="x", rotation=30)
    for bar in bars3:
        h = bar.get_height()
        ax3.text(
            bar.get_x() + bar.get_width() / 2, h + 0.5,
            f"{int(h):,}", ha="center", va="bottom", fontsize=8,
        )

    # ── Panel 4: Summary stats text box ──────────────────────────────────────
    ax4 = axes[1, 1]
    ax4.axis("off")

    total_primary = float(primary.sum())
    total_co2 = float(co2.sum()) if len(co2) else 0.0
    n_buildings = len(df)
    mean_primary = float(primary.mean())
    equiv_households = total_primary / HOUSEHOLDS_KWH_YR

    if has_stage3:
        total_absorbed = float(energy.sum())
        stats_lines = [
            f"Suburb:               {suburb_name}",
            f"Buildings analysed: {n_buildings:,}",
            "",
            f"Stage 2 (absorbed solar reduction)",
            f"  Total:  {total_absorbed:,.0f} kWh/yr",
            "",
            f"Stage 3 (cooling electricity saved)",
            f"  Total:  {total_primary:,.0f} kWh/yr",
            f"  CO₂:    {total_co2:,.0f} kg/yr",
            f"  Equiv:  {equiv_households:,.1f} households",
            f"  Mean:   {mean_primary:,.0f} kWh/yr/bldg",
            f"  Median: {median_val:,.0f} kWh/yr/bldg",
        ]
    else:
        stats_lines = [
            f"Suburb:               {suburb_name}",
            f"Buildings analysed: {n_buildings:,}",
            "",
            f"Total energy saved:  {total_primary:,.0f} kWh/yr",
            f"Total CO₂ saved:     {total_co2:,.0f} kg/yr",
            f"Equiv. households:   {equiv_households:,.1f}",
            f"  (@ {HOUSEHOLDS_KWH_YR:,} kWh/household/yr)",
            "",
            f"Mean per building:   {mean_primary:,.0f} kWh/yr",
            f"Median per building: {median_val:,.0f} kWh/yr",
        ]

    stats_text = "\n".join(stats_lines)
    ax4.text(
        0.05, 0.95, stats_text,
        transform=ax4.transAxes,
        fontsize=10,
        verticalalignment="top",
        fontfamily="monospace",
        bbox=dict(
            boxstyle="round,pad=0.6",
            facecolor="#f0f4f8",
            edgecolor="#aabbcc",
            linewidth=1.5,
        ),
    )
    ax4.set_title("Summary Statistics")

    fig.tight_layout(rect=[0, 0, 1, 0.96])

    stage_prefix = "stage3" if has_stage3 else "stage2"
    out_path = OUTPUT_DIR / f"{stage_prefix}_{suburb_key}_summary.png"
    fig.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Summary chart saved to %s", out_path)
    return out_path


# ── Output 3: HTML report ─────────────────────────────────────────────────────


def build_html_report(
    df: pd.DataFrame,
    suburb_key: str,
    suburb_name: str,
    chart_path: Path,
    map_path: Path,
) -> Path:
    """
    Build a one-page HTML report with key numbers, embedded chart, and map link.

    Shows Stage 3 electricity savings as primary metric when available; falls
    back to Stage 2 absorbed solar reduction.
    """
    has_stage3 = "electricity_saved_kwh_yr" in df.columns

    energy = df["energy_saved_kwh_yr"].dropna()
    total_absorbed = float(energy.sum())
    n_buildings = len(df)
    run_date = datetime.now().strftime("%d %B %Y")

    if has_stage3:
        elec = df["electricity_saved_kwh_yr"].dropna()
        co2 = df["co2_electricity_saved_kg_yr"].dropna()
        total_energy = float(elec.sum())
        total_co2 = float(co2.sum())
    else:
        total_energy = total_absorbed
        total_co2 = float(df["co2_saved_kg_yr"].dropna().sum())

    equiv_households = total_energy / HOUSEHOLDS_KWH_YR

    # Use relative paths for portability when both files are in OUTPUT_DIR
    chart_rel = chart_path.name
    map_rel = map_path.name

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Raising Rooves — {suburb_name} Cool Roof Report</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f7f9fc;
      color: #1a2332;
    }}
    header {{
      background: linear-gradient(135deg, #1a3a5c 0%, #2d6a9f 100%);
      color: white;
      padding: 32px 40px 24px;
    }}
    header h1 {{ font-size: 1.9rem; font-weight: 700; letter-spacing: -0.5px; }}
    header p  {{ margin-top: 6px; opacity: 0.85; font-size: 0.95rem; }}
    .container {{ max-width: 960px; margin: 0 auto; padding: 36px 24px; }}
    .kpi-row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 32px;
    }}
    .kpi {{
      background: white;
      border-radius: 10px;
      padding: 20px 22px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.07);
      border-left: 4px solid #2d6a9f;
    }}
    .kpi .value {{
      font-size: 1.7rem;
      font-weight: 700;
      color: #2d6a9f;
      line-height: 1.1;
    }}
    .kpi .label {{
      font-size: 0.8rem;
      color: #6b7a8d;
      margin-top: 4px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .section-title {{
      font-size: 1.1rem;
      font-weight: 600;
      color: #1a3a5c;
      margin: 28px 0 12px;
      padding-bottom: 6px;
      border-bottom: 2px solid #e2eaf3;
    }}
    .chart-wrap {{
      background: white;
      border-radius: 10px;
      padding: 16px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.07);
      text-align: center;
    }}
    .chart-wrap img {{
      max-width: 100%;
      height: auto;
      border-radius: 6px;
    }}
    .map-link-box {{
      background: white;
      border-radius: 10px;
      padding: 20px 24px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.07);
      display: flex;
      align-items: center;
      gap: 16px;
    }}
    .map-link-box a {{
      display: inline-block;
      background: #2d6a9f;
      color: white;
      text-decoration: none;
      padding: 10px 22px;
      border-radius: 6px;
      font-weight: 600;
      font-size: 0.95rem;
      transition: background 0.2s;
    }}
    .map-link-box a:hover {{ background: #1a3a5c; }}
    .map-link-box p {{ color: #6b7a8d; font-size: 0.88rem; margin: 0; }}
    .note {{
      font-size: 0.78rem;
      color: #8a95a3;
      margin-top: 28px;
      border-top: 1px solid #e2eaf3;
      padding-top: 14px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Raising Rooves — {suburb_name} Cool Roof Report</h1>
    <p>{"Stage 3 thermal + cool roof analysis" if has_stage3 else "Stage 2 cool roof delta analysis"} &nbsp;|&nbsp; {n_buildings:,} buildings &nbsp;|&nbsp; Generated {run_date}</p>
  </header>
  <div class="container">

    <div class="kpi-row">
      <div class="kpi">
        <div class="value">{n_buildings:,}</div>
        <div class="label">Buildings analysed</div>
      </div>
      <div class="kpi">
        <div class="value">{total_energy / 1e6:,.2f} GWh/yr</div>
        <div class="label">{"Electricity saved (cooling)" if has_stage3 else "Absorbed solar reduced"}</div>
      </div>
      <div class="kpi">
        <div class="value">{total_co2 / 1000:,.1f} t CO₂/yr</div>
        <div class="label">CO₂ avoided</div>
      </div>
      <div class="kpi">
        <div class="value">{equiv_households:,.0f}</div>
        <div class="label">Equivalent households powered</div>
      </div>
      {"<div class='kpi'><div class='value'>" + f"{total_absorbed / 1e6:,.2f} GWh/yr" + "</div><div class='label'>Absorbed solar reduction (Stage 2)</div></div>" if has_stage3 else ""}
    </div>

    <div class="section-title">Summary Charts</div>
    <div class="chart-wrap">
      <img src="{chart_rel}" alt="Summary charts for {suburb_name}" />
    </div>

    <div class="section-title">Interactive Building Map</div>
    <div class="map-link-box">
      <div>
        <a href="{map_rel}" target="_blank">Open Interactive Map</a>
        <p style="margin-top:8px;">Buildings coloured by {"electricity saved" if has_stage3 else "energy saved"} (kWh/yr). Hover for per-building details.</p>
      </div>
    </div>

    <p class="note">
      <strong>Methodology note:</strong>
      Stage 2: absorbed solar reduction = annual GHI × roof surface area × (absorptance_before − 0.20),
      where absorptance is estimated from roof HSV pixel classification (AS/NZS 4859.1).
      {"Stage 3: electricity saved = absorbed reduction × heat transfer fraction (0.65) × cooling fraction (0.70) / HVAC COP (3.0 residential / 4.0 commercial). " if has_stage3 else ""}
      CO₂ factor: Victorian grid intensity 0.79 kg CO₂-e/kWh (AEMO 2023).
      Equivalent households: {HOUSEHOLDS_KWH_YR:,} kWh/yr (AER 2023, Victoria).
    </p>

  </div>
</body>
</html>
"""

    stage_prefix = "stage3" if has_stage3 else "stage2"
    out_path = OUTPUT_DIR / f"{stage_prefix}_{suburb_key}_report.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("HTML report saved to %s", out_path)
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visualise Stage 2/3 cool roof results for a suburb.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--suburb",
        required=True,
        help="Suburb name (e.g. Carlton, Richmond).",
    )
    parser.add_argument(
        "--stage2-file",
        type=Path,
        default=None,
        help="Path to Stage 2 output file (.parquet or .csv). "
             "Defaults to data/output/stage2_{suburb}.parquet.",
    )
    parser.add_argument(
        "--stage2-only",
        action="store_true",
        help="Use Stage 2 data even if Stage 3 output exists.",
    )
    parser.add_argument(
        "--polygons",
        action="store_true",
        help="Render full building polygon outlines instead of circles. "
             "Only recommended for suburbs with <1000 buildings — large suburbs "
             "will use significant browser memory.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.debug:
        for handler in logger.handlers:
            handler.setLevel("DEBUG")
        logger.setLevel("DEBUG")
        logger.debug("Debug logging enabled.")

    suburb_name = args.suburb.strip().title()
    from config.suburbs import get_suburb
    suburb_key = get_suburb(suburb_name).key

    logger.info("=== Visualise Results: %s ===", suburb_name)

    # Load Stage 3 when available (contains all Stage 2 columns + thermal columns).
    # Fall back to Stage 2 when Stage 3 hasn't been run yet or --stage2-only is set.
    df = None
    if not args.stage2_only:
        df = load_stage3(suburb_key)
        if df is not None:
            logger.info("Using Stage 3 data (electricity savings included).")
    if df is None:
        df = load_stage2(suburb_key, args.stage2_file)
        logger.info("Using Stage 2 data (absorbed solar reduction only).")

    polygons = load_polygons(suburb_key)

    # Report any optional columns that are missing but not required
    optional_cols = {"roof_colour", "roof_shape", "pitch_deg", "annual_ghi_kwh_m2"}
    missing_optional = optional_cols - set(df.columns)
    if missing_optional:
        logger.info(
            "Optional columns not present in Stage 2 output (not required): %s",
            sorted(missing_optional),
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build outputs
    map_path = build_choropleth_map(df, polygons, suburb_key, suburb_name,
                                    use_polygons=args.polygons)
    chart_path = build_summary_charts(df, suburb_key, suburb_name)
    report_path = build_html_report(df, suburb_key, suburb_name, chart_path, map_path)

    logger.info("=== Done ===")
    logger.info("  Map:    %s", map_path)
    logger.info("  Charts: %s", chart_path)
    logger.info("  Report: %s", report_path)

    print(f"\nOutputs written to {OUTPUT_DIR}:")
    print(f"  {map_path.name}")
    print(f"  {chart_path.name}")
    print(f"  {report_path.name}")


if __name__ == "__main__":
    main()
