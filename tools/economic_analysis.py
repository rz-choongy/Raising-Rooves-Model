"""
economic_analysis.py — economic assessment of cool roof options, one row per
building per discount rate. All assumptions live in config/settings.py under
the "Economic Analysis" section (tariffs, gas prices, discount rates, product
costs). Authored by Angus (economics); see DECISION_LOG 2026-10-10.

    python -m tools.economic_analysis --input data/output/stage3_carlton_hourly.parquet
    python -m tools.economic_analysis --input sydney.parquet --zone Ausgrid
    python -m tools.economic_analysis --batch              # every file in config.settings.ECONOMIC_RUNS
    python -m tools.economic_analysis --input f.parquet --rebuild  # ignore the cached aggregate

Each run's results go to data/output/economic_analysis/<input file name>/, so
runs don't overwrite each other.

HOW IT WORKS
------------
1. AGGREGATE (DuckDB, streamed, cached). Each hourly row is classed as a
   heating or cooling hour, then summed to
       building x day-of-week x hour-of-day
   — 168 rows per building instead of ~8,760. Tariff and occupancy depend on
   time only through day and hour, so nothing is lost. The one indicative
   year is then repeated for every year of the analysis period.
2. PRICE. Cooling-hour energy at the hour's electricity rate. Heating-hour
   energy at the electricity rate, or converted to gas. Gives a year-1 dollar
   saving per building, split into an electricity part and a gas part (they
   escalate at different rates).
3. CASH FLOWS (nominal). Year 0: install. Years 1..N: saving x degradation x
   price escalation. Reapply at each warranty end at inflated cost. Residual
   credit for unused life at year N. Discount at the nominal rate.
4. OUTPUT. economic_analysis_results.xlsx — formatted workbook: Read me
           (definitions + every assumption), Summary, Results, Cash flow.
           economic_analysis_results.csv — one row per building per rate.
           economic_analysis_summary.csv — medians and % viable per scenario.
           economic_analysis_cashflow_example.csv — year-by-year nominal
           flows for one building, showing the inflation applied each year.

Input: an hourly Stage 3 export (building_id, time_melbourne,
roof_construction, roof_surface_area_m2, flux_base_wh_m2, flux_cool_wh_m2,
flux_coolmax_wh_m2, electricity_saved_cool_wh, electricity_saved_coolmax_wh,
optional electricity_zone). The data files don't record location, so the zone
must be supplied per file (--zone) or per building (an electricity_zone
column, which takes priority) — otherwise every building falls back to
config.settings.ECONOMIC_DEFAULT_ZONE and a non-default-zone file is priced on
the wrong tariffs with only a log warning.

RESULTS COLUMNS are "<scenario> – <metric>", e.g.
    "Coating, gas heating – NPV over 30 yrs ($)"
Scenarios: Coating / Coolmax, each with gas or electric heating. Savings are
positive, penalties and costs negative, so each net is the sum of its parts.
Full definitions are on the workbook's Read me sheet (DEFINITIONS below).
run() also returns the same results with short machine-friendly names
(<option>_<fuel>__<metric>) for anyone scripting against them.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from config import settings as cfg
from shared.file_io import ensure_dir
from shared.logging_config import setup_logging

logger = setup_logging("economic_analysis")

HOURS_PER_YEAR = 8760
MJ_PER_KWH = 3.6
OPTIONS = ("coating", "coolmax")
FUEL_TAG = {"gas": "gas", "electric": "elec"}

# Expected columns in the hourly Stage 3 export. See module docstring.
COL_BUILDING = "building_id"
COL_TIME = "time_melbourne"            # Melbourne LOCAL clock time (includes DST)
COL_ROOF = "roof_construction"
COL_AREA = "roof_surface_area_m2"      # actual roof surface area (not plan area)
COL_FLUX_BASE = "flux_base_wh_m2"      # heat flow through roof, existing roof
COL_FLUX_COATING = "flux_cool_wh_m2"   # ... with the roof-appropriate coating
COL_FLUX_COOLMAX = "flux_coolmax_wh_m2"  # ... with a Coolmax steel roof
COL_ELEC_COATING = "electricity_saved_cool_wh"      # + saving, - penalty (Wh)
COL_ELEC_COOLMAX = "electricity_saved_coolmax_wh"

# Optional per-building column naming the electricity distribution zone (a key
# of cfg.ECONOMIC_ELECTRICITY_ZONES). If absent, every building uses
# _DEFAULT_ZONE (set at the top of run()/run_batch() from --zone or
# cfg.ECONOMIC_DEFAULT_ZONE).
COL_ZONE = "electricity_zone"

# Which values are HEATING (priced as gas when the fuel is "gas"):
#   "sign"  (default) Positive = cooling saving, priced as electricity.
#           Negative = heating penalty, priced as gas.
#   "mode"  Alternative: classifies each hour by whether the heat pump was
#           heating or cooling (uses the flux columns). Moves ~1-3% of energy.
HEATING_RULE = "sign"

# Effective COP implied by the Stage 3 simulation: electricity_saved =
# (flux_base - flux_option) x area / (HVAC_COP_RESIDENTIAL / COOLING_FRACTION).
# Confirmed against Carlton Stage 3 output to 1e-11 Wh in every hour (Angus's
# economics audit, DECISION_LOG 2026-10-09).
SIMULATION_COP = cfg.HVAC_COP_RESIDENTIAL / cfg.COOLING_FRACTION

_DEFAULT_ZONE = cfg.ECONOMIC_DEFAULT_ZONE
_OUTPUT_DIR = cfg.OUTPUT_DIR / "economic_analysis"


def _fail(message: str) -> None:
    logger.error(message)
    sys.exit(1)


def _set_zone(zone: str) -> None:
    global _DEFAULT_ZONE
    if zone not in cfg.ECONOMIC_ELECTRICITY_ZONES:
        _fail(f"Unknown zone {zone!r}. Options: {', '.join(cfg.ECONOMIC_ELECTRICITY_ZONES)}")
    _DEFAULT_ZONE = zone


# =============================================================================
# 1. AGGREGATE
# =============================================================================

def _reader(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".parquet":
        return f"read_parquet('{path}')"
    if ext in (".csv", ".txt"):
        return f"read_csv_auto('{path}', header=true)"
    raise ValueError(f"Unsupported input type {ext!r} — use .csv or .parquet")


def _heating_case(elec: str, flux_opt: str) -> str:
    """SQL boolean: is this row a heating hour for this option?"""
    if HEATING_RULE == "mode":
        # Heating hour: electricity value has the opposite sign to the flux
        # reduction (less heat in => more heating energy needed).
        return f"(sign({elec}) * sign({COL_FLUX_BASE} - {flux_opt}) < 0)"
    if HEATING_RULE == "sign":
        return f"({elec} < 0)"
    raise ValueError(f"HEATING_RULE must be 'mode' or 'sign', got {HEATING_RULE!r}")


def aggregate(path: str, rebuild: bool = False) -> pd.DataFrame:
    """Stream the hourly file and sum kWh to building x weekday x hour."""
    if not os.path.exists(path):
        _fail(f"Input not found: {path}")

    st = os.stat(path)
    key = "|".join(map(str, [os.path.abspath(path), st.st_size, st.st_mtime,
                             HEATING_RULE, COL_ELEC_COATING,
                             COL_ELEC_COOLMAX, COL_ZONE, _DEFAULT_ZONE]))
    ensure_dir(_OUTPUT_DIR)
    cache = _OUTPUT_DIR / f".cache_{hashlib.md5(key.encode()).hexdigest()[:12]}.parquet"
    if cache.exists() and not rebuild:
        return pd.read_parquet(cache)

    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    src = _reader(path)
    cols = set(con.execute(f"SELECT * FROM {src} LIMIT 0").df().columns)

    needed = {COL_BUILDING, COL_TIME, COL_ROOF, COL_AREA,
              COL_ELEC_COATING, COL_ELEC_COOLMAX}
    if HEATING_RULE == "mode":
        needed |= {COL_FLUX_BASE, COL_FLUX_COATING, COL_FLUX_COOLMAX}
    missing = needed - cols
    if missing:
        _fail(f"Input is missing columns: {sorted(missing)}\nFound: {sorted(cols)}")

    zone_sql = f"any_value({COL_ZONE})" if COL_ZONE in cols else f"'{_DEFAULT_ZONE}'"
    t = f"CAST({COL_TIME} AS TIMESTAMP)"
    hc = _heating_case(COL_ELEC_COATING, COL_FLUX_COATING)
    hx = _heating_case(COL_ELEC_COOLMAX, COL_FLUX_COOLMAX)
    ec, ex = COL_ELEC_COATING, COL_ELEC_COOLMAX

    sql = f"""
        SELECT
            {COL_BUILDING}                           AS building_id,
            CAST(isodow({t}) - 1 AS TINYINT)         AS dow,      -- 0 = Monday
            CAST(hour({t}) AS TINYINT)               AS hr,
            any_value({COL_ROOF})                    AS roof,
            min({COL_AREA})                          AS area_min,
            max({COL_AREA})                          AS area_max,
            {zone_sql}                               AS zone,
            count(*)                                 AS n_hours,
            SUM(CASE WHEN {hc} THEN 0 ELSE {ec} END) / 1000.0 AS coating_cool_kwh,
            SUM(CASE WHEN {hc} THEN {ec} ELSE 0 END) / 1000.0 AS coating_heat_kwh,
            SUM(CASE WHEN {hx} THEN 0 ELSE {ex} END) / 1000.0 AS coolmax_cool_kwh,
            SUM(CASE WHEN {hx} THEN {ex} ELSE 0 END) / 1000.0 AS coolmax_heat_kwh
        FROM {src}
        GROUP BY 1, 2, 3
    """
    agg = con.execute(sql).df()
    con.close()
    agg.to_parquet(cache, index=False)
    return agg


# =============================================================================
# 2. PRICE — year-1 dollars per building
# =============================================================================

def _hour_rates(zone: str) -> np.ndarray:
    """24 electricity rates ($/kWh), one per local clock hour."""
    if zone not in cfg.ECONOMIC_ELECTRICITY_ZONES:
        _fail(f"Unknown electricity zone {zone!r}. Options: {sorted(cfg.ECONOMIC_ELECTRICITY_ZONES)}")
    z = cfg.ECONOMIC_ELECTRICITY_ZONES[zone]
    if cfg.ECONOMIC_TARIFF_STRUCTURE == "flat":
        return np.full(24, z["flat"])
    if cfg.ECONOMIC_TARIFF_STRUCTURE != "tou":
        _fail(f"ECONOMIC_TARIFF_STRUCTURE must be 'tou' or 'flat', got {cfg.ECONOMIC_TARIFF_STRUCTURE!r}")
    rates = np.full(24, np.nan)
    for _, windows, rate in z["tou"]:
        for s, e in windows:
            hours = range(s, e) if s < e else [*range(s, 24), *range(0, e)]
            rates[list(hours)] = rate
    if np.isnan(rates).any():
        _fail(f"{zone}: ToU windows leave hours {np.where(np.isnan(rates))[0].tolist()} uncovered")
    return rates


_WARNED_NO_GAS_STATES: set[str] = set()


def _gas_dollars_per_heat_kwh(state: str) -> float:
    """$ per kWh of SIMULATED heating electricity, if that heat came from gas.

        elec kWh x COP           = heat delivered (kWh)
        heat / efficiency x 3.6  = MJ of gas burnt
        MJ x c/MJ / 100          = dollars

    NaN for states with no reticulated mains gas network (WA, NT) rather than
    a fabricated rate — the gas-heating scenario genuinely doesn't exist
    there, it isn't a missing-data gap.
    """
    if state not in cfg.ECONOMIC_GAS_RATE_C_PER_MJ:
        if state not in _WARNED_NO_GAS_STATES:
            logger.warning(
                "No reticulated gas network modelled for state %r — "
                "*_heat_gas_aud will be NaN for these buildings.", state)
            _WARNED_NO_GAS_STATES.add(state)
        return float("nan")
    mj = SIMULATION_COP / cfg.ECONOMIC_GAS_HEATER_EFFICIENCY * MJ_PER_KWH
    return mj * cfg.ECONOMIC_GAS_RATE_C_PER_MJ[state] / 100.0


def price(agg: pd.DataFrame) -> pd.DataFrame:
    """Annual kWh and year-1 dollars per building, by option and fuel."""
    df = agg.copy()

    occ = 1.0 - cfg.ECONOMIC_OCCUPANCY_DIAL * (1.0 - np.asarray(cfg.ECONOMIC_OCCUPANCY_PROFILE, float))
    if occ.shape != (7, 24):
        _fail("ECONOMIC_OCCUPANCY_PROFILE must be 7 rows (Mon-Sun) x 24 hours")
    f = occ[df["dow"].to_numpy(), df["hr"].to_numpy()]
    occ_c = f if cfg.ECONOMIC_OCCUPANCY_APPLIES_TO_COOLING else 1.0
    occ_h = f if cfg.ECONOMIC_OCCUPANCY_APPLIES_TO_HEATING else 1.0

    rate = np.empty(len(df))
    gas = np.empty(len(df))
    for zone, idx in df.groupby("zone").indices.items():
        rate[idx] = _hour_rates(zone)[df["hr"].to_numpy()[idx]]
        gas[idx] = _gas_dollars_per_heat_kwh(cfg.ECONOMIC_ELECTRICITY_ZONES[zone]["state"])

    for opt in OPTIONS:
        cool = df[f"{opt}_cool_kwh"] * occ_c
        heat = df[f"{opt}_heat_kwh"] * occ_h            # negative = penalty
        df[f"{opt}_cool_kwh"] = cool
        df[f"{opt}_heat_kwh"] = heat
        df[f"{opt}_cool_aud"] = cool * rate                # always electricity
        df[f"{opt}_heat_elec_aud"] = heat * rate           # heating as electricity
        df[f"{opt}_heat_gas_aud"] = heat * gas              # heating as gas

    sums = [c for c in df.columns if c.endswith(("_kwh", "_aud"))]
    b = df.groupby("building_id").agg(
        roof=("roof", "first"), area_min=("area_min", "min"),
        area_max=("area_max", "max"), zone=("zone", "first"),
        n_hours=("n_hours", "sum"), **{c: (c, "sum") for c in sums})

    bad = b[(b.area_max - b.area_min).abs() > 1e-6]
    if len(bad):
        logger.warning("%d buildings have more than one roof area; using the max.", len(bad))
    b["area"] = b["area_max"]

    # pandas' default sum() treats an all-NaN group as 0, not NaN, so an
    # hour-by-hour NaN gas rate would silently become a free $0 heating bill
    # after the groupby above. Re-null the gas columns explicitly for states
    # with no reticulated gas network (see _gas_dollars_per_heat_kwh).
    no_gas = b["zone"].map(
        lambda z: cfg.ECONOMIC_ELECTRICITY_ZONES[z]["state"] not in cfg.ECONOMIC_GAS_RATE_C_PER_MJ)
    gas_cols = [c for c in sums if c.endswith("_heat_gas_aud")]
    b.loc[no_gas, gas_cols] = np.nan

    short = b[b.n_hours < 0.95 * HOURS_PER_YEAR]
    if len(short):
        logger.warning("%d buildings have <95%% of a year of hours (min %d); annualising anyway.",
                       len(short), int(short.n_hours.min()))

    scale = HOURS_PER_YEAR / b["n_hours"]                  # gross up to 8,760 h
    for c in sums:
        b[c] = b[c] * scale

    b["coating_product"] = b["roof"].str.lower().map(cfg.ECONOMIC_COATING_FOR_ROOF)
    unknown = b.loc[b.coating_product.isna(), "roof"].unique()
    if len(unknown):
        _fail(f"No coating mapped for roof type(s) {list(unknown)}. "
              f"Add them to ECONOMIC_COATING_FOR_ROOF in config/settings.py.")
    return b.drop(columns=["area_min", "area_max"]).reset_index()


# =============================================================================
# 3. CASH FLOWS
# =============================================================================

def _escalator(real_growth: float) -> float:
    return (1.0 + cfg.ECONOMIC_INFLATION) * (1.0 + real_growth)


def _vectors(product: dict, rate_real: float, years: int, reapply: bool):
    """Per-unit multiplier arrays shared by every building on this product.

    Returns (disc, idx_elec, idx_gas, cost) — each length years+1, where
        flows = S_elec * idx_elec + S_gas * idx_gas + C0 * cost
    S_* = year-1 saving at today's prices, C0 = today's install cost.
    """
    r_nom = (1.0 + rate_real) * (1.0 + cfg.ECONOMIC_INFLATION) - 1.0
    t = np.arange(years + 1)
    disc = (1.0 + r_nom) ** -t.astype(float)

    life = int(product["warranty_years"])
    apps = list(range(0, years, life)) if reapply else [0]
    floor, tau = product["degradation_floor"], product["degradation_tau"]

    activity = np.zeros(years + 1)                # degradation x active flag
    for yr in range(1, years + 1):
        last = max(a for a in apps if a < yr)
        age = yr - last                            # 1 = first year of a coat
        if not reapply and age > life:
            continue                               # single coat has expired
        activity[yr] = (floor + (1 - floor) * np.exp(-(age - 1) / tau)
                        if cfg.ECONOMIC_APPLY_DEGRADATION else 1.0)

    # Every cash flow at year t uses the price index for year t — energy,
    # reapplications and residual alike. Using the same index as the discount
    # rate is what makes inflation cancel exactly out of the NPV.
    esc_e = _escalator(cfg.ECONOMIC_REAL_GROWTH_ELECTRICITY) ** t
    esc_g = _escalator(cfg.ECONOMIC_REAL_GROWTH_GAS) ** t
    idx_e, idx_g = activity * esc_e, activity * esc_g

    # Costs: install at year 0 at today's price; reapplications inflated.
    esc_c = _escalator(cfg.ECONOMIC_REAL_GROWTH_INSTALL_COST)
    cost = np.zeros(years + 1)
    for a in apps:
        cost[a] -= esc_c ** a
    if cfg.ECONOMIC_RESIDUAL_VALUE:
        remaining = life - (years - apps[-1])
        if remaining > 0:                          # unused life at the boundary
            cost[years] += esc_c ** years * remaining / life
    return disc, idx_e, idx_g, cost


def _payback(flows: np.ndarray) -> np.ndarray:
    """Years until the cumulative flow reaches zero AND STAYS there. NaN = never.

    Pass flows WITHOUT the residual credit: payback measures when savings
    recover the outlay, and a period-end accounting credit shouldn't tip it.
    Using the last crossing (not the first) means a coating that briefly
    breaks even, then falls behind again at its next reapplication, is not
    reported as paid back.
    """
    cum = np.cumsum(flows, axis=1)
    out = np.full(len(flows), np.nan)
    for i in range(len(flows)):
        if cum[i, -1] < 0:
            continue                                  # behind at the end
        neg = np.flatnonzero(cum[i] < 0)
        if len(neg) == 0:
            out[i] = 0.0                              # never behind
            continue
        k = neg[-1]                                   # last year still behind
        out[i] = k + (-cum[i, k]) / flows[i, k + 1]   # interpolate into k+1
    return out


def evaluate(b: pd.DataFrame, rate_real: float) -> pd.DataFrame:
    """Every metric for every building at one discount rate."""
    out = pd.DataFrame(index=b.index)
    years = int(cfg.ECONOMIC_ANALYSIS_PERIOD_YEARS)

    for opt in OPTIONS:
        products = (b["coating_product"] if opt == "coating"
                    else pd.Series("coolmax", index=b.index))
        for fuel in cfg.ECONOMIC_HEATING_FUELS:
            tag = f"{opt}_{FUEL_TAG[fuel]}"
            s_elec = b[f"{opt}_cool_aud"].copy()
            s_gas = pd.Series(0.0, index=b.index)
            if fuel == "gas":
                s_gas = b[f"{opt}_heat_gas_aud"]
            else:
                s_elec = s_elec + b[f"{opt}_heat_elec_aud"]

            cols = {m: np.full(len(b), np.nan) for m in [
                "npv_aud", "npv_first_application_aud", "discounted_payback_yrs",
                "simple_payback_yrs", "break_even_cost_aud_per_m2"]}

            for key, idx in products.groupby(products).indices.items():
                p = cfg.ECONOMIC_PRODUCTS[key]
                se = s_elec.to_numpy()[idx][:, None]
                sg = s_gas.to_numpy()[idx][:, None]
                area = b["area"].to_numpy()[idx]
                c0 = (p["install_cost_aud_per_m2"] * area)[:, None]

                # Full analysis period, with reapplications and residual.
                disc, ie, ig, cost = _vectors(p, rate_real, years,
                                              cfg.ECONOMIC_REAPPLY_AT_WARRANTY_END)
                benefit = se * ie + sg * ig
                flows = benefit + c0 * cost
                cols["npv_aud"][idx] = flows @ disc
                # Payback ignores the residual credit (see _payback).
                no_residual = benefit + c0 * np.minimum(cost, 0)
                cols["discounted_payback_yrs"][idx] = _payback(no_residual * disc)
                cols["simple_payback_yrs"][idx] = _payback(no_residual)
                pv_benefit = benefit @ disc
                pv_cost_per_m2 = -(cost @ disc) * area
                # No install cost makes it viable if the benefit itself is <= 0.
                cols["break_even_cost_aud_per_m2"][idx] = np.where(
                    pv_benefit > 0, pv_benefit / pv_cost_per_m2, np.nan)

                # First application only, ending just before the first recoat.
                life = int(p["warranty_years"])
                d1, ie1, ig1, cost1 = _vectors(p, rate_real, life, False)
                f1 = se * ie1 + sg * ig1 + c0 * cost1 * (np.arange(life + 1) == 0)
                cols["npv_first_application_aud"][idx] = f1 @ d1

            heat_col = f"{opt}_heat_gas_aud" if fuel == "gas" else f"{opt}_heat_elec_aud"
            out[f"{tag}__year1_cooling_aud"] = b[f"{opt}_cool_aud"]
            out[f"{tag}__year1_heating_aud"] = b[heat_col]
            out[f"{tag}__year1_saving_aud"] = s_elec + s_gas
            install = products.map(lambda k: cfg.ECONOMIC_PRODUCTS[k]["install_cost_aud_per_m2"])
            out[f"{tag}__install_cost_aud"] = install * b["area"]
            for m, v in cols.items():
                out[f"{tag}__{m}"] = v
    return out


def cashflow_example(b: pd.DataFrame, building_id=None) -> pd.DataFrame:
    """Year-by-year nominal cash flows for one building — the audit trail.

    Shows the price index applied each year, so the inflation is visible, and
    the present value of each year at every discount rate.
    """
    if building_id is None:
        row = b.iloc[0]
    else:
        match = b[b.building_id == building_id]
        if match.empty:
            _fail(f"ECONOMIC_EXAMPLE_BUILDING {building_id} not found in the input.")
        row = match.iloc[0]
    years = int(cfg.ECONOMIC_ANALYSIS_PERIOD_YEARS)
    t = np.arange(years + 1)
    out = []
    for opt, fuel, name in _scenarios():
        key = row["coating_product"] if opt == "coating" else "coolmax"
        p = cfg.ECONOMIC_PRODUCTS[key]
        c0 = p["install_cost_aud_per_m2"] * row["area"]
        _, ie, ig, cost = _vectors(p, cfg.ECONOMIC_DISCOUNT_RATES_REAL[0], years,
                                   cfg.ECONOMIC_REAPPLY_AT_WARRANTY_END)
        cooling = row[f"{opt}_cool_aud"] * ie                 # electricity index
        heating = (row[f"{opt}_heat_gas_aud"] * ig if fuel == "gas"
                   else row[f"{opt}_heat_elec_aud"] * ie)
        capital = c0 * np.minimum(cost, 0)
        residual = c0 * np.maximum(cost, 0)
        net = cooling + heating + capital + residual
        df = pd.DataFrame({
            "Scenario": f"{name} ({p['label']})",
            "Year": t,
            "Electricity price index": _escalator(cfg.ECONOMIC_REAL_GROWTH_ELECTRICITY) ** t,
            "Gas price index": _escalator(cfg.ECONOMIC_REAL_GROWTH_GAS) ** t,
            "Cooling saving ($)": cooling,
            "Heating penalty ($)": heating,
            "Install / reapplication ($)": capital,
            "Residual value ($)": residual,
            "Net cash flow ($)": net,
            "Cumulative ($)": np.cumsum(net),
        })
        for r in cfg.ECONOMIC_DISCOUNT_RATES_REAL:
            r_nom = (1 + r) * (1 + cfg.ECONOMIC_INFLATION) - 1
            df[f"Present value @ {r:.0%} real ($)"] = net / (1 + r_nom) ** t
        out.append(df)
    return pd.concat(out, ignore_index=True)


# =============================================================================
# 4. PRESENTATION — plain-English tables and the Excel workbook
# =============================================================================
# Sign convention everywhere: savings positive, penalties and costs negative,
# so every net figure is simply the sum of the parts.

def _scenarios():
    """(option, fuel tag, display name) for every scenario, in output order."""
    names = {"coating": "Coating", "coolmax": "Coolmax"}
    fuels = {"gas": "gas heating", "electric": "electric heating"}
    return [(opt, FUEL_TAG[f], f"{names[opt]}, {fuels[f]}")
            for opt in OPTIONS for f in cfg.ECONOMIC_HEATING_FUELS]


def _metrics():
    """(internal metric, display header, kind) — kind drives Excel formatting."""
    n = int(cfg.ECONOMIC_ANALYSIS_PERIOD_YEARS)
    return [
        ("year1_cooling_aud",          "Year-1 cooling saving ($)",       "money"),
        ("year1_heating_aud",          "Year-1 heating penalty ($)",      "money"),
        ("year1_saving_aud",           "Year-1 net saving ($)",           "money"),
        ("install_cost_aud",           "Install cost ($)",                "money"),
        ("npv_aud",                    f"NPV over {n} yrs ($)",           "money"),
        ("npv_first_application_aud",  "NPV, first application only ($)", "money"),
        ("discounted_payback_yrs",     "Discounted payback (yrs)",        "years"),
        ("simple_payback_yrs",         "Simple payback (yrs)",            "years"),
        ("break_even_cost_aud_per_m2", "Break-even install cost ($/m²)",  "rate"),
    ]


BASE_COLUMNS = [  # (internal, display, kind)
    ("building_id",            "Building ID",                          "text"),
    ("roof_construction",      "Roof type",                            "text"),
    ("roof_surface_area_m2",   "Roof area (m²)",                       "area"),
    ("electricity_zone",       "Electricity zone",                     "text"),
    ("coating_product",        "Coating product",                      "text"),
    ("discount_rate_real",     "Discount rate (real)",                 "pct"),
    ("discount_rate_nominal",  "Discount rate (nominal)",              "pct"),
    ("coating_cooling_kwh_yr", "Coating energy – Cooling energy saved (kWh/yr)", "kwh"),
    ("coating_heating_kwh_yr", "Coating energy – Heating energy added (kWh/yr)", "kwh"),
    ("coolmax_cooling_kwh_yr", "Coolmax energy – Cooling energy saved (kWh/yr)", "kwh"),
    ("coolmax_heating_kwh_yr", "Coolmax energy – Heating energy added (kWh/yr)", "kwh"),
]


def results_table(res: pd.DataFrame):
    """Display version of the results: plain headers, N/A where undefined.

    Returns (table, kinds, bands) where bands = [(scenario name, first col,
    last col)] for the Excel group headers.
    """
    tbl, kinds, bands = pd.DataFrame(), [], []
    for key, label, kind in BASE_COLUMNS:
        col = res[key]
        if key == "coating_product":
            col = col.map(lambda k: cfg.ECONOMIC_PRODUCTS[k]["label"])
        tbl[label] = col
        kinds.append(kind)
    for band in ("Coating energy", "Coolmax energy"):     # bands over the kWh pairs
        idx = [i for i, c in enumerate(tbl.columns) if c.startswith(band + " – ")]
        bands.append((band, idx[0], idx[-1]))
    for opt, fuel, name in _scenarios():
        first = len(tbl.columns)
        for metric, label, kind in _metrics():
            tbl[f"{name} – {label}"] = res[f"{opt}_{fuel}__{metric}"]
            kinds.append(kind)
        bands.append((name, first, len(tbl.columns) - 1))
    return tbl, kinds, bands


def summary_table(res: pd.DataFrame) -> pd.DataFrame:
    """One row per discount rate per scenario."""
    rows = []
    for r, g in res.groupby("discount_rate_real"):
        for opt, fuel, name in _scenarios():
            c = lambda m: g[f"{opt}_{fuel}__{m}"]
            be = c("break_even_cost_aud_per_m2")
            rows.append({
                "Discount rate (real)": r,
                "Scenario": name,
                "Buildings": len(g),
                "Median year-1 cooling saving ($)": c("year1_cooling_aud").median(),
                "Median year-1 heating penalty ($)": c("year1_heating_aud").median(),
                "Median year-1 net saving ($)": c("year1_saving_aud").median(),
                "Median install cost ($)": c("install_cost_aud").median(),
                f"Median NPV over {cfg.ECONOMIC_ANALYSIS_PERIOD_YEARS} yrs ($)": c("npv_aud").median(),
                "Total NPV, all buildings ($)": c("npv_aud").sum(),
                "Buildings with positive NPV (%)": 100 * (c("npv_aud") > 0).mean(),
                "Buildings paying back within first application (%)":
                    100 * (c("npv_first_application_aud") > 0).mean(),
                "Buildings with no break-even cost (%)": 100 * be.isna().mean(),
                "Median break-even install cost ($/m², where one exists)": be.median(),
            })
    return pd.DataFrame(rows)


def assumptions_table() -> pd.DataFrame:
    """Every assumption behind the run, read live from config/settings.py."""
    rows = [
        ("Analysis period", f"{cfg.ECONOMIC_ANALYSIS_PERIOD_YEARS} years (one indicative year repeated)"),
        ("Discount rates (real)", ", ".join(f"{r:.0%}" for r in cfg.ECONOMIC_DISCOUNT_RATES_REAL)
            + "  — Office of Impact Analysis convention"),
        ("Inflation", f"{cfg.ECONOMIC_INFLATION:.1%} a year, applied to every price"),
        ("Real price growth", f"electricity {cfg.ECONOMIC_REAL_GROWTH_ELECTRICITY:+.1%}, gas "
            f"{cfg.ECONOMIC_REAL_GROWTH_GAS:+.1%}, install cost {cfg.ECONOMIC_REAL_GROWTH_INSTALL_COST:+.1%} a year"),
        ("Electricity tariff", f"{cfg.ECONOMIC_TARIFF_STRUCTURE.upper()}, zone from data or default "
            f"{_DEFAULT_ZONE}"),
        ("Gas price", ", ".join(f"{k} {v} c/MJ" for k, v in cfg.ECONOMIC_GAS_RATE_C_PER_MJ.items())),
        ("Heating attribution", "Positive values = cooling saving (electricity); negative "
            "values = heating penalty" if HEATING_RULE == "sign"
            else "By heat-pump mode, from the flux columns"),
        ("Gas conversion", f"penalty kWh x COP {SIMULATION_COP:.4f} = heat lost; "
            f"/ heater efficiency {cfg.ECONOMIC_GAS_HEATER_EFFICIENCY:.0%} = gas; x 3.6 = MJ"),
        ("Reapplication", "At the end of each warranty, at the inflated cost"
            if cfg.ECONOMIC_REAPPLY_AT_WARRANTY_END else "None — single application"),
        ("Residual value", "Unused life at the end of the period credited at replacement cost"
            if cfg.ECONOMIC_RESIDUAL_VALUE else "None"),
        ("Reflectance degradation", "On" if cfg.ECONOMIC_APPLY_DEGRADATION else "Off"),
        ("Occupancy adjustment", "Off" if cfg.ECONOMIC_OCCUPANCY_DIAL == 0
            else f"Dial {cfg.ECONOMIC_OCCUPANCY_DIAL}"),
    ]
    for key, p in cfg.ECONOMIC_PRODUCTS.items():
        rows.append((p["label"], f"${p['install_cost_aud_per_m2']:.2f}/m², "
                     f"warranty / reapply every {p['warranty_years']} yrs"))
    return pd.DataFrame(rows, columns=["Assumption", "Value"])


DEFINITIONS = [
    ("Sign convention", "Savings are positive; penalties and costs are negative. "
        "Every net figure is the sum of its parts."),
    ("Year-1 cooling saving", "Value of electricity saved in hours the roof reduces "
        "heat gain, at today's prices."),
    ("Year-1 heating penalty", "Cost of extra heating in hours the roof increases heat "
        "loss — priced as gas or electricity depending on the scenario."),
    ("Install cost", "Today's cost of the first application on this roof."),
    ("NPV", "Present value of all cash flows over the analysis period, including "
        "reapplications and residual value."),
    ("NPV, first application only", "Value at the end of the first warranty, i.e. just "
        "before the first reapplication. For Coolmax, over its full 36-year life."),
    ("Discounted / simple payback", "Years until the cumulative (discounted / "
        "undiscounted) cash flow reaches zero and stays there. Excludes the residual "
        "credit, so it measures when savings alone recover the outlay. N/A = never "
        "within the period."),
    ("Break-even install cost", "The install cost per m² at which NPV would be exactly "
        "zero. N/A = the energy value is itself negative, so no price could pay back."),
    ("Discount rate (nominal)", "The real rate grossed up for inflation. Nominal cash "
        "flows at the nominal rate give the same NPV as real flows at the real rate."),
]


def write_workbook(path: Path, results: pd.DataFrame, kinds, bands,
                   summary: pd.DataFrame, cashflow: pd.DataFrame,
                   example: pd.Series) -> bool:
    """Formatted Excel workbook. Returns False if xlsxwriter isn't installed."""
    try:
        import xlsxwriter  # noqa: F401
    except ImportError:
        logger.info("xlsxwriter not installed — skipping the formatted Excel workbook "
                    "(pip install xlsxwriter to get it).")
        return False

    with pd.ExcelWriter(path, engine="xlsxwriter") as xw:
        wb = xw.book
        f = {
            "title":  wb.add_format({"bold": True, "font_size": 14}),
            "bold":   wb.add_format({"bold": True}),
            "wrap":   wb.add_format({"text_wrap": True, "valign": "top"}),
            "head":   wb.add_format({"bold": True, "text_wrap": True, "valign": "bottom",
                                     "bg_color": "#DDE4EE", "border": 1}),
            "band":   wb.add_format({"bold": True, "align": "center", "border": 1,
                                     "bg_color": "#B7C6DB"}),
            "money":  wb.add_format({"num_format": "#,##0;[Red]-#,##0"}),
            "rate":   wb.add_format({"num_format": "0.00", "align": "right"}),
            "years":  wb.add_format({"num_format": "0.0", "align": "right"}),
            "pct":    wb.add_format({"num_format": "0.0%"}),
            "pct100": wb.add_format({"num_format": "0.0"}),
            "kwh":    wb.add_format({"num_format": "#,##0;[Red]-#,##0"}),
            "area":   wb.add_format({"num_format": "#,##0.0"}),
            "index":  wb.add_format({"num_format": "0.000"}),
            "text":   None,
            "total":  wb.add_format({"bold": True, "top": 1, "num_format": "#,##0;[Red]-#,##0"}),
        }

        # ---- Read me ---------------------------------------------------------
        ws = wb.add_worksheet("Read me")
        ws.set_column(0, 0, 30); ws.set_column(1, 1, 95)
        ws.write(0, 0, "Cool roof economic assessment", f["title"])
        ws.write(1, 0, "Each building appears once per discount rate. Four scenarios: "
                       "the roof-appropriate coating or a Coolmax re-roof, with the winter "
                       "heating penalty priced as gas or as electricity.", f["wrap"])
        r = 3
        ws.write(r, 0, "Definitions", f["bold"]); r += 1
        for k, v in DEFINITIONS:
            ws.write(r, 0, k, f["bold"]); ws.write(r, 1, v, f["wrap"]); r += 1
        r += 1
        ws.write(r, 0, "Assumptions in this run", f["bold"]); r += 1
        for k, v in assumptions_table().itertuples(index=False):
            ws.write(r, 0, k); ws.write(r, 1, v, f["wrap"]); r += 1

        def na(df):
            """Undefined cells (paybacks, break-evens, no-gas-network dollars)
            read 'N/A', not blank — and xlsxwriter can't write a bare NaN."""
            d = df.copy()
            for c in d.columns:
                if d[c].isna().any():
                    d[c] = d[c].astype(object).where(d[c].notna(), "N/A")
            return d
        summary, results = na(summary), na(results)

        # ---- Summary ---------------------------------------------------------
        summary.to_excel(xw, sheet_name="Summary", index=False, startrow=1, header=False)
        ws = xw.sheets["Summary"]
        for c, name in enumerate(summary.columns):
            ws.write(0, c, name, f["head"])
            kind = ("pct" if "rate" in name.lower() else "pct100" if "(%)" in name
                    else "rate" if "$/m²" in name else "text" if c in (1, 2) else "money")
            ws.set_column(c, c, 13 if c != 1 else 26, f[kind])
        ws.set_row(0, 60); ws.freeze_panes(1, 2)

        # ---- Results: scenario bands over metric headers ---------------------
        results.to_excel(xw, sheet_name="Results", index=False, startrow=2, header=False)
        ws = xw.sheets["Results"]
        for c, name in enumerate(results.columns):
            short = name.split(" – ", 1)[-1]
            ws.write(1, c, short, f["head"])
            width = {0: 12, 1: 11, 4: 38}.get(c, 13)
            ws.set_column(c, c, width, f[kinds[c]])
        for name, c0, c1 in bands:
            ws.merge_range(0, c0, 0, c1, name, f["band"])
        ws.set_row(1, 62); ws.freeze_panes(2, 2)
        ws.autofilter(1, 0, len(results) + 1, len(results.columns) - 1)

        # ---- Cash flow example: one titled block per scenario ----------------
        ws = wb.add_worksheet("Cash flow example")
        cols = [c for c in cashflow.columns if c != "Scenario"]
        ws.set_column(0, 0, 8); ws.set_column(1, len(cols) - 1, 14)
        ws.write(0, 0, f"Building {example['building_id']} — {example['roof']}, "
                       f"{example['area']:,.1f} m². Nominal dollars; present values at each "
                       f"real rate sum to the NPV in the Results sheet.", f["bold"])
        r = 2
        for scen, g in cashflow.groupby("Scenario", sort=False):
            ws.write(r, 0, scen, f["bold"]); r += 1
            for c, name in enumerate(cols):
                ws.write(r, c, name, f["head"])
            ws.set_row(r, 32); r += 1
            for vals in g[cols].itertuples(index=False):
                for c, v in enumerate(vals):
                    kind = "index" if "index" in cols[c] else "text" if c == 0 else "money"
                    if isinstance(v, float) and np.isnan(v):
                        ws.write(r, c, "N/A", f["text"])
                    else:
                        ws.write(r, c, v, f[kind])
                r += 1
            ws.write(r, 0, "Total", f["bold"])
            for c, name in enumerate(cols):
                if name.endswith("($)") and name != "Cumulative ($)":
                    total = g[name].sum() if g[name].notna().any() else np.nan
                    if np.isnan(total):
                        ws.write(r, c, "N/A", f["text"])
                    else:
                        ws.write(r, c, total, f["total"])
            r += 3
    return True


# =============================================================================
# 5. RUN
# =============================================================================

def run(path: str, rebuild: bool = False) -> pd.DataFrame:
    t0 = time.time()
    agg = aggregate(path, rebuild)
    b = price(agg)
    logger.info("Loaded %s buildings in %.1fs", f"{len(b):,}", time.time() - t0)

    unverified = sorted({z for z in b.zone if not cfg.ECONOMIC_ELECTRICITY_ZONES[z]["verified"]})
    if unverified:
        logger.warning("ToU rates for %s are approximate placeholders.", unverified)

    base = pd.DataFrame({
        "building_id": b["building_id"],
        "roof_construction": b["roof"],
        "roof_surface_area_m2": b["area"],
        "electricity_zone": b["zone"],
        "coating_product": b["coating_product"],
        "coating_cooling_kwh_yr": b["coating_cool_kwh"],
        "coating_heating_kwh_yr": b["coating_heat_kwh"],
        "coolmax_cooling_kwh_yr": b["coolmax_cool_kwh"],
        "coolmax_heating_kwh_yr": b["coolmax_heat_kwh"],
    })
    frames = []
    for r in cfg.ECONOMIC_DISCOUNT_RATES_REAL:
        head = base.copy()
        head.insert(5, "discount_rate_real", r)
        head.insert(6, "discount_rate_nominal", round((1 + r) * (1 + cfg.ECONOMIC_INFLATION) - 1, 6))
        frames.append(pd.concat([head, evaluate(b, r)], axis=1))
    res = (pd.concat(frames, ignore_index=True)
             .sort_values(["building_id", "discount_rate_real"], kind="stable")
             .reset_index(drop=True))

    results, kinds, bands = results_table(res)
    summary = summary_table(res)
    example = (b.iloc[0] if cfg.ECONOMIC_EXAMPLE_BUILDING is None
               else b[b.building_id == cfg.ECONOMIC_EXAMPLE_BUILDING].iloc[0])
    cashflow = cashflow_example(b, cfg.ECONOMIC_EXAMPLE_BUILDING)

    ensure_dir(_OUTPUT_DIR)
    out = lambda name: _OUTPUT_DIR / name

    def to_csv(df, name):
        d = df.copy()
        for c in d.columns:
            if d[c].dtype.kind == "f":
                d[c] = d[c].round(4 if "rate (" in c else 2)
                if "payback" in c or "break-even" in c.lower():
                    d[c] = d[c].astype(object).where(d[c].notna(), "N/A")
        d.to_csv(out(name), index=False)

    to_csv(results, "economic_analysis_results.csv")
    to_csv(summary, "economic_analysis_summary.csv")
    to_csv(cashflow, "economic_analysis_cashflow_example.csv")
    xlsx = write_workbook(out("economic_analysis_results.xlsx"), results, kinds, bands,
                          summary, cashflow, example)

    pd.set_option("display.width", 200)
    show = summary[["Discount rate (real)", "Scenario", "Median year-1 net saving ($)",
                    f"Median NPV over {cfg.ECONOMIC_ANALYSIS_PERIOD_YEARS} yrs ($)",
                    "Buildings with positive NPV (%)"]]
    print("\n" + show.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print(f"\n{len(res):,} rows ({len(b):,} buildings x {len(cfg.ECONOMIC_DISCOUNT_RATES_REAL)} rates) -> "
          f"{_OUTPUT_DIR}/  [3 CSVs{' + Excel workbook' if xlsx else ''}]  "
          f"[{time.time() - t0:.1f}s]")
    return res


def run_batch() -> pd.DataFrame:
    """Run every (file, zone) pair in cfg.ECONOMIC_RUNS; one results folder each,
    plus a combined summary across all areas."""
    global _OUTPUT_DIR
    if not cfg.ECONOMIC_RUNS:
        _fail("No runs defined in config.settings.ECONOMIC_RUNS.")
    parent, frames = cfg.OUTPUT_DIR / "economic_analysis", []
    for name, (path, zone) in cfg.ECONOMIC_RUNS.items():
        logger.info("%s: %s  [zone %s]", name, path, zone)
        _set_zone(zone)
        _OUTPUT_DIR = parent / name
        run(path)
        summ = pd.read_csv(_OUTPUT_DIR / "economic_analysis_summary.csv")
        summ.insert(0, "Area", name)
        summ.insert(1, "Electricity zone", zone)
        frames.append(summ)
    _OUTPUT_DIR = parent
    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(parent / "all_areas_summary.csv", index=False)
    logger.info("Combined summary of %d areas -> %s/all_areas_summary.csv", len(cfg.ECONOMIC_RUNS), parent)
    return combined


def main(argv: list[str] | None = None) -> None:
    global _OUTPUT_DIR

    ap = argparse.ArgumentParser(
        description="Cool roof economic assessment: NPV/payback per building per discount rate"
    )
    ap.add_argument("--input", help="hourly Stage 3 export, CSV or parquet")
    ap.add_argument("--zone", help="electricity zone for every building in this file, "
                    f"e.g. Ausgrid (options: {', '.join(cfg.ECONOMIC_ELECTRICITY_ZONES)})")
    ap.add_argument("--output", help="results folder (default: data/output/economic_analysis/<input file name>)")
    ap.add_argument("--batch", action="store_true", help="run every file listed in config.settings.ECONOMIC_RUNS")
    ap.add_argument("--rebuild", action="store_true", help="ignore cached aggregate")
    ap.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    args = ap.parse_args(argv)

    if args.debug:
        logger.setLevel("DEBUG")

    if args.batch:
        run_batch()
        return

    if not args.input:
        ap.error("--input is required unless --batch is given")
    if args.zone:
        _set_zone(args.zone)
    stem = Path(args.input).stem
    _OUTPUT_DIR = Path(args.output) if args.output else cfg.OUTPUT_DIR / "economic_analysis" / stem
    logger.info("%s  [zone %s]  ->  %s/", args.input, _DEFAULT_ZONE, _OUTPUT_DIR)
    run(args.input, args.rebuild)


if __name__ == "__main__":
    main()
