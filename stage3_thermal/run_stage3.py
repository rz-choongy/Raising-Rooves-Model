"""
CLI entry point for Stage 3: Transient Roof Heat-Ingress.

Runs a transient 1-D finite-volume conduction model through a layered roof for
every building in a suburb, at the building's current solar absorptance and at
the cool-roof target, and reports the cooling-season electricity saving and the
winter heating penalty.

Usage:
    python -m stage3_thermal.run_stage3 --suburb Carlton
    python -m stage3_thermal.run_stage3 --suburb Carlton --year 2007 --debug
    python -m stage3_thermal.run_stage3 --suburb Carlton --weather-csv path/to/hourly.csv
    python -m stage3_thermal.run_stage3 --suburb Carlton --insulation-r-upgrade 4.1
    python -m stage3_thermal.run_stage3 --list-suburbs

Prerequisites:
    Stage 2 output must exist for the suburb:
        data/output/stage2_{suburb}.parquet

    Hourly weather is auto-resolved: --weather-csv → cached
    data/raw/barra/heat_ingress_{suburb}_{year}.csv → BARRA2 OPeNDAP fetch
    (needs xarray + pydap + network) → committed data/samples fallback.

Output files:
    data/output/stage3_{suburb}.parquet
    data/output/stage3_{suburb}.csv
"""

import argparse
import sys
from pathlib import Path

from config.settings import (
    HEAT_INGRESS_REFERENCE_YEAR,
    INSULATION_UPGRADE_R_M2K_W,
    INSULATION_UPGRADE_THICKNESS_M,
)
from config.suburbs import list_suburbs
from shared.logging_config import setup_logging
from stage3_thermal.pipeline import run_stage3


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Raising Rooves — Stage 3: Transient Roof Heat-Ingress"
    )
    parser.add_argument(
        "--suburb",
        type=str,
        help="Name of the Melbourne suburb to process (e.g. 'Carlton')",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=HEAT_INGRESS_REFERENCE_YEAR,
        help=f"BARRA2 reference year for the hourly weather (default {HEAT_INGRESS_REFERENCE_YEAR})",
    )
    parser.add_argument(
        "--weather-csv",
        type=str,
        default=None,
        help="Explicit hourly BARRA2 weather CSV (overrides auto-resolution)",
    )
    parser.add_argument(
        "--insulation-r-upgrade",
        type=float,
        default=None,
        metavar="R_M2K_W",
        help=(
            "Opt-in: also march an insulation-upgrade scenario at this "
            f"R-value (m2K/W; e.g. {INSULATION_UPGRADE_R_M2K_W} matches the "
            "reference notebook's own Insulation_new row), same absorptance/"
            "emissivity as the current roof. Adds insulation_* columns; "
            "omit to leave Stage 3's output unchanged."
        ),
    )
    parser.add_argument(
        "--insulation-thickness-upgrade",
        type=float,
        default=None,
        metavar="THICKNESS_M",
        help=(
            "Paired thickness (m) for --insulation-r-upgrade (default: keep "
            f"the stack's own thickness; {INSULATION_UPGRADE_THICKNESS_M} "
            "matches the reference notebook's Insulation_new row)."
        ),
    )
    parser.add_argument(
        "--coolmax",
        action="store_true",
        help=(
            "Opt-in re-roof scenario: also march every building as a new steel "
            "roof in standard Colorbond (current colour) vs Colorbond Coolmax "
            "(SR 0.77). Adds *_coolmax columns."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug-level logging",
    )
    parser.add_argument(
        "--list-suburbs",
        action="store_true",
        help="List available suburbs and exit",
    )

    args = parser.parse_args()

    if args.list_suburbs:
        print("Available suburbs:")
        for name in list_suburbs():
            print(f"  - {name}")
        sys.exit(0)

    if not args.suburb:
        parser.error("--suburb is required (or use --list-suburbs)")

    level = "DEBUG" if args.debug else "INFO"
    logger = setup_logging("stage3_cli", level=level)
    logger.info("Starting Stage 3 for suburb: %s", args.suburb)

    try:
        df = run_stage3(
            suburb_name=args.suburb,
            weather_csv=Path(args.weather_csv) if args.weather_csv else None,
            year=args.year,
            insulation_r_upgrade_m2k_w=args.insulation_r_upgrade,
            insulation_thickness_upgrade_m=args.insulation_thickness_upgrade,
            coolmax=args.coolmax,
        )
        if df.empty:
            logger.warning("No results produced. Check logs for details.")
            sys.exit(1)

        total_elec = df["electricity_saved_kwh_yr"].sum()
        total_net = df["net_electricity_saved_kwh_yr"].sum()
        total_co2 = df["co2_electricity_saved_kg_yr"].sum()

        logger.info(
            "Done. %d buildings | %.0f kWh/yr cooling electricity saved | "
            "%.0f kWh/yr net of heating penalty | %.0f kg CO2/yr avoided.",
            len(df), total_elec, total_net, total_co2,
        )
    except Exception as e:
        logger.error("Pipeline failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
