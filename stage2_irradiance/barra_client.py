"""
BARRA2 climate data client for the Raising Rooves pipeline.

Accesses the Bureau of Meteorology's BARRA2 reanalysis dataset via
OPeNDAP on the NCI THREDDS server. BARRA2 provides:
  - ~11 km spatial resolution (AUS-11 BARRA-R2 grid)
  - Hourly temporal resolution
  - Variables: solar irradiance (rsds), temperature (tas), wind speed (sfcWind)

Data is cached locally as NetCDF files to avoid repeated downloads.

NCI account required. Monash students: register at https://my.nci.org.au
and ask your supervisor (Stuart) for project ob53 access.

URL structure (verified against NCI THREDDS catalog 2026-04-30):
  OPeNDAP base:  https://thredds.nci.org.au/thredds/dodsC/ob53
  Catalog base:  https://thredds.nci.org.au/thredds/catalog/ob53
  Path template: output/reanalysis/{domain}/BOM/ERA5/historical/hres/BARRA-R2/v1/
                 1hr/{variable}/{version}/{variable}_{domain}_ERA5_historical_hres_BOM_BARRA-R2_v1_1hr_{YYYYMM}-{YYYYMM}.nc

  Notes:
  - Domain for BARRA-R2 is AUS-11 (11 km), NOT AUS-04 (that is BARRA-C2).
  - The THREDDS path does NOT include the 'BARRA2/' product subfolder that
    exists on the gdata filesystem.
  - Variable names are CORDEX/CF names (rsds, tas) not UM internal names
    (av_swsfcdown, temp_scrn).
  - Each file covers one calendar month. Use 'latest' as the version token
    to resolve the most recent data release without knowing the exact date tag.

── BARRA2 Ingestion Checklist ────────────────────────────────────────────────
When NCI project ob53 access lands, here is exactly what to do:

1. Flip the master switch in config/settings.py:
       BARRA2_ENABLED = True

2. Verify connectivity (no NCI auth needed for this check):
       python -c "from stage2_irradiance.barra_client import check_barra2_connection; print(check_barra2_connection())"

3. Run a single-suburb smoke test:
       python -m stage2_irradiance.run_stage2 --suburb Carlton --debug

   This calls fetch_all_climate_data() → fetch_barra_data() for
   solar_irradiance (rsds), temperature_2m (tas), and wind_speed_10m
   (sfcWind), one year at a time, 12 months per year.  Each month is a
   separate OPeNDAP request.
   Cached under data/raw/barra/{solar_irradiance,temperature_2m,wind_speed_10m}/.

4. The Stage 2 pipeline then:
   a. Computes monthly irradiance stats via compute_irradiance_stats()
   b. Computes annual GHI from hourly rsds via compute_annual_ghi_from_hourly()
      (mean W/m² × 8760 / 1000 = kWh/m²/yr — the more accurate hourly path)
   c. Returns the hourly-derived GHI alongside the climate DataFrame so
      run_stage2() can use it directly (the GHI discard bug is fixed)
   d. Saves climate summary to stage2_{suburb}_climate.parquet
   e. Uses the hourly-derived annual GHI scalar as the irradiance source
      for per-building cool-roof calculations

5. To add more climate variables (e.g. longwave radiation rlds, precipitation pr):
   a. Add entries to BARRA2_VARIABLES in config/settings.py
   b. Add the key to the list in fetch_all_climate_data()
   c. Add a processing function (or use the existing ones in
      irradiance_processor.py / temperature_processor.py)
"""

import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from config.settings import (
    BARRA2_CATALOG_BASE,
    BARRA2_DOMAIN,
    BARRA2_THREDDS_BASE,
    BARRA2_VARIABLES,
    BARRA_DIR,
)
from shared.file_io import ensure_dir
from shared.logging_config import setup_logging

logger = setup_logging("barra_client")

# Connection test timeout in seconds — short so the pipeline fails fast.
_CONNECTION_TIMEOUT_S = 8


def _build_barra2_url(variable_key: str, year: int, month: int = 1) -> str:
    """
    Build the OPeNDAP URL for a BARRA2 variable, year, and month.

    BARRA2 files on NCI THREDDS are one file per calendar month.  The 'latest'
    version token resolves to the most recently published data release without
    requiring the caller to know the exact release date tag (e.g. v20231001).

    Path components (all confirmed from live catalog 2026-04-30):
      ob53                   — NCI project code, top-level directory on THREDDS
      output/reanalysis      — product type (model output, reanalysis)
      {domain}               — AUS-11 (BARRA-R2 11 km grid)
      BOM                    — RCM institution
      ERA5                   — lateral boundary driving model
      historical/hres        — experiment / variant label
      BARRA-R2               — source model
      v1                     — version realisation
      1hr                    — temporal frequency
      {variable}             — CORDEX/CF variable name (e.g. rsds, tas)
      latest                 — version folder (points to most recent release)
      {filename}             — one file per month

    Args:
        variable_key: Key from BARRA2_VARIABLES (e.g. "solar_irradiance").
        year: Calendar year (e.g. 2015).
        month: Calendar month 1–12 (default 1).

    Returns:
        OPeNDAP URL string.
    """
    var_name = BARRA2_VARIABLES[variable_key]
    yyyymm = f"{year}{month:02d}"
    filename = (
        f"{var_name}_{BARRA2_DOMAIN}_ERA5_historical_hres_BOM_BARRA-R2_v1"
        f"_1hr_{yyyymm}-{yyyymm}.nc"
    )
    return (
        f"{BARRA2_THREDDS_BASE}/output/reanalysis/{BARRA2_DOMAIN}"
        f"/BOM/ERA5/historical/hres/BARRA-R2/v1/1hr/{var_name}/latest/{filename}"
    )


def _build_barra2_catalog_url(variable_key: str) -> str:
    """
    Return the THREDDS catalog HTML URL for a BARRA2 variable.

    Useful for debugging without NCI OPeNDAP access — the catalog page is
    publicly browsable even when OPeNDAP requires authentication.

    Args:
        variable_key: Key from BARRA2_VARIABLES.

    Returns:
        THREDDS catalog URL string.
    """
    var_name = BARRA2_VARIABLES[variable_key]
    return (
        f"{BARRA2_CATALOG_BASE}/output/reanalysis/{BARRA2_DOMAIN}"
        f"/BOM/ERA5/historical/hres/BARRA-R2/v1/1hr/{var_name}/catalog.html"
    )


def check_barra2_connection() -> bool:
    """
    Check whether the NCI THREDDS server is reachable.

    Tries to open the THREDDS catalog URL (not OPeNDAP) with a short timeout.
    This does not require NCI authentication, so a True result only confirms
    that the server is reachable — not that OPeNDAP data access will succeed.

    Returns:
        True if the catalog URL responds with HTTP 200, False otherwise.
    """
    url = _build_barra2_catalog_url("solar_irradiance")
    logger.info("Testing BARRA2 connection: %s", url)
    try:
        with urllib.request.urlopen(url, timeout=_CONNECTION_TIMEOUT_S) as resp:
            if resp.status == 200:
                logger.info("BARRA2 THREDDS catalog reachable (HTTP 200).")
                return True
            logger.warning(
                "BARRA2 THREDDS catalog returned HTTP %d — unexpected.", resp.status
            )
            return False
    except urllib.error.HTTPError as e:
        logger.warning("BARRA2 THREDDS catalog HTTP error %d: %s", e.code, e.reason)
    except urllib.error.URLError as e:
        logger.warning(
            "BARRA2 THREDDS catalog unreachable: %s. "
            "Check network connectivity or NCI VPN.",
            e.reason,
        )
    except OSError as e:
        logger.warning("BARRA2 connection test failed: %s", e)
    return False


def _get_cache_path(variable_key: str, lat: float, lon: float, year: int) -> Path:
    """Get the local cache path for a downloaded BARRA2 data slice."""
    cache_dir = ensure_dir(BARRA_DIR / variable_key)
    return cache_dir / f"{variable_key}_lat{lat:.2f}_lon{lon:.2f}_{year}.nc"


def fetch_barra_data(
    variable_key: str,
    lat: float,
    lon: float,
    start_year: int,
    end_year: int,
) -> xr.Dataset | None:
    """
    Fetch BARRA2 data for a specific variable and location over a year range.

    Selects the nearest grid point to the given lat/lon.  Caches data locally
    to avoid repeated OPeNDAP requests.  Fails gracefully when NCI is not
    accessible — returns None and logs a WARNING rather than raising.

    BARRA2 files are one file per calendar month.  This function fetches all
    12 months for each year in [start_year, end_year] and concatenates them
    along the time dimension.

    Args:
        variable_key: Key from BARRA2_VARIABLES (e.g. "solar_irradiance").
        lat: Latitude in EPSG:4326.
        lon: Longitude in EPSG:4326.
        start_year: First year to fetch (inclusive).
        end_year: Last year to fetch (inclusive).

    Returns:
        xarray Dataset with the requested variable, or None if fetch fails.
    """
    var_name = BARRA2_VARIABLES.get(variable_key)
    if var_name is None:
        logger.error(
            "Unknown variable key: '%s'. Available: %s",
            variable_key,
            list(BARRA2_VARIABLES.keys()),
        )
        return None

    all_datasets: list[xr.Dataset] = []

    for year in range(start_year, end_year + 1):
        # Check for a year-level cache first
        cache_path = _get_cache_path(variable_key, lat, lon, year)
        if cache_path.exists():
            logger.debug("Loading cached data: %s", cache_path)
            all_datasets.append(xr.open_dataset(cache_path))
            continue

        # Fetch all 12 months from OPeNDAP and concatenate into one year
        monthly_slices: list[xr.Dataset] = []
        for month in range(1, 13):
            url = _build_barra2_url(variable_key, year, month)
            logger.info(
                "Fetching BARRA2 %s %d-%02d from OPeNDAP...", variable_key, year, month
            )
            try:
                ds_remote = xr.open_dataset(url, engine="netcdf4")
                ds_point = ds_remote.sel(lat=lat, lon=lon, method="nearest")
                ds_point = ds_point.load()
                monthly_slices.append(ds_point)
            except Exception as e:
                logger.warning(
                    "Failed to fetch BARRA2 %s %d-%02d: %s. "
                    "NCI OPeNDAP access may require authentication — "
                    "register at https://my.nci.org.au and request ob53 access.",
                    variable_key,
                    year,
                    month,
                    e,
                )

        if not monthly_slices:
            logger.warning(
                "No monthly data retrieved for BARRA2 %s %d — skipping year.",
                variable_key,
                year,
            )
            continue

        # Concatenate months → annual dataset, cache to disk
        ds_year = xr.concat(monthly_slices, dim="time")
        try:
            ds_year.to_netcdf(cache_path)
            logger.info("Cached BARRA2 %s %d -> %s", variable_key, year, cache_path)
        except Exception as e:
            logger.warning("Could not cache BARRA2 %s %d: %s", variable_key, year, e)

        all_datasets.append(ds_year)

    if not all_datasets:
        logger.warning(
            "No BARRA2 data retrieved for %s (lat=%.4f, lon=%.4f, %d–%d). "
            "Catalog URL for manual inspection: %s",
            variable_key,
            lat,
            lon,
            start_year,
            end_year,
            _build_barra2_catalog_url(variable_key),
        )
        return None

    combined = xr.concat(all_datasets, dim="time")
    logger.info(
        "BARRA2 %s: loaded %d timesteps from %d to %d.",
        variable_key,
        len(combined.time),
        start_year,
        end_year,
    )
    return combined


def ingest_barra2_csv(
    csv_path: Path,
    suburb_name: str,
) -> tuple[pd.DataFrame, float | None]:
    """
    Ingest a pre-extracted hourly BARRA2 CSV file and compute climate stats.

    This is the CSV-based equivalent of ``fetch_all_climate_data()`` +
    ``compute_irradiance_stats()`` + ``compute_temperature_stats()`` +
    ``compute_annual_ghi_from_hourly()``.  It exists so the pipeline can use
    BARRA2 data that has been pre-extracted outside the OPeNDAP path (e.g. via
    NCI Gadi job submission or the THREDDS subsetting service).

    Expected CSV columns (one row per hour)::

        time_UTC, rsds_total_Wm2, temp_C

    The file may also carry extra columns (rsdsdir_Wm2, rsdsdif_Wm2, temp_K,
    rel_humidity_percent) — they are ignored. An optional ``wind_ms`` column
    (10 m wind speed, m/s) IS used when present, to compute an annual mean
    wind speed for Stage 3's wind-dependent h_out.

    Args:
        csv_path: Path to the hourly BARRA2 CSV file.
        suburb_name: Suburb name for labelling output rows.

    Returns:
        (climate_df, annual_ghi_kwh_m2, annual_mean_wind_speed_ms) where
        *climate_df* has monthly irradiance and temperature stats (same
        schema as the OPeNDAP path), *annual_ghi_kwh_m2* is the hourly-derived
        annual GHI scalar, and *annual_mean_wind_speed_ms* is the mean of the
        ``wind_ms`` column (None if that column isn't present).
        Returns (empty DataFrame, None, None) when the CSV is missing or
        unreadable.
    """
    if not csv_path.exists():
        logger.warning("BARRA2 CSV not found: %s", csv_path)
        return pd.DataFrame(), None, None

    try:
        df = pd.read_csv(csv_path, parse_dates=["time_UTC"])
    except Exception as e:
        logger.warning("Failed to read BARRA2 CSV %s: %s", csv_path, e)
        return pd.DataFrame(), None, None

    required = {"time_UTC", "rsds_total_Wm2", "temp_C"}
    missing = required - set(df.columns)
    if missing:
        logger.warning(
            "BARRA2 CSV %s is missing required columns: %s. Found: %s",
            csv_path, missing, set(df.columns),
        )
        return pd.DataFrame(), None, None

    logger.info(
        "Ingesting BARRA2 hourly CSV: %s (%d rows, %d columns).",
        csv_path.name, len(df), len(df.columns),
    )

    # ── Monthly irradiance stats ────────────────────────────────────────────
    df["month"] = df["time_UTC"].dt.month

    irradiance_records = []
    for month, group in df.groupby("month"):
        rsds = group["rsds_total_Wm2"]
        mean_w_m2 = float(rsds.mean())
        peak_w_m2 = float(rsds.max())
        mean_kwh_m2_day = mean_w_m2 * 24 / 1000  # 24h mean flux → daily energy
        irradiance_records.append({
            "suburb": suburb_name,
            "month": int(month),
            "mean_ghi_w_m2": round(mean_w_m2, 2),
            "mean_ghi_kwh_m2_day": round(mean_kwh_m2_day, 2),
            "peak_ghi_w_m2": round(peak_w_m2, 2),
        })

    irradiance_stats = pd.DataFrame(irradiance_records)

    # ── Monthly temperature stats ───────────────────────────────────────────
    from config.settings import CDD_BASE_TEMP, HDD_BASE_TEMP

    temperature_records = []
    for month, group in df.groupby("month"):
        temp = group["temp_C"]
        mean_temp = float(temp.mean())
        max_temp = float(temp.max())
        min_temp = float(temp.min())
        days_in_month = group["time_UTC"].dt.day.nunique()
        cdd = max(0, mean_temp - CDD_BASE_TEMP) * days_in_month
        hdd = max(0, HDD_BASE_TEMP - mean_temp) * days_in_month
        temperature_records.append({
            "suburb": suburb_name,
            "month": int(month),
            "mean_temp_c": round(mean_temp, 2),
            "max_temp_c": round(max_temp, 2),
            "min_temp_c": round(min_temp, 2),
            "cdd": round(cdd, 1),
            "hdd": round(hdd, 1),
        })

    temperature_stats = pd.DataFrame(temperature_records)

    # ── Merge irradiance + temperature on month ─────────────────────────────
    combined = pd.merge(
        irradiance_stats,
        temperature_stats.drop(columns=["suburb"], errors="ignore"),
        on="month",
        how="outer",
    )
    combined["suburb"] = suburb_name

    # ── Annual GHI from hourly rsds ─────────────────────────────────────────
    # mean W/m² × 8760 h/yr ÷ 1000 = kWh/m²/yr
    valid_rsds = df["rsds_total_Wm2"].dropna()
    if len(valid_rsds) == 0:
        logger.warning("No valid rsds_total_Wm2 values in %s.", csv_path.name)
        annual_ghi = None
    else:
        mean_w_m2 = float(valid_rsds.mean())
        annual_ghi = round(mean_w_m2 * 8760 / 1000, 1)
        logger.info(
            "BARRA2 CSV annual GHI: %.1f kWh/m²/yr (mean %.2f W/m² over %d hours).",
            annual_ghi, mean_w_m2, len(valid_rsds),
        )

    # ── Annual mean wind speed (optional column) ────────────────────────────
    annual_wind_ms: float | None = None
    if "wind_ms" in df.columns:
        valid_wind = df["wind_ms"].dropna()
        if len(valid_wind) > 0:
            annual_wind_ms = round(float(valid_wind.mean()), 2)
            logger.info(
                "BARRA2 CSV annual mean wind speed: %.2f m/s (%d hours).",
                annual_wind_ms, len(valid_wind),
            )

    logger.info(
        "BARRA2 CSV ingestion complete: %d monthly rows, annual GHI %s kWh/m²/yr.",
        len(combined), f"{annual_ghi:.0f}" if annual_ghi is not None else "N/A",
    )
    return combined, annual_ghi, annual_wind_ms


def fetch_all_climate_data(
    lat: float,
    lon: float,
    start_year: int = 1990,
    end_year: int = 2020,
) -> dict[str, xr.Dataset | None]:
    """
    Fetch all required climate variables for a location.

    Args:
        lat: Latitude in EPSG:4326.
        lon: Longitude in EPSG:4326.
        start_year: First year (inclusive).
        end_year: Last year (inclusive).

    Returns:
        Dict mapping variable keys to xarray Datasets (or None if failed).
    """
    results: dict[str, xr.Dataset | None] = {}
    for key in ["solar_irradiance", "temperature_2m", "wind_speed_10m"]:
        results[key] = fetch_barra_data(key, lat, lon, start_year, end_year)
    return results
