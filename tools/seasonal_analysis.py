"""
Seasonal cool roof analysis with R_roof (insulation) sensitivity.

Shows how the cool roof benefit varies across the year — from winter solstice
to summer solstice — and how roof insulation (R_roof) attenuates or amplifies
the seasonal swing.

A cool roof is NOT always beneficial:
  - Summer (cooling months): reflects solar radiation → reduces AC load → SAVINGS
  - Winter (heating months): reflects desirable passive solar gain → INCREASES
    heating demand → PENALTY

The net annual effect depends on the climate's cooling/heating balance and the
building's insulation level.  Well-insulated buildings (high R_roof) see smaller
effects in both directions; poorly-insulated buildings (low R_roof) see large
seasonal swings.

Usage:
    python -m tools.seasonal_analysis --suburb Clayton
    python -m tools.seasonal_analysis --suburb Carlton --r-values 0.5,1.0,2.5,5.0
    python -m tools.seasonal_analysis --list-suburbs

Inputs (read from data/output/):
    stage2_{suburb}_climate.parquet  — 12 monthly rows (GHI, temp, CDD, HDD)
    stage2_{suburb}.parquet           — per-building attributes

Outputs:
    data/output/stage2_{suburb}_seasonal.png  — 2-panel seasonal visualization
"""

import argparse
import calendar
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config.settings import (
    CDD_BASE_TEMP,
    COOL_ROOF_ABSORPTANCE,
    COOLING_FRACTION,
    HDD_BASE_TEMP,
    HEATING_FRACTION,
    HVAC_COP_COMMERCIAL,
    HVAC_COP_RESIDENTIAL,
    H_OUTSIDE_W_M2K,
    OUTPUT_DIR,
    PROJECT_ROOT,
)
from shared.logging_config import setup_logging
from stage2_irradiance.cool_roof_calculator import cool_absorptance_for_material

logger = setup_logging("seasonal_analysis")

# ── Default R_roof sweep values (m2.K/W) ──────────────────────────────────
DEFAULT_R_VALUES = [0.5, 1.5, 2.5, 3.2, 5.0]

# Human-readable labels for each R_roof value
R_VALUE_LABELS: dict[float, str] = {
    0.5: "R0.5 (uninsulated)",
    1.5: "R1.5 (metal deck)",
    2.5: "R2.5 (modern resi)",
    3.2: "R3.2 (well insulated)",
    5.0: "R5.0 (passive house)",
}

# Days in each month (non-leap year — error is negligible for climate normals)
DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

# ── Commercial building type detection ─────────────────────────────────────
_COMMERCIAL_TYPES = frozenset({
    "commercial", "office", "retail", "industrial", "warehouse",
})


def _heat_fraction(r_roof: float) -> float:
    """U/(U + h_out) — fraction of roof-surface heat that conducts to interior."""
    if r_roof <= 0:
        return 0.0
    u_roof = 1.0 / r_roof
    return u_roof / (u_roof + H_OUTSIDE_W_M2K)


def _is_commercial(building_type: str | None) -> bool:
    """Return True if the building type maps to a commercial COP category."""
    if building_type is None:
        return False
    return str(building_type).lower().strip() in _COMMERCIAL_TYPES


def load_climate_stats(suburb_key: str) -> pd.DataFrame | None:
    """Load the monthly climate parquet for a suburb."""
    path = OUTPUT_DIR / f"stage2_{suburb_key}_climate.parquet"
    if not path.exists():
        logger.error("Climate stats not found: %s", path)
        logger.error(
            "Run Stage 2 first: python -m stage2_irradiance.run_stage2 --suburb %s",
            suburb_key.replace("_", " ").title(),
        )
        return None
    return pd.read_parquet(path)


def load_buildings(suburb_key: str) -> pd.DataFrame | None:
    """Load the Stage 2 per-building parquet for a suburb."""
    path = OUTPUT_DIR / f"stage2_{suburb_key}.parquet"
    if not path.exists():
        logger.error("Stage 2 output not found: %s", path)
        return None
    df = pd.read_parquet(path)
    required = {"area_m2", "absorptance_before"}
    missing = required - set(df.columns)
    if missing:
        logger.error("Stage 2 output missing columns: %s", missing)
        return None
    return df


def compute_monthly_effects(
    buildings: pd.DataFrame,
    climate: pd.DataFrame,
    r_roof: float,
) -> pd.DataFrame:
    """
    Compute per-month cool roof electricity effect for a given R_roof value.

    Returns a DataFrame with columns: month, cooling_mwh, heating_mwh, net_mwh.
    Positive = electricity saved (benefit), negative = electricity penalty.
    """
    fraction = _heat_fraction(r_roof)
    # Coated absorptance appropriate to each roof type (same as Stages 2/3).
    if "roof_material" in buildings.columns:
        absorptance_after = buildings["roof_material"].apply(cool_absorptance_for_material)
    else:
        absorptance_after = COOL_ROOF_ABSORPTANCE

    records = []
    for _, row in climate.iterrows():
        month = int(row["month"])
        days = DAYS_IN_MONTH[month - 1]
        ghi_month_kwh_m2 = float(row["mean_ghi_kwh_m2_day"]) * days
        mean_temp = float(row["mean_temp_c"])
        cdd = float(row.get("cdd", 0))
        hdd = float(row.get("hdd", 0))

        # Per-building: solar not absorbed this month
        # GHI_month × area × (α_before - α_cool)
        solar_blocked = (
            ghi_month_kwh_m2
            * buildings["area_m2"]
            * (buildings["absorptance_before"] - absorptance_after)
        )
        # Clamp: already-cool roofs (α <= coated α) save nothing
        solar_blocked = solar_blocked.clip(lower=0)

        # Heat that reaches the interior through the roof
        heat_to_interior = solar_blocked * fraction

        # Select COP per building (commercial vs residential)
        cop = buildings["building_type"].apply(
            lambda bt: HVAC_COP_COMMERCIAL if _is_commercial(bt) else HVAC_COP_RESIDENTIAL
        )

        # Cooling benefit: positive
        cooling_effect = (heat_to_interior * COOLING_FRACTION / cop).sum() / 1000  # kWh -> MWh

        # Heating penalty: negative (cool roof reflects desirable winter sun)
        heating_effect = (heat_to_interior * HEATING_FRACTION / cop).sum() / 1000  # kWh -> MWh

        # Net: only one side applies per month (or both in shoulder months)
        is_cooling = cdd > 0
        is_heating = hdd > 0

        net_mwh = 0.0
        if is_cooling:
            net_mwh += cooling_effect
        if is_heating:
            net_mwh -= heating_effect  # penalty = negative

        records.append({
            "month": month,
            "ghi_kwh_m2": round(ghi_month_kwh_m2, 1),
            "mean_temp_c": mean_temp,
            "cdd": cdd,
            "hdd": hdd,
            "cooling_mwh": round(cooling_effect, 1),
            "heating_mwh": round(heating_effect, 1),
            "net_mwh": round(net_mwh, 1),
        })

    return pd.DataFrame(records)


def run_seasonal_analysis(
    suburb_name: str,
    r_values: list[float] | None = None,
) -> Path | None:
    """
    Run the seasonal analysis for a suburb and produce the visualization.

    Args:
        suburb_name: Suburb name (e.g. "Clayton").
        r_values: R_roof values to sweep. Defaults to DEFAULT_R_VALUES.

    Returns:
        Path to the generated PNG, or None on failure.
    """
    from config.suburbs import get_suburb

    if r_values is None:
        r_values = DEFAULT_R_VALUES

    suburb = get_suburb(suburb_name)
    suburb_key = suburb.key

    logger.info("=" * 60)
    logger.info("Seasonal Analysis: %s", suburb.name)
    logger.info("R_roof sweep: %s", r_values)
    logger.info("=" * 60)

    # ── Load data ──────────────────────────────────────────────────────────
    climate = load_climate_stats(suburb_key)
    if climate is None or climate.empty:
        return None

    buildings = load_buildings(suburb_key)
    if buildings is None or buildings.empty:
        return None

    logger.info(
        "Loaded %d buildings, %d climate months.",
        len(buildings), len(climate),
    )

    # ── Compute per-R_roof monthly effects ──────────────────────────────────
    all_results: dict[float, pd.DataFrame] = {}
    for r in r_values:
        monthly = compute_monthly_effects(buildings, climate, r)
        all_results[r] = monthly
        net_annual = monthly["net_mwh"].sum()
        label = R_VALUE_LABELS.get(r, f"R{r}")
        fraction = _heat_fraction(r)
        logger.info(
            "R=%.1f (%.1f%% heat transfer): net annual %.0f MWh (cooling +%.0f, heating -%.0f)",
            r,
            fraction * 100,
            net_annual,
            monthly["cooling_mwh"].sum(),
            monthly["heating_mwh"].sum(),
        )

    # ── Visualization ──────────────────────────────────────────────────────
    fig, (ax_climate, ax_effect) = plt.subplots(
        2, 1, figsize=(12, 9), sharex=True,
        gridspec_kw={"height_ratios": [1, 1.5]},
    )

    months = np.arange(1, 13)
    month_labels = [calendar.month_abbr[m] for m in months]

    # --- Top panel: Climate drivers ---
    ghi_daily = climate["mean_ghi_kwh_m2_day"].values
    temps = climate["mean_temp_c"].values

    ax_climate.bar(months - 0.15, ghi_daily, 0.3, color="#F4A460", label="Daily GHI (kWh/m2/day)")
    ax_twin = ax_climate.twinx()
    ax_twin.plot(months, temps, "o-", color="#2E86C1", linewidth=2, markersize=6, label="Mean temp (C)")
    ax_twin.axhline(y=CDD_BASE_TEMP, color="#888888", linestyle="--", linewidth=1, alpha=0.7)
    ax_twin.annotate(
        f"{CDD_BASE_TEMP}C base", xy=(12, CDD_BASE_TEMP),
        fontsize=8, color="#888888", ha="right", va="bottom",
    )

    # Solstice markers
    ax_climate.axvline(x=6, color="#FFD700", linestyle=":", linewidth=1.5, alpha=0.8)
    ax_climate.axvline(x=12, color="#FF4500", linestyle=":", linewidth=1.5, alpha=0.8)

    ax_climate.set_ylabel("GHI (kWh/m2/day)", color="#F4A460")
    ax_twin.set_ylabel("Temperature (C)", color="#2E86C1")
    ax_climate.set_title(f"{suburb.name} — Monthly Climate Drivers", fontweight="bold")
    ax_climate.set_xticks(months)
    ax_climate.set_xticklabels(month_labels)

    # Combine legends
    lines1, labels1 = ax_climate.get_legend_handles_labels()
    lines2, labels2 = ax_twin.get_legend_handles_labels()
    ax_climate.legend(
        lines1 + lines2, labels1 + labels2,
        loc="upper left", fontsize=8,
    )

    # Winter / Summer season shading
    ax_climate.axvspan(0.5, 2.5, alpha=0.06, color="blue", label="_nolegend_")
    ax_climate.axvspan(5.5, 8.5, alpha=0.06, color="blue", label="_nolegend_")
    ax_climate.axvspan(11.5, 12.5, alpha=0.06, color="red", label="_nolegend_")

    # --- Bottom panel: Net electricity effect by R_roof ---
    colors = plt.cm.RdYlGn(np.linspace(0.15, 0.85, len(r_values)))

    bar_width = 0.8 / len(r_values)
    for i, r in enumerate(r_values):
        monthly = all_results[r]
        fraction = _heat_fraction(r)
        label = R_VALUE_LABELS.get(r, f"R{r}")
        offset = (i - len(r_values) / 2 + 0.5) * bar_width
        bars = ax_effect.bar(
            months + offset,
            monthly["net_mwh"].values,
            bar_width,
            color=colors[i],
            edgecolor="white",
            linewidth=0.3,
            label=f"{label} ({fraction*100:.1f}%)",
        )

    ax_effect.axhline(y=0, color="black", linewidth=0.8)
    ax_effect.set_ylabel("Net Electricity Effect (MWh)")
    ax_effect.set_title(
        f"{suburb.name} — Monthly Cool Roof Effect by Insulation Level",
        fontweight="bold",
    )
    ax_effect.set_xticks(months)
    ax_effect.set_xticklabels(month_labels)
    ax_effect.set_xlabel("Month")
    ax_effect.legend(loc="lower right", fontsize=7.5, ncol=2)

    # Annotate solstices on bottom panel
    ax_effect.annotate(
        "Winter\nSolstice\n(~Jun 21)",
        xy=(6, ax_effect.get_ylim()[1] * 0.85),
        fontsize=8, color="#FFD700", ha="center", fontstyle="italic",
    )
    ax_effect.annotate(
        "Summer\nSolstice\n(~Dec 21)",
        xy=(12, ax_effect.get_ylim()[1] * 0.85),
        fontsize=8, color="#FF4500", ha="center", fontstyle="italic",
    )

    # Net annual summary in a text box
    summary_lines = ["Net Annual Effect:"]
    for r in r_values:
        monthly = all_results[r]
        net = monthly["net_mwh"].sum()
        cool = monthly["cooling_mwh"].sum()
        heat = monthly["heating_mwh"].sum()
        label = R_VALUE_LABELS.get(r, f"R{r}")
        summary_lines.append(
            f"  {label}: {net:+.0f} MWh  (cool +{cool:.0f}, heat -{heat:.0f})"
        )

    summary_text = "\n".join(summary_lines)
    ax_effect.text(
        0.02, 0.98, summary_text,
        transform=ax_effect.transAxes,
        fontsize=7.5, fontfamily="monospace",
        verticalalignment="top",
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.85, "pad": 0.5},
    )

    # ── Save ───────────────────────────────────────────────────────────────
    out_path = OUTPUT_DIR / f"stage2_{suburb_key}_seasonal.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Seasonal analysis saved to: %s", out_path)

    return out_path


def list_suburbs_with_climate() -> list[str]:
    """List suburbs that have a climate parquet file."""
    from config.suburbs import list_suburbs

    available = []
    for name in list_suburbs():
        key = name.lower().replace(" ", "_")
        if (OUTPUT_DIR / f"stage2_{key}_climate.parquet").exists():
            available.append(name)
    return sorted(available)


def main():
    parser = argparse.ArgumentParser(
        description="Seasonal cool roof analysis with R_roof sensitivity"
    )
    parser.add_argument(
        "--suburb",
        type=str,
        help="Suburb to analyse (e.g. 'Clayton')",
    )
    parser.add_argument(
        "--r-values",
        type=str,
        default=None,
        help=f"Comma-separated R_roof values to sweep (default: {','.join(str(r) for r in DEFAULT_R_VALUES)})",
    )
    parser.add_argument(
        "--list-suburbs",
        action="store_true",
        help="List suburbs with climate data available and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.list_suburbs:
        available = list_suburbs_with_climate()
        if available:
            print("Suburbs with climate data (seasonal analysis ready):")
            for name in available:
                print(f"  - {name}")
        else:
            print("No suburbs have climate data yet.")
            print("Run Stage 2 first: python -m stage2_irradiance.run_stage2 --suburb <name>")
        sys.exit(0)

    if not args.suburb:
        parser.error("--suburb is required (or use --list-suburbs)")

    r_values = DEFAULT_R_VALUES
    if args.r_values:
        try:
            r_values = [float(x.strip()) for x in args.r_values.split(",")]
        except ValueError:
            parser.error(f"--r-values must be comma-separated numbers, got: {args.r_values}")

    logger.info("Starting seasonal analysis for %s...", args.suburb)
    result = run_seasonal_analysis(args.suburb, r_values)
    if result is None:
        logger.error("Seasonal analysis failed. Check logs above.")
        sys.exit(1)
    logger.info("Done. Output: %s", result)


if __name__ == "__main__":
    main()
