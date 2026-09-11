"""
Fetch a single-point hourly BARRA2 weather extract for the heat-ingress model.

The ``heat_ingress_model`` notebook needs one hourly CSV for a single BARRA2
grid point with these columns (one row per hour, whole calendar year)::

    time_UTC, rsdsdir_Wm2, rsdsdif_Wm2, temp_K, rel_humidity_percent,
    wind_ms, rsds_total_Wm2, temp_C

Historically this file was produced outside the repo (NCI Gadi / THREDDS
subsetting) and dropped into a Google Drive "Input Tables" folder.  This tool
reproduces it from the same public BARRA2 OPeNDAP endpoint the Stage 2
pipeline uses (``config.settings.BARRA2_*``), so the notebook can be run from
a clone with no external files.

Variables pulled (AUS-11 BARRA-R2, 1hr, one file per calendar month):
    rsds     -> rsds_total_Wm2
    rsdsdir  -> rsdsdir_Wm2      (diffuse = rsds - rsdsdir, clipped at 0)
    tas      -> temp_K / temp_C
    hurs     -> rel_humidity_percent
    sfcWind  -> wind_ms

Usage::

    python -m tools.fetch_heat_ingress_weather                # Clayton-ish default, 2007
    python -m tools.fetch_heat_ingress_weather --lat -37.91 --lon 145.13 --year 2007
    python -m tools.fetch_heat_ingress_weather --out "Input Tables/barra2_-37.91_145.13_2007.csv"
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from config.settings import BARRA2_DOMAIN, BARRA2_THREDDS_BASE, BARRA2_VARIABLES
from shared.logging_config import setup_logging

logger = setup_logging("fetch_heat_ingress_weather")

# Repo root (…/tools/fetch_heat_ingress_weather.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[1]

# OPeNDAP engines to try, in order of preference. Only engines xarray actually
# has installed are attempted (netcdf4 wheels frequently lack DAP support; pydap
# is the pure-Python path). Unknown/again-uninstalled engines are skipped.
_PREFERRED_ENGINES = ("pydap", "netcdf4")

# NCI THREDDS occasionally drops a single OPeNDAP request under load; retry before
# giving up on a whole month.
_OPEN_ATTEMPTS = 4
_RETRY_BACKOFF_S = 3.0


def _available_engines() -> tuple[str, ...]:
    """Preferred OPeNDAP engines that xarray can actually use in this environment."""
    try:
        installed = set(xr.backends.list_engines())
    except Exception:  # noqa: BLE001 - be permissive; fall back to the full list
        installed = set(_PREFERRED_ENGINES)
    engines = tuple(e for e in _PREFERRED_ENGINES if e in installed)
    return engines or _PREFERRED_ENGINES


def _build_url(var_name: str, year: int, month: int) -> str:
    """Build the BARRA2 OPeNDAP URL for one variable and one calendar month."""
    yyyymm = f"{year}{month:02d}"
    filename = (
        f"{var_name}_{BARRA2_DOMAIN}_ERA5_historical_hres_BOM_BARRA-R2_v1"
        f"_1hr_{yyyymm}-{yyyymm}.nc"
    )
    return (
        f"{BARRA2_THREDDS_BASE}/output/reanalysis/{BARRA2_DOMAIN}"
        f"/BOM/ERA5/historical/hres/BARRA-R2/v1/1hr/{var_name}/latest/{filename}"
    )


def _open_point(url: str, lat: float, lon: float) -> xr.Dataset:
    """
    Open a remote BARRA2 file and select the nearest grid point.

    Tries each available engine, and retries transient failures (NCI THREDDS
    drops the odd request under load) with a short backoff before giving up.
    """
    engines = _available_engines()
    last_err: Exception | None = None
    for attempt in range(1, _OPEN_ATTEMPTS + 1):
        for engine in engines:
            try:
                ds = xr.open_dataset(url, engine=engine)
                return ds.sel(lat=lat, lon=lon, method="nearest").load()
            except Exception as exc:  # noqa: BLE001 - try next engine / retry
                last_err = exc
                logger.debug(
                    "engine=%s attempt=%d failed for %s: %s", engine, attempt, url, exc
                )
        if attempt < _OPEN_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(
        f"Could not open {url} with engines {engines} after {_OPEN_ATTEMPTS} attempts: {last_err}"
    )


def _fetch_variable_year(
    var_key: str, lat: float, lon: float, year: int
) -> pd.Series:
    """Fetch one BARRA2 variable for a whole year as an hourly Series."""
    var_name = BARRA2_VARIABLES[var_key]
    monthly: list[pd.Series] = []
    for month in range(1, 13):
        url = _build_url(var_name, year, month)
        logger.info("Fetching %s %d-%02d ...", var_name, year, month)
        point = _open_point(url, lat, lon)
        series = point[var_name].to_series()
        monthly.append(series)
        logger.info(
            "  %s %d-%02d: %d hours, grid point lat=%.3f lon=%.3f",
            var_name, year, month, len(series),
            float(point["lat"]), float(point["lon"]),
        )
    return pd.concat(monthly).sort_index()


def build_hourly_weather_table(lat: float, lon: float, year: int) -> pd.DataFrame:
    """
    Build the hourly BARRA2 weather table for one grid point and one year.

    Args:
        lat: Latitude in EPSG:4326.
        lon: Longitude in EPSG:4326.
        year: Calendar year (BARRA2 reference year; the project default is 2007).

    Returns:
        DataFrame with columns time_UTC, rsdsdir_Wm2, rsdsdif_Wm2, temp_K,
        rel_humidity_percent, wind_ms, rsds_total_Wm2, temp_C — one row per hour.
    """
    # tas / hurs / sfcWind are instantaneous, stamped at the top of the hour
    # (bounds [HH:00, HH+1:00)).  rsds / rsdsdir are hour-mean fluxes stamped at
    # the interval midpoint (HH:30) with the same bounds.  Flooring every series
    # to the hour therefore lines them all up on the hour they describe, exactly
    # as the original hand-built "Input Tables" CSV had them.
    def _hourly(series: pd.Series) -> pd.Series:
        s = series.copy()
        s.index = pd.to_datetime(s.index, utc=True).floor("h")
        return s[~s.index.duplicated(keep="first")].sort_index()

    rsds = _hourly(_fetch_variable_year("solar_irradiance", lat, lon, year))
    rsdsdir = _hourly(_fetch_variable_year("solar_irradiance_direct", lat, lon, year))
    tas = _hourly(_fetch_variable_year("temperature_2m", lat, lon, year))
    hurs = _hourly(_fetch_variable_year("relative_humidity_2m", lat, lon, year))
    sfc_wind = _hourly(_fetch_variable_year("wind_speed_10m", lat, lon, year))

    frame = pd.DataFrame(
        {
            "rsds_total_Wm2": rsds,
            "rsdsdir_Wm2": rsdsdir,
            "temp_K": tas,
            "rel_humidity_percent": hurs,
            "wind_ms": sfc_wind,
        }
    )
    # Keep only whole hours that have every variable (drops the dangling hour
    # that a month-boundary midpoint stamp can leave behind).
    frame = frame[frame.index.year == year].dropna(how="any").sort_index()

    frame["rsdsdif_Wm2"] = (frame["rsds_total_Wm2"] - frame["rsdsdir_Wm2"]).clip(lower=0.0)
    frame["temp_C"] = frame["temp_K"] - 273.15

    frame = frame.reset_index(names="time_UTC")
    frame["time_UTC"] = frame["time_UTC"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
    frame["time_UTC"] = frame["time_UTC"].str.replace(
        r"([+-]\d{2})(\d{2})$", r"\1:\2", regex=True
    )

    ordered = [
        "time_UTC",
        "rsdsdir_Wm2",
        "rsdsdif_Wm2",
        "temp_K",
        "rel_humidity_percent",
        "wind_ms",
        "rsds_total_Wm2",
        "temp_C",
    ]
    return frame[ordered]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a single-point hourly BARRA2 weather CSV for the heat-ingress model."
    )
    parser.add_argument("--lat", type=float, default=-37.91)
    parser.add_argument("--lon", type=float, default=145.13)
    parser.add_argument("--year", type=int, default=2007)
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help=(
            "Output CSV path (relative to repo root). "
            "Default: 'Input Tables/barra2_{lat}_{lon}_{year}.csv'."
        ),
    )
    args = parser.parse_args()

    if args.out is None:
        out_path = REPO_ROOT / "Input Tables" / (
            f"barra2_{args.lat:.2f}_{args.lon:.2f}_{args.year}.csv"
        )
    else:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = REPO_ROOT / out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Building hourly BARRA2 weather table for lat=%.3f lon=%.3f year=%d",
        args.lat, args.lon, args.year,
    )
    table = build_hourly_weather_table(args.lat, args.lon, args.year)
    table.to_csv(out_path, index=False)
    logger.info("Wrote %d rows -> %s", len(table), out_path)
    print(table.head().to_string(index=False))
    print(f"\nSaved: {out_path}  ({len(table)} rows)")


if __name__ == "__main__":
    main()
