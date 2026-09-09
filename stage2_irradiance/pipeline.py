"""
Stage 2 pipeline orchestrator for the Raising Rooves pipeline.

Three responsibilities:
  1. Climate data retrieval: fetch monthly irradiance + temperature stats
     from BARRA2. Saved as stage2_{suburb}_climate.parquet.

  2. Building orientation: fit a minimum-rotated-rectangle to each Stage 1
     footprint (building_orientation.py) to estimate the house's heading
     (azimuth_deg) and the wall length ratio between its main heading and
     the perpendicular walls (wall_ratio).

  3. Cool roof delta: join Stage 1 buildings with annual GHI, compute per-building
     energy saving and CO2 reduction. Saved as stage2_{suburb}.parquet / .csv.

Irradiance source priority:
  a. BARRA2 via OPeNDAP — gated behind BARRA2_ENABLED (False until NCI
     project ob53 access lands).
  b. BARRA2 via hourly CSV (--barra-csv) — pre-extracted hourly data,
     no NCI auth needed; also produces monthly temperature stats for Stage 3.
  c. CSV file provided via --irradiance-file (lat, lon, annual_ghi_kwh_m2)
  d. NASA POWER REST API (free, no key — ~50 km resolution, cached per suburb)
  e. Melbourne default GHI constant (~1850 kWh/m²/yr) — last-resort placeholder
"""

import json
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from config.settings import (
    BARRA2_ENABLED,
    BARRA2_VARIABLES,
    MELBOURNE_DEFAULT_GHI_KWH_M2_YR,
    OUTPUT_DIR,
)
from config.suburbs import get_suburb
from shared.file_io import ensure_dir, load_stage_input, save_parquet, save_stage_outputs
from shared.logging_config import setup_logging
from stage2_irradiance.barra_client import fetch_all_climate_data, ingest_barra2_csv
from stage2_irradiance.building_orientation import compute_orientations_for_polygons
from stage2_irradiance.cool_roof_calculator import calculate_building_benefit
from stage2_irradiance.irradiance_loader import (
    load_irradiance_csv,
    load_nasa_power_irradiance,
    make_default_irradiance_df,
    nearest_ghi,
)
from stage2_irradiance.irradiance_processor import (
    compute_annual_ghi_from_hourly,
    compute_annual_irradiance_summary,
    compute_irradiance_stats,
)
from stage2_irradiance.temperature_processor import (
    compute_annual_temperature_summary,
    compute_temperature_stats,
)
from stage2_irradiance.wind_processor import (
    compute_annual_wind_summary,
    compute_wind_stats,
)

logger = setup_logging("stage2_pipeline")


def _load_polygon_latlons(suburb_key: str, n_rows: int) -> list[list[list[float]]]:
    """
    Load the Stage 1 polygon sidecar and align its length to the Stage 1 table.

    Stage 1 saves polygon_latlon separately (stage1_{suburb}_polygons.json) in
    the same row order as its parquet/CSV output. Missing or short sidecars
    (e.g. the tracked sample fixture, or older Stage 1 runs) are padded with
    empty polygons so orientation comes back as None rather than failing Stage 2.
    """
    sidecar = OUTPUT_DIR / f"stage1_{suburb_key}_polygons.json"
    if not sidecar.exists():
        logger.warning(
            "No polygon sidecar found at %s -- building orientation columns "
            "will be empty. Re-run Stage 1 to regenerate it.",
            sidecar,
        )
        return [[] for _ in range(n_rows)]

    with open(sidecar) as fh:
        polygon_latlons = json.load(fh)

    if len(polygon_latlons) != n_rows:
        logger.warning(
            "Polygon sidecar has %d entries but Stage 1 table has %d rows -- "
            "padding/truncating for orientation calculation.",
            len(polygon_latlons), n_rows,
        )
        polygon_latlons = polygon_latlons[:n_rows]
        while len(polygon_latlons) < n_rows:
            polygon_latlons.append([])

    return polygon_latlons


def run_stage2_climate(
    suburb_name: str,
    start_year: int = 2010,
    end_year: int = 2020,
) -> tuple[pd.DataFrame, float | None, float | None]:
    """
    Fetch monthly climate statistics for a suburb from BARRA2.

    Returns (climate_df, hourly_annual_ghi_kwh_m2, annual_mean_wind_speed_ms)
    where *climate_df* has monthly irradiance, temperature, and wind stats,
    *hourly_annual_ghi_kwh_m2* is the preferred annual GHI from hourly flux
    integration (mean W/m² × 8760 / 1000), and *annual_mean_wind_speed_ms* is
    the mean 10 m wind speed (BARRA2 sfcWind) — feeds Stage 3's wind-dependent
    h_out. Any of the three may be None when BARRA2 is unreachable. The
    hourly GHI value is more accurate than the monthly approximation because
    it doesn't assume constant flux across each day.

    Saved to stage2_{suburb}_climate.parquet. Returns (empty DataFrame, None,
    None) when BARRA2 is unreachable (no NCI access).
    """
    suburb = get_suburb(suburb_name)
    suburb_key = suburb.key
    lat, lon = suburb.centroid

    logger.info("=" * 60)
    logger.info("Stage 2 Climate: %s (lat=%.4f, lon=%.4f)", suburb.name, lat, lon)
    logger.info("Year range: %d-%d", start_year, end_year)
    logger.info("=" * 60)

    # ── Fetch climate data ────────────────────────────────────────────────
    logger.info("Fetching climate data from BARRA2...")
    climate_data = fetch_all_climate_data(lat, lon, start_year, end_year)

    irradiance_ds = climate_data.get("solar_irradiance")
    temperature_ds = climate_data.get("temperature_2m")
    wind_ds = climate_data.get("wind_speed_10m")
    irradiance_var = BARRA2_VARIABLES["solar_irradiance"]
    temperature_var = BARRA2_VARIABLES["temperature_2m"]
    wind_var = BARRA2_VARIABLES["wind_speed_10m"]

    if irradiance_ds is None:
        logger.warning("BARRA2 irradiance unavailable (NCI access required).")
    if temperature_ds is None:
        logger.warning("BARRA2 temperature unavailable (NCI access required).")
    if wind_ds is None:
        logger.warning("BARRA2 wind speed unavailable (NCI access required).")

    # ── Process stats ─────────────────────────────────────────────────────
    irradiance_stats = compute_irradiance_stats(irradiance_ds, irradiance_var, suburb.name)
    irradiance_summary = compute_annual_irradiance_summary(irradiance_stats)
    if irradiance_summary:
        logger.info("Annual GHI: %.2f kWh/m²/day", irradiance_summary.get("annual_mean_ghi_kwh_m2_day", 0))

    # When hourly BARRA2 data is available, compute annual GHI directly from
    # hourly flux values (mean W/m² x 8760 / 1000) rather than the monthly
    # summary approximation (mean_W x 24 / 1000 per month).  The hourly path
    # is more accurate because it doesn't assume constant flux across the day.
    hourly_annual_ghi: float | None = None
    if irradiance_ds is not None and irradiance_var in irradiance_ds:
        try:
            hourly_annual_ghi = compute_annual_ghi_from_hourly(irradiance_ds, irradiance_var)
            logger.info(
                "Annual GHI from hourly data: %.1f kWh/m²/yr "
                "(preferred over monthly approximation).",
                hourly_annual_ghi,
            )
        except Exception as e:
            logger.debug("compute_annual_ghi_from_hourly failed (non-fatal): %s", e)

    temperature_stats = compute_temperature_stats(temperature_ds, temperature_var, suburb.name)
    temperature_summary = compute_annual_temperature_summary(temperature_stats)
    if temperature_summary:
        logger.info(
            "Annual mean temp: %.1f°C, CDD: %.0f, HDD: %.0f",
            temperature_summary.get("annual_mean_temp_c", 0),
            temperature_summary.get("annual_cdd", 0),
            temperature_summary.get("annual_hdd", 0),
        )

    wind_stats = compute_wind_stats(wind_ds, wind_var, suburb.name)
    wind_summary = compute_annual_wind_summary(wind_stats)
    annual_mean_wind_speed_ms = wind_summary.get("annual_mean_wind_speed_ms")
    if wind_summary:
        logger.info("Annual mean wind speed: %.2f m/s", annual_mean_wind_speed_ms)

    if irradiance_stats.empty and temperature_stats.empty and wind_stats.empty:
        logger.warning("No climate data retrieved for %s.", suburb.name)
        return pd.DataFrame(), None, None

    combined = None
    for stats_df in (irradiance_stats, temperature_stats, wind_stats):
        if stats_df.empty:
            continue
        if combined is None:
            combined = stats_df
        else:
            combined = pd.merge(
                combined,
                stats_df.drop(columns=["suburb"], errors="ignore"),
                on="month",
                how="outer",
            )

    combined["suburb"] = suburb.name

    out_path = ensure_dir(OUTPUT_DIR) / f"stage2_{suburb_key}_climate.parquet"
    save_parquet(combined, out_path)
    logger.info("Climate data saved to: %s", out_path)
    return combined, hourly_annual_ghi, annual_mean_wind_speed_ms


def run_stage2(
    suburb_name: str,
    irradiance_file: Path | None = None,
    barra_csv: Path | None = None,
    start_year: int = 2010,
    end_year: int = 2020,
) -> pd.DataFrame:
    """
    Run the full Stage 2 pipeline: load Stage 1 buildings, assign irradiance,
    and compute per-building cool roof benefit.

    Irradiance source priority:
      1. BARRA2 via OPeNDAP (only when BARRA2_ENABLED — run_stage2_climate)
      2. BARRA2 via hourly CSV (barra_csv — pre-extracted, no NCI auth needed)
      3. CSV file at irradiance_file (lat, lon, annual_ghi_kwh_m2)
      4. NASA POWER REST API (free, no key — cached to data/raw/nasa_power/)
      5. Melbourne default GHI constant (~1850 kWh/m²/yr)

    Args:
        suburb_name: Suburb to process (must have a Stage 1 output).
        irradiance_file: Path to irradiance CSV, or None.
        barra_csv: Path to pre-extracted hourly BARRA2 CSV, or None.
        start_year: First year for BARRA2 OPeNDAP query.
        end_year: Last year for BARRA2 OPeNDAP query.

    Returns:
        DataFrame with all Stage 1 columns plus:
        azimuth_deg, wall_length_main_m, wall_length_perp_m, wall_ratio,
        annual_ghi_kwh_m2, absorptance_before, roof_surface_area_m2,
        energy_incident_kwh_yr, energy_saved_kwh_yr, co2_saved_kg_yr,
        irradiance_source, mean_wind_speed_ms (BARRA2 sfcWind; None when the
        irradiance source isn't BARRA2 — feeds Stage 3's wind-dependent h_out).
    """
    suburb = get_suburb(suburb_name)
    suburb_key = suburb.key

    logger.info("=" * 60)
    logger.info("Stage 2 Pipeline: %s", suburb.name)
    logger.info("=" * 60)

    # ── Step 1: Load Stage 1 output ───────────────────────────────────────
    df = load_stage_input(1, suburb_key)
    if df is None:
        return pd.DataFrame()
    logger.info("Step 1/4: Loaded %d buildings from Stage 1.", len(df))

    # ── Step 2: Building orientation (heading + wall aspect ratio) ────────
    logger.info("Step 2/4: Computing building orientation from footprints...")
    polygon_latlons = _load_polygon_latlons(suburb_key, len(df))
    orientation_df = compute_orientations_for_polygons(polygon_latlons)
    df = pd.concat([df.reset_index(drop=True), orientation_df.reset_index(drop=True)], axis=1)
    n_resolved = df["azimuth_deg"].notna().sum()
    logger.info(
        "Building orientation resolved for %d/%d buildings (mean wall_ratio %.2f).",
        n_resolved, len(df),
        df["wall_ratio"].mean() if n_resolved else float("nan"),
    )

    # ── Step 3: Resolve irradiance data ───────────────────────────────────
    logger.info("Step 3/4: Resolving irradiance data...")
    # irradiance_source tracks which fallback was used (written to output)
    irradiance_source: str = "unknown"
    irradiance_df: pd.DataFrame | None = None

    # Priority 1: BARRA2 via OPeNDAP. Gated behind BARRA2_ENABLED because
    # without NCI access every monthly fetch fails after a network round-trip
    # (hundreds of doomed calls per run). Flip the flag in config/settings.py
    # when ob53 access lands.
    annual_ghi_scalar: float | None = None
    annual_wind_speed_scalar: float | None = None
    if BARRA2_ENABLED:
        try:
            climate_df, hourly_ghi, annual_wind_speed_scalar = run_stage2_climate(
                suburb_name, start_year, end_year
            )
            # Prefer the hourly-derived GHI (mean W/m² × 8760 / 1000) over the
            # monthly approximation (daily_mean_kWh × 365) — the hourly path is
            # more accurate because it doesn't assume constant flux across each day.
            if hourly_ghi is not None:
                annual_ghi_scalar = hourly_ghi
                irradiance_source = "barra2"
                logger.info(
                    "Irradiance source: BARRA2 (hourly) — annual GHI %.0f kWh/m²/yr",
                    annual_ghi_scalar,
                )
            elif not climate_df.empty and "mean_ghi_kwh_m2_day" in climate_df.columns:
                daily_mean = climate_df["mean_ghi_kwh_m2_day"].mean()
                annual_ghi_scalar = round(daily_mean * 365, 1)
                irradiance_source = "barra2"
                logger.info(
                    "Irradiance source: BARRA2 (monthly approx) — annual GHI %.0f kWh/m²/yr",
                    annual_ghi_scalar,
                )
        except Exception as e:
            logger.debug("BARRA2 not available: %s", e)
    else:
        logger.debug("BARRA2 disabled (BARRA2_ENABLED=False) — skipping to CSV/NASA POWER.")

    # Priority 2: BARRA2 via pre-extracted hourly CSV.  This path mirrors
    # run_stage2_climate but reads from a local CSV instead of OPeNDAP, so it
    # works without NCI access.  When the CSV is valid we get both the annual
    # GHI scalar AND a climate DataFrame; when it fails we fall through.
    if annual_ghi_scalar is None and barra_csv is not None:
        logger.info("Irradiance source: BARRA2 hourly CSV — %s", barra_csv)
        try:
            climate_df, hourly_ghi, csv_wind_speed = ingest_barra2_csv(barra_csv, suburb.name)
            if csv_wind_speed is not None:
                annual_wind_speed_scalar = csv_wind_speed
            if hourly_ghi is not None:
                annual_ghi_scalar = hourly_ghi
                irradiance_source = "barra2_csv"
                logger.info(
                    "BARRA2 CSV annual GHI: %.0f kWh/m²/yr.", annual_ghi_scalar,
                )
                # Save climate stats alongside the stage2 output for Stage 3.
                if not climate_df.empty:
                    out_path = (
                        ensure_dir(OUTPUT_DIR)
                        / f"stage2_{suburb_key}_climate.parquet"
                    )
                    save_parquet(climate_df, out_path)
                    logger.info("BARRA2 CSV climate stats saved to: %s", out_path)
            else:
                logger.warning(
                    "BARRA2 CSV %s produced no annual GHI — falling through.", barra_csv
                )
        except Exception as e:
            logger.warning("BARRA2 CSV ingestion failed: %s — falling through.", e)

    if annual_ghi_scalar is None:
        # Priority 3: User-supplied CSV
        if irradiance_file:
            logger.info("Irradiance source: user CSV — %s", irradiance_file)
            irradiance_df = load_irradiance_csv(irradiance_file)
            irradiance_source = "csv_file"
        else:
            # Priority 4: NASA POWER (free REST API, cached per suburb)
            logger.info(
                "Irradiance source: NASA POWER API (bbox south=%.4f west=%.4f "
                "north=%.4f east=%.4f).",
                *suburb.bbox,
            )
            south, west, north, east = suburb.bbox
            nasa_df = load_nasa_power_irradiance(
                south=south,
                west=west,
                north=north,
                east=east,
                suburb_key=suburb_key,
            )
            if not nasa_df.empty:
                irradiance_df = nasa_df
                irradiance_source = "nasa_power"
                mean_ghi = nasa_df["annual_ghi_kwh_m2"].mean()
                logger.info(
                    "NASA POWER: %d grid points, mean annual GHI %.1f kWh/m²/yr.",
                    len(nasa_df), mean_ghi,
                )
            else:
                # Priority 5: Melbourne default constant (last resort)
                logger.warning(
                    "NASA POWER returned no data — falling back to Melbourne "
                    "default GHI constant (%.0f kWh/m²/yr).",
                    MELBOURNE_DEFAULT_GHI_KWH_M2_YR,
                )
                irradiance_df = make_default_irradiance_df(suburb.bbox)
                irradiance_source = "melbourne_default"

    # Assign GHI to each building
    if annual_ghi_scalar is not None:
        df["annual_ghi_kwh_m2"] = annual_ghi_scalar
    else:
        ghi_values = [
            nearest_ghi(row["lat"], row["lon"], irradiance_df)
            for _, row in df.iterrows()
        ]
        df["annual_ghi_kwh_m2"] = ghi_values

    df["irradiance_source"] = irradiance_source

    # Suburb-uniform BARRA2 wind speed (same ~11 km grid resolution as GHI/temp).
    # None when BARRA2 wasn't the source (NASA POWER / user CSV / Melbourne
    # default carry no wind data) — Stage 3 falls back to the fixed H_OUTSIDE
    # constant for those buildings.
    df["mean_wind_speed_ms"] = annual_wind_speed_scalar

    logger.info(
        "GHI assigned to %d buildings (source: %s). "
        "Mean GHI: %.1f kWh/m²/yr. Mean wind speed: %s m/s.",
        len(df), irradiance_source, df["annual_ghi_kwh_m2"].mean(),
        f"{annual_wind_speed_scalar:.2f}" if annual_wind_speed_scalar is not None else "N/A",
    )

    # ── Step 4: Compute cool roof benefit per building ────────────────────
    logger.info("Step 4/4: Computing cool roof delta per building...")
    benefit_rows = []
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Calculating benefit"):
        est = row.get("absorptance_estimate")
        unc = row.get("absorptance_uncertainty")
        benefit = calculate_building_benefit(
            area_m2=float(row["area_m2"]),
            pitch_deg=float(row.get("pitch_deg", 22.5)),
            annual_ghi_kwh_m2=float(row["annual_ghi_kwh_m2"]),
            roof_colour=row.get("roof_colour"),
            roof_material=row.get("roof_material"),
            absorptance_estimate=float(est) if est is not None and str(est) != "nan" else None,
            absorptance_uncertainty=float(unc) if unc is not None and str(unc) != "nan" else None,
        )
        benefit_rows.append(benefit)

    benefit_df = pd.DataFrame(benefit_rows)
    # Stage 1 now provides roof_surface_area_m2 directly — drop the duplicate
    # recalculated by calculate_building_benefit to avoid conflicting columns.
    if "roof_surface_area_m2" in df.columns and "roof_surface_area_m2" in benefit_df.columns:
        benefit_df = benefit_df.drop(columns=["roof_surface_area_m2"])
    df = pd.concat([df.reset_index(drop=True), benefit_df], axis=1)

    # Flag buildings where the HSV absorptance estimate had high uncertainty.
    # "low" = chromatic or mid-grey surface where classifier confidence is weaker.
    df["absorptance_confidence"] = df["absorptance_uncertainty"].apply(
        lambda u: "low" if (u is not None and not pd.isna(u) and float(u) > 0.12) else "ok"
    )

    # ── Summary ───────────────────────────────────────────────────────────
    total_energy_saved = df["energy_saved_kwh_yr"].sum()
    total_co2_saved = df["co2_saved_kg_yr"].sum()
    mean_absorptance = df["absorptance_before"].mean()
    total_roof_area = df["roof_surface_area_m2"].sum()

    logger.info(
        "Suburb %s: %d buildings | total roof surface %.0f m²",
        suburb.name, len(df), total_roof_area,
    )
    logger.info(
        "Cool roof benefit: %.0f kWh/yr saved | %.0f kg CO2/yr avoided | mean absorptance %.2f",
        total_energy_saved, total_co2_saved, mean_absorptance,
    )

    # ── Save outputs ──────────────────────────────────────────────────────
    save_stage_outputs(df, 2, suburb_key)

    logger.info("=" * 60)
    logger.info("Stage 2 complete for %s.", suburb.name)
    logger.info("=" * 60)

    return df
