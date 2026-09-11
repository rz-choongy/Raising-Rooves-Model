"""
Stage 3 pipeline orchestrator for the Raising Rooves pipeline.

Reads Stage 2 parquet output for a suburb, runs the transient roof heat-ingress
model (``stage3_thermal/heat_ingress_model.py``) for every building, and writes:
  - data/output/stage3_{suburb_key}.parquet
  - data/output/stage3_{suburb_key}.csv

Added columns (on top of all Stage 2 columns):
  roof_heat_ingress_base_kwh_m2_yr    — annual interior heat / m² roof, current absorptance
  roof_heat_ingress_cool_kwh_m2_yr    — same, at COOL_ROOF_ABSORPTANCE
  cooling_season_heat_avoided_kwh_yr  — interior heat kept out during warm hours
  heating_season_heat_added_kwh_yr    — wanted winter solar gain the cool roof rejects
  cooling_fraction_applied / hvac_cop — audit
  electricity_saved_kwh_yr            — cooling-season electricity saved (after COP)
  heating_penalty_electricity_kwh_yr  — extra winter heating electricity
  net_electricity_saved_kwh_yr        — electricity_saved − heating_penalty_electricity
  co2_electricity_saved_kg_yr         — CO2 avoided from the cooling saving
  net_co2_electricity_saved_kg_yr     — CO2 avoided net of the heating penalty

The per-building saving is the difference between marching the model at the
building's current solar absorptance and at COOL_ROOF_ABSORPTANCE. Weather is a
suburb-uniform hourly BARRA2 extract (~11 km grid); see _resolve_weather for the
source priority.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from config.settings import (
    BARRA_DIR,
    DATA_DIR,
    HEAT_INGRESS_REFERENCE_YEAR,
    OUTPUT_DIR,
)
from config.suburbs import Suburb, get_suburb
from shared.file_io import ensure_dir, load_stage_input, save_stage_outputs
from shared.logging_config import setup_logging
from stage3_thermal.heat_ingress_model import run_model

logger = setup_logging("stage3_pipeline")

# Average Victorian household electricity consumption (kWh/yr) — used only to
# express suburb totals in household-equivalent units.
# Source: AER State of the Energy Market 2023 — Victorian residential average.
_HOUSEHOLD_KWH_YR = 4_200.0

_REQUIRED_WEATHER_COLUMNS = {
    "time_UTC",
    "rsdsdir_Wm2",
    "rsdsdif_Wm2",
    "temp_C",
    "wind_ms",
}


def _read_weather_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    missing = _REQUIRED_WEATHER_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Weather file {path} is missing columns: {sorted(missing)}")
    return df


def _resolve_weather(
    suburb: Suburb,
    year: int,
    weather_csv: Path | None,
) -> tuple[pd.DataFrame, str]:
    """
    Resolve the suburb's hourly weather table.

    Priority:
      1. --weather-csv PATH (explicit override)
      2. data/raw/barra/heat_ingress_{key}_{year}.csv  (cache from a previous run)
      3. BARRA2 OPeNDAP fetch via tools.fetch_heat_ingress_weather (cached to #2)
      4. data/samples/heat_ingress_{key}_{year}.csv     (committed offline sample)
    """
    if weather_csv is not None:
        return _read_weather_csv(Path(weather_csv)), f"--weather-csv {weather_csv}"

    cache = BARRA_DIR / f"heat_ingress_{suburb.key}_{year}.csv"
    if cache.exists():
        return _read_weather_csv(cache), str(cache)

    lat, lon = suburb.centroid
    try:
        from tools.fetch_heat_ingress_weather import build_hourly_weather_table

        logger.info(
            "Fetching hourly BARRA2 weather for %s centroid (lat=%.4f, lon=%.4f, %d)...",
            suburb.name, lat, lon, year,
        )
        table = build_hourly_weather_table(lat, lon, year)
        ensure_dir(cache.parent)
        table.to_csv(cache, index=False)
        return table, f"BARRA2 OPeNDAP → {cache}"
    except Exception as exc:  # noqa: BLE001 - fall back to the offline sample
        logger.warning(
            "BARRA2 hourly fetch failed (%s). Falling back to the offline sample.", exc
        )

    sample = DATA_DIR / "samples" / f"heat_ingress_{suburb.key}_{year}.csv"
    if sample.exists():
        return _read_weather_csv(sample), str(sample)

    raise FileNotFoundError(
        f"No hourly weather for {suburb.name}. Provide --weather-csv, or run "
        f"`python -m tools.fetch_heat_ingress_weather --lat {lat} --lon {lon} "
        f"--year {year} --out {cache}` (needs xarray + pydap + network)."
    )


def run_stage3(
    suburb_name: str,
    weather_csv: Path | None = None,
    year: int = HEAT_INGRESS_REFERENCE_YEAR,
) -> pd.DataFrame:
    """
    Run the full Stage 3 heat-ingress pipeline for a suburb.

    Reads Stage 2 output, runs the transient roof model per building, writes
    Stage 3 outputs (parquet + CSV), and logs a per-building / per-m² summary.

    Args:
        suburb_name: Suburb to process (must have a Stage 2 output parquet).
        weather_csv: Optional explicit hourly BARRA2 CSV (else auto-resolved).
        year: BARRA2 reference year for the weather.

    Returns:
        DataFrame with all Stage 2 columns plus the Stage 3 thermal columns.
        Empty DataFrame if Stage 2 output is missing.
    """
    suburb = get_suburb(suburb_name)
    suburb_key = suburb.key

    logger.info("=" * 60)
    logger.info("Stage 3 Heat-Ingress Pipeline: %s", suburb.name)
    logger.info("=" * 60)

    # ── Step 1: Load Stage 2 output ───────────────────────────────────────────
    df = load_stage_input(2, suburb_key)
    if df is None:
        return pd.DataFrame()
    logger.info("Step 1/3: Loaded %d buildings from Stage 2.", len(df))

    # ── Step 2: Resolve hourly weather (suburb-uniform) ───────────────────────
    weather_df, weather_source = _resolve_weather(suburb, year, weather_csv)
    logger.info(
        "Step 2/3: Weather source: %s (%d hourly rows).", weather_source, len(weather_df)
    )

    # ── Step 3: Transient model per building ──────────────────────────────────
    logger.info("Step 3/3: Marching the transient roof model per building...")
    thermal_df = run_model(df, weather_df)
    df = pd.concat([df.reset_index(drop=True), thermal_df.reset_index(drop=True)], axis=1)

    # ── Summary (per-building / per-m² first, totals second) ──────────────────
    roof_area = df["roof_surface_area_m2"].clip(lower=0)
    per_building_elec = df["electricity_saved_kwh_yr"].mean()
    per_building_net = df["net_electricity_saved_kwh_yr"].mean()
    valid_area = roof_area > 0
    per_m2_elec = (
        (df.loc[valid_area, "electricity_saved_kwh_yr"] / roof_area[valid_area]).mean()
        if valid_area.any()
        else 0.0
    )

    total_elec = df["electricity_saved_kwh_yr"].sum()
    total_penalty = df["heating_penalty_electricity_kwh_yr"].sum()
    total_net = df["net_electricity_saved_kwh_yr"].sum()
    total_co2 = df["co2_electricity_saved_kg_yr"].sum()
    total_net_co2 = df["net_co2_electricity_saved_kg_yr"].sum()

    logger.info("Suburb %s: %d buildings processed.", suburb.name, len(df))
    logger.info(
        "Per building : %.0f kWh/yr cooling electricity saved | %.0f kWh/yr net "
        "(after heating penalty).",
        per_building_elec, per_building_net,
    )
    logger.info("Per m² roof  : %.2f kWh/m²/yr cooling electricity saved.", per_m2_elec)
    logger.info(
        "Suburb totals: %.0f kWh/yr cooling saved | %.0f kWh/yr heating penalty | "
        "%.0f kWh/yr net.",
        total_elec, total_penalty, total_net,
    )
    logger.info(
        "CO2          : %.0f kg/yr avoided (cooling) | %.0f kg/yr net | "
        "~%.1f households/yr (net).",
        total_co2, total_net_co2, total_net / _HOUSEHOLD_KWH_YR,
    )

    # ── Save outputs ──────────────────────────────────────────────────────────
    save_stage_outputs(df, 3, suburb_key)

    logger.info("=" * 60)
    logger.info("Stage 3 complete for %s.", suburb.name)
    logger.info("=" * 60)

    return df
