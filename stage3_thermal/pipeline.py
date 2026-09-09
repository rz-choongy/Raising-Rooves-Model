"""
Stage 3 pipeline orchestrator for the Raising Rooves pipeline.

Reads Stage 2 parquet output for a suburb, applies the thermal benefit
calculator per building, and writes:
  - data/output/stage3_{suburb_key}.parquet
  - data/output/stage3_{suburb_key}.csv

Added columns (on top of all Stage 2 columns):
  roof_r_value_m2k              — R_roof inferred from building attributes
  h_out_w_m2k                   — outdoor surface coefficient (wind-derived from
                                   Stage 2's mean_wind_speed_ms when available,
                                   else the fixed H_OUTSIDE_W_M2K fallback)
  heat_transfer_fraction        — effective roof→interior fraction (incl. multistorey)
  heat_to_interior_kwh_yr       — roof heat that reaches the interior
  cooling_load_reduction_kwh_yr — subset that drives the cooling system
  electricity_saved_kwh_yr      — electricity saved by reduced AC demand
  co2_electricity_saved_kg_yr   — CO2 avoided from electricity saving

Note: electricity_saved_kwh_yr is the thermal electricity saving from the cool
roof intervention, distinct from the absorbed-solar saving (energy_saved_kwh_yr)
in Stage 2 which is always larger.
"""

from pathlib import Path

import pandas as pd
from tqdm import tqdm

from config.settings import OUTPUT_DIR
from config.suburbs import get_suburb
from shared.file_io import ensure_dir, load_stage_input, save_parquet, save_stage_outputs
from shared.logging_config import setup_logging
from stage3_thermal.thermal_calculator import calculate_thermal_benefit

logger = setup_logging("stage3_pipeline")

# Average Australian household electricity consumption (kWh/yr) used to express
# suburb totals in relatable household-equivalent units.
# Source: AER State of the Energy Market 2023 — Victorian residential average.
_HOUSEHOLD_KWH_YR = 4_200.0


def run_stage3(suburb_name: str) -> pd.DataFrame:
    """
    Run the full Stage 3 thermal pipeline for a suburb.

    Reads Stage 2 output, applies thermal benefit calculations, writes Stage 3
    outputs (parquet + CSV), and logs a suburb-level summary.

    Args:
        suburb_name: Suburb to process (must have a Stage 2 output parquet).

    Returns:
        DataFrame with all Stage 2 columns plus the four Stage 3 thermal
        columns. Returns an empty DataFrame if Stage 2 output is missing.
    """
    suburb = get_suburb(suburb_name)
    suburb_key = suburb.key

    logger.info("=" * 60)
    logger.info("Stage 3 Thermal Pipeline: %s", suburb.name)
    logger.info("=" * 60)

    # ── Step 1: Load Stage 2 output ───────────────────────────────────────────
    df = load_stage_input(2, suburb_key)
    if df is None:
        return pd.DataFrame()
    logger.info("Step 1/2: Loaded %d buildings from Stage 2.", len(df))

    # ── Step 2: Apply thermal benefit per building ────────────────────────────
    logger.info("Step 2/2: Computing thermal benefit per building...")
    thermal_rows = []
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Thermal calc"):
        wind = row.get("mean_wind_speed_ms")
        result = calculate_thermal_benefit(
            energy_saved_kwh_yr=float(row.get("energy_saved_kwh_yr", 0.0)),
            roof_material=row.get("roof_material"),
            building_type=row.get("building_type"),
            levels=row.get("levels"),
            wind_speed_ms=float(wind) if wind is not None and str(wind) != "nan" else None,
        )
        thermal_rows.append(result)

    thermal_df = pd.DataFrame(thermal_rows)
    df = pd.concat([df.reset_index(drop=True), thermal_df], axis=1)

    # ── Summary ───────────────────────────────────────────────────────────────
    total_elec_saved = df["electricity_saved_kwh_yr"].sum()
    total_co2_elec_saved = df["co2_electricity_saved_kg_yr"].sum()
    total_absorbed_saved = df["energy_saved_kwh_yr"].sum()
    equiv_households = total_elec_saved / _HOUSEHOLD_KWH_YR

    thermal_ratio = (
        (total_elec_saved / total_absorbed_saved * 100)
        if total_absorbed_saved > 0
        else 0.0
    )

    logger.info(
        "Suburb %s: %d buildings processed",
        suburb.name, len(df),
    )
    logger.info(
        "Stage 2 absorbed solar saving : %.0f kWh/yr",
        total_absorbed_saved,
    )
    logger.info(
        "Stage 3 electricity saving    : %.0f kWh/yr  (%.1f%% of absorbed solar)",
        total_elec_saved, thermal_ratio,
    )
    logger.info(
        "CO2 avoided (electricity)     : %.0f kg/yr",
        total_co2_elec_saved,
    )
    logger.info(
        "Equivalent households powered : %.1f households/yr",
        equiv_households,
    )
    n_wind = df["mean_wind_speed_ms"].notna().sum() if "mean_wind_speed_ms" in df.columns else 0
    logger.info(
        "Outdoor surface coefficient   : mean h_out %.2f W/m²K (wind-derived for "
        "%d/%d buildings, fixed fallback for the rest)",
        df["h_out_w_m2k"].mean(), n_wind, len(df),
    )

    # ── Save outputs ──────────────────────────────────────────────────────────
    save_stage_outputs(df, 3, suburb_key)

    logger.info("=" * 60)
    logger.info("Stage 3 complete for %s.", suburb.name)
    logger.info("=" * 60)

    return df
