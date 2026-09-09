"""
Fetch BARRA2 climate data (temperature, irradiance, wind) for every configured
suburb and summarise annual averages in one table.

Does not require Stage 1 building footprints -- climate data is keyed off each
suburb's centroid (config/suburbs.py), so this can run standalone ahead of, or
independent of, Stage 1/Overpass.

Writes stage2_{suburb}_climate.parquet per suburb (same as Stage 2's normal
climate step) plus one combined summary:
    data/output/climate_summary_all_suburbs.csv

Usage:
    python -m tools.fetch_climate_all_suburbs
    python -m tools.fetch_climate_all_suburbs --start-year 2010 --end-year 2020
    python -m tools.fetch_climate_all_suburbs --debug
"""

from __future__ import annotations

import argparse

import pandas as pd

from config.settings import OUTPUT_DIR
from config.suburbs import SUBURBS
from shared.file_io import ensure_dir
from shared.logging_config import setup_logging
from stage2_irradiance.pipeline import run_stage2_climate

logger = setup_logging("fetch_climate_all_suburbs")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch BARRA2 annual temperature/irradiance/wind for all configured suburbs."
    )
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year", type=int, default=2020)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    rows = []
    for suburb_key, suburb in SUBURBS.items():
        logger.info("── %s ──", suburb.name)
        try:
            climate_df, annual_ghi, annual_wind = run_stage2_climate(
                suburb.name, start_year=args.start_year, end_year=args.end_year
            )
        except Exception as exc:
            logger.error("Failed for %s: %s", suburb.name, exc, exc_info=args.debug)
            rows.append(
                {
                    "suburb": suburb.name,
                    "annual_mean_temp_c": None,
                    "annual_ghi_kwh_m2_yr": None,
                    "annual_mean_wind_speed_ms": None,
                    "status": f"error: {exc}",
                }
            )
            continue

        if climate_df.empty:
            logger.warning("No climate data returned for %s.", suburb.name)
            rows.append(
                {
                    "suburb": suburb.name,
                    "annual_mean_temp_c": None,
                    "annual_ghi_kwh_m2_yr": None,
                    "annual_mean_wind_speed_ms": None,
                    "status": "no data (BARRA2 unreachable)",
                }
            )
            continue

        annual_temp = (
            round(climate_df["mean_temp_c"].mean(), 1)
            if "mean_temp_c" in climate_df.columns
            else None
        )

        rows.append(
            {
                "suburb": suburb.name,
                "annual_mean_temp_c": annual_temp,
                "annual_ghi_kwh_m2_yr": round(annual_ghi, 1) if annual_ghi is not None else None,
                "annual_mean_wind_speed_ms": round(annual_wind, 2) if annual_wind is not None else None,
                "status": "ok",
            }
        )

    summary = pd.DataFrame(rows)
    out_path = ensure_dir(OUTPUT_DIR) / "climate_summary_all_suburbs.csv"
    summary.to_csv(out_path, index=False)
    logger.info("Saved combined summary to %s", out_path)

    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
