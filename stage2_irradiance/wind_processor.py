"""
Wind data processor for the Raising Rooves pipeline.

Processes BARRA2 near-surface wind speed (sfcWind, m/s) into monthly and
annual stats. Feeds Stage 3's wind-dependent outdoor surface coefficient
(h_out) — see H_OUT_WIND_INTERCEPT_W_M2K / H_OUT_WIND_SLOPE_W_M2K_PER_MS in
config/settings.py.
"""

import pandas as pd
import xarray as xr

from shared.logging_config import setup_logging

logger = setup_logging("wind_processor")


def compute_wind_stats(
    wind_ds: xr.Dataset,
    variable_name: str,
    suburb_name: str,
) -> pd.DataFrame:
    """
    Compute monthly wind speed statistics from raw BARRA2 data.

    Args:
        wind_ds: xarray Dataset with the wind speed variable (m/s, sfcWind).
        variable_name: Name of the wind variable in the dataset.
        suburb_name: Suburb name for labelling output.

    Returns:
        DataFrame with columns: suburb, month, mean_wind_speed_ms,
        max_wind_speed_ms.
    """
    if wind_ds is None or variable_name not in wind_ds:
        logger.warning("No wind data available for '%s'.", suburb_name)
        return pd.DataFrame()

    data = wind_ds[variable_name]
    monthly = data.groupby("time.month")

    records = []
    for month, group in monthly:
        mean_wind = float(group.mean().values)
        max_wind = float(group.max().values)
        records.append({
            "suburb": suburb_name,
            "month": int(month),
            "mean_wind_speed_ms": round(mean_wind, 2),
            "max_wind_speed_ms": round(max_wind, 2),
        })

    df = pd.DataFrame(records)

    if not df.empty:
        annual_mean = df["mean_wind_speed_ms"].mean()
        logger.info(
            "Wind stats for '%s': annual mean = %.2f m/s", suburb_name, annual_mean,
        )

    return df


def compute_annual_wind_summary(monthly_stats: pd.DataFrame) -> dict:
    """
    Compute annual wind summary from monthly stats.

    Args:
        monthly_stats: DataFrame from compute_wind_stats().

    Returns:
        Dict with annual_mean_wind_speed_ms, or {} if input is empty.
    """
    if monthly_stats.empty:
        return {}

    return {
        "annual_mean_wind_speed_ms": round(monthly_stats["mean_wind_speed_ms"].mean(), 2),
        "annual_max_wind_speed_ms": round(monthly_stats["max_wind_speed_ms"].max(), 2),
    }
