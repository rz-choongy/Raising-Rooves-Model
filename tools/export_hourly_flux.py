"""
Export hourly roof heat flux for selected buildings under all three roof options.

Stage 3 only writes annual totals. This tool re-marches the same transient
model for a handful of buildings and keeps every hour, side by side:

    flux_existing_wh_m2  — the roof as it is now (uncoated)
    flux_coated_wh_m2    — same roof with its roof-type cool coating
    flux_coolmax_wh_m2   — roof replaced with Colorbond Coolmax steel

plus roof_surface_area_m2 and heat_*_kwh (flux × area) so per-building kWh
needs no join. Flux is Wh per m² of roof per hour (= average W/m² that hour);
positive = heat into the room, negative = heat out of it.

Usage:
    python -m tools.export_hourly_flux --suburb Carlton --building-id 22818860
    python -m tools.export_hourly_flux --suburb Carlton --sample 5 --max-area 300
    python -m tools.export_hourly_flux --suburb Carlton --sample 5 --year 2007 --debug

Inputs:
    data/output/stage2_{suburb}.parquet (same input as Stage 3)
    hourly weather, resolved exactly as Stage 3 does (--weather-csv / cache /
    BARRA2 fetch / committed sample)

Output:
    data/output/hourly_flux_{suburb}.csv (override with --out; .parquet also works)
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

from config.settings import HEAT_INGRESS_REFERENCE_YEAR, OUTPUT_DIR
from config.suburbs import get_suburb
from shared.file_io import ensure_dir, load_stage_input
from shared.logging_config import setup_logging
from stage3_thermal.heat_ingress_model import hourly_scenario_flux
from stage3_thermal.pipeline import _resolve_weather

logger = setup_logging("export_hourly_flux")

# Every row is one building-hour, so a whole suburb is ~50M rows. Cap the
# selection; pass --max-buildings to raise it deliberately.
_DEFAULT_MAX_BUILDINGS = 50


def select_buildings(
    df: pd.DataFrame,
    building_ids: list[str] | None,
    sample: int | None,
    min_area_m2: float | None,
    max_area_m2: float | None,
    seed: int,
) -> pd.DataFrame:
    """Pick the buildings to export: explicit ids, else a seeded random sample within the area bounds."""
    if building_ids:
        wanted = {str(b) for b in building_ids}
        picked = df[df["building_id"].astype(str).isin(wanted)]
        missing = wanted - set(picked["building_id"].astype(str))
        if missing:
            raise ValueError(f"building_id not in Stage 2 output: {sorted(missing)}")
        return picked

    area = pd.to_numeric(df["roof_surface_area_m2"], errors="coerce")
    keep = area > 0
    if min_area_m2 is not None:
        keep &= area >= min_area_m2
    if max_area_m2 is not None:
        keep &= area <= max_area_m2
    pool = df[keep]
    if pool.empty:
        raise ValueError("No buildings match the area filter.")
    return pool.sample(n=min(int(sample), len(pool)), random_state=seed)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hourly roof heat flux: existing vs coated vs Coolmax, per building"
    )
    parser.add_argument("--suburb", required=True, help="Suburb with a Stage 2 parquet")
    pick = parser.add_mutually_exclusive_group(required=True)
    pick.add_argument("--building-id", nargs="+", help="One or more building_id values")
    pick.add_argument("--sample", type=int, help="Random sample of N buildings")
    parser.add_argument("--min-area", type=float, default=None,
                        help="With --sample: minimum roof_surface_area_m2")
    parser.add_argument("--max-area", type=float, default=None,
                        help="With --sample: maximum roof_surface_area_m2 (e.g. 300 for houses)")
    parser.add_argument("--seed", type=int, default=0, help="Random seed for --sample")
    parser.add_argument("--max-buildings", type=int, default=_DEFAULT_MAX_BUILDINGS,
                        help=f"Safety cap on buildings exported (default {_DEFAULT_MAX_BUILDINGS})")
    parser.add_argument("--year", type=int, default=HEAT_INGRESS_REFERENCE_YEAR,
                        help=f"BARRA2 weather year (default {HEAT_INGRESS_REFERENCE_YEAR})")
    parser.add_argument("--weather-csv", type=str, default=None,
                        help="Explicit hourly BARRA2 weather CSV")
    parser.add_argument("--out", type=str, default=None,
                        help="Output path (.csv or .parquet)")
    parser.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    args = parser.parse_args()

    if args.debug:
        setup_logging("export_hourly_flux", level="DEBUG")

    suburb = get_suburb(args.suburb)
    df = load_stage_input(2, suburb.key)
    if df is None:
        sys.exit(1)

    try:
        chosen = select_buildings(
            df, args.building_id, args.sample, args.min_area, args.max_area, args.seed
        )
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    if len(chosen) > args.max_buildings:
        logger.error(
            "%d buildings selected; cap is %d (each is ~8,760 rows). Raise --max-buildings "
            "if you really want this.", len(chosen), args.max_buildings,
        )
        sys.exit(1)

    weather_df, source = _resolve_weather(
        suburb, args.year, Path(args.weather_csv) if args.weather_csv else None
    )
    logger.info("Weather: %s. Marching %d buildings × 3 roof options...", source, len(chosen))

    hourly = hourly_scenario_flux(chosen, weather_df)
    flux_cols = [c for c in hourly.columns if c.startswith("flux_")]
    heat_cols = [c for c in hourly.columns if c.startswith("heat_")]
    hourly[flux_cols] = hourly[flux_cols].round(3)
    hourly[heat_cols] = hourly[heat_cols].round(4)

    out = Path(args.out) if args.out else OUTPUT_DIR / f"hourly_flux_{suburb.key}.csv"
    ensure_dir(out.parent)
    if out.suffix == ".parquet":
        hourly.to_parquet(out, index=False)
    else:
        hourly.to_csv(out, index=False)

    counted = hourly[~hourly["spin_up"]]
    summary = counted.groupby("building_id").agg(
        roof_construction=("roof_construction", "first"),
        roof_surface_area_m2=("roof_surface_area_m2", "first"),
        existing_kwh_yr=("heat_existing_kwh", "sum"),
        coated_kwh_yr=("heat_coated_kwh", "sum"),
        coolmax_kwh_yr=("heat_coolmax_kwh", "sum"),
    ).round(1)
    logger.info("Annual ceiling heat (thermal kWh, after spin-up):\n%s", summary.to_string())
    logger.info("Wrote %d rows to %s", len(hourly), out)


if __name__ == "__main__":
    main()
