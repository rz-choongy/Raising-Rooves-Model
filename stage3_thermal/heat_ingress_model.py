"""
Transient roof heat-ingress model for Stage 3 — vectorised across buildings.

This is the pipeline engine ported from ``stage3_thermal/heat_ingress_model.ipynb``
(the single-building reference). It marches a 1-D forward-Euler finite-volume
conduction model through a layered roof:

    outside air ──[h_ext + shortwave + long-wave]── steel ── insulation ── cavity
      ── plaster ──[h_internal]── indoor air (held at a fixed setpoint)

The notebook loops one building at a time; here ``MidTemps`` is a ``(layers,
buildings)`` array so every building in a suburb is marched in lockstep. The
method, timestep, and equations are identical — only the per-building Python
loop is removed.

Stage 3 cool-roof saving per building = march the model at the building's current
solar absorptance and again at ``COOL_ROOF_ABSORPTANCE``, then difference the
plaster→interior heat flow. Hours with outdoor temp ≥ ``CDD_BASE_TEMP`` count the
avoided heat as a cooling-season saving; colder hours count it as a heating-season
penalty (the cool roof also rejects wanted winter solar gain).

Weather is uniform across a suburb (BARRA2 ~11 km grid) and hourly; wind → h_ext
is recomputed every hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

try:  # optional accelerator for the tight time-marching loop
    from numba import njit, prange

    _HAS_NUMBA = True
except Exception:  # noqa: BLE001 - numba is optional; fall back to vectorised numpy
    _HAS_NUMBA = False

    def njit(*args, **kwargs):  # type: ignore[misc]
        def _wrap(fn):
            return fn

        return _wrap(args[0]) if args and callable(args[0]) else _wrap

    prange = range  # type: ignore[assignment]

from config.settings import (
    CDD_BASE_TEMP,
    COOL_ROOF_ABSORPTANCE,
    COOLING_FRACTION,
    GRID_EMISSIONS_FACTOR_KG_KWH,
    H_OUT_WIND_INTERCEPT_W_M2K,
    H_OUT_WIND_SLOPE_W_M2K_PER_MS,
    HEAT_INGRESS_INDOOR_SETPOINT_C,
    HEAT_INGRESS_INTERNAL_H_W_M2K,
    HEAT_INGRESS_ROOF_EMISSIVITY,
    HEAT_INGRESS_ROOF_HEIGHT_M,
    HEAT_INGRESS_SOLVER_DT_S,
    HEAT_INGRESS_SPINUP_HOURS,
    HEATING_FRACTION,
    HVAC_COP_COMMERCIAL,
    HVAC_COP_RESIDENTIAL,
    ROOF_LAYERS_CSV,
)
from shared.logging_config import setup_logging

logger = setup_logging("heat_ingress_model")

# ── Physical / model constants ───────────────────────────────────────────────
_STEFAN_BOLTZMANN_W_M2K4 = 5.6703e-8
_KELVIN = 273.15
_LAYER_ORDER = ("Steel", "Insulation", "Cavity", "Plaster")

# Sky temperature for the long-wave term, following the notebook's transient
# cell: T_sky ≈ T_air − 10 K (the "TSky = Tair - (10 to 20) K" assumption).
_SKY_DEPRESSION_K = 10.0

# Wind-speed height correction (BARRA2 10 m wind → roof height), EnergyPlus
# Eq. 3.84. Met station = open country; site = towns/cities. From notebook cell
# "External Convection Coefficient Calculation".
_MET_WIND_HEIGHT_M = 10.0
_MET_ALPHA = 0.14
_MET_DELTA_M = 270.0
_SITE_ALPHA = 0.33
_SITE_DELTA_M = 460.0

# ISO 6946 thermal resistance for the 0.3 m unventilated ceiling cavity under
# downward (summer) heat flow. The notebook's transient run uses this in place
# of the nominal CSV value; a direction-dependent switch (0.16 upward) is a
# documented follow-up. Set to None to keep the CSV value instead.
CAVITY_R_DOWNWARD_M2K_W = 0.23

# Building types billed as commercial: different HVAC COP. Ported from the
# retired thermal_calculator.
_COMMERCIAL_TYPES = frozenset(
    {"commercial", "office", "retail", "industrial", "warehouse"}
)


def _normalize_label(value) -> str:
    """Lower-case and strip an OSM label; treat None/NaN as an empty string."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    return str(value).lower().strip()


def hvac_cop(building_type) -> float:
    """COP for the building's cooling/heating plant (commercial 4.0, else 3.0)."""
    if _normalize_label(building_type) in _COMMERCIAL_TYPES:
        return HVAC_COP_COMMERCIAL
    return HVAC_COP_RESIDENTIAL


# ── Roof construction ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RoofStack:
    """Layered roof construction, outermost layer first."""

    layer_names: tuple[str, ...]
    thickness_m: np.ndarray
    density_kg_m3: np.ndarray
    heat_capacity_j_kgk: np.ndarray
    r_value_m2k_w: np.ndarray

    @property
    def areal_heat_capacity_j_m2k(self) -> np.ndarray:
        """ρ·Cp·t per layer — the thermal mass the solver updates."""
        return self.density_kg_m3 * self.heat_capacity_j_kgk * self.thickness_m


def load_roof_layers(
    csv_path: str | Path | None = None,
    *,
    cavity_r_m2k_w: float | None = CAVITY_R_DOWNWARD_M2K_W,
) -> RoofStack:
    """
    Load the roof layer stack from a ``Regular_Roof.csv``-schema file.

    Columns: ``Material_type, Thickness_m, Density_Kg_m3, Spec_Heat_Cap_J_KgK,
    R_value_m2K_W``. Rows are reordered to steel → insulation → cavity → plaster.

    Args:
        csv_path: CSV path. Defaults to ``config.settings.ROOF_LAYERS_CSV``.
        cavity_r_m2k_w: Override for the cavity layer's R-value (see
            ``CAVITY_R_DOWNWARD_M2K_W``). ``None`` keeps the CSV value.
    """
    path = Path(csv_path) if csv_path is not None else Path(ROOF_LAYERS_CSV)
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    df = df.assign(_key=df["Material_type"].str.strip()).set_index("_key")
    missing = [name for name in _LAYER_ORDER if name not in df.index]
    if missing:
        raise ValueError(f"{path} is missing roof layers: {missing}")
    df = df.loc[list(_LAYER_ORDER)]

    r_values = df["R_value_m2K_W"].to_numpy(float)
    if cavity_r_m2k_w is not None:
        r_values = r_values.copy()
        r_values[_LAYER_ORDER.index("Cavity")] = float(cavity_r_m2k_w)

    return RoofStack(
        layer_names=_LAYER_ORDER,
        thickness_m=df["Thickness_m"].to_numpy(float),
        density_kg_m3=df["Density_Kg_m3"].to_numpy(float),
        heat_capacity_j_kgk=df["Spec_Heat_Cap_J_KgK"].to_numpy(float),
        r_value_m2k_w=r_values,
    )


# ── Weather preparation ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class HourlyWeather:
    """Suburb-uniform hourly forcing for the transient march."""

    time_melbourne: np.ndarray          # datetime64[ns], local, tz-naive
    outdoor_temp_c: np.ndarray          # (H,)
    shortwave_incident_w_m2: np.ndarray  # (H,) direct + diffuse on the roof
    h_ext_w_m2k: np.ndarray             # (H,) outdoor surface film coeff

    @property
    def n_hours(self) -> int:
        return self.outdoor_temp_c.size


def build_hourly_weather(
    weather_df: pd.DataFrame,
    *,
    roof_height_m: float | None = None,
) -> HourlyWeather:
    """
    Turn a raw BARRA2 hourly CSV/frame into model forcing arrays.

    Expected columns (``tools.fetch_heat_ingress_weather`` schema):
    ``time_UTC, rsdsdir_Wm2, rsdsdif_Wm2, temp_C, wind_ms`` (others ignored).
    """
    z = float(roof_height_m if roof_height_m is not None else HEAT_INGRESS_ROOF_HEIGHT_M)

    df = weather_df.copy()
    df.columns = df.columns.str.strip()

    time_utc = pd.to_datetime(df["time_UTC"], utc=True)
    df = df.assign(_t=time_utc).sort_values("_t").reset_index(drop=True)
    time_melb = df["_t"].dt.tz_convert("Australia/Melbourne").dt.tz_localize(None)

    incident = df["rsdsdir_Wm2"].astype(float).to_numpy() + df["rsdsdif_Wm2"].astype(float).to_numpy()

    wind_ms = df["wind_ms"].astype(float).to_numpy()
    v_local = (
        wind_ms
        * (_MET_DELTA_M / _MET_WIND_HEIGHT_M) ** _MET_ALPHA
        * (z / _SITE_DELTA_M) ** _SITE_ALPHA
    )
    h_ext = H_OUT_WIND_INTERCEPT_W_M2K + H_OUT_WIND_SLOPE_W_M2K_PER_MS * v_local

    return HourlyWeather(
        time_melbourne=time_melb.to_numpy(),
        outdoor_temp_c=df["temp_C"].astype(float).to_numpy(),
        shortwave_incident_w_m2=incident,
        h_ext_w_m2k=h_ext,
    )


# ── Stability ────────────────────────────────────────────────────────────────
def max_stable_timestep(
    stack: RoofStack,
    h_ext_max_w_m2k: float,
    internal_h_w_m2k: float = HEAT_INGRESS_INTERNAL_H_W_M2K,
) -> float:
    """
    Largest forward-Euler timestep (s) that keeps every layer's update stable.

    Port of the notebook's stability cell, evaluated with the most conservative
    (lowest) cavity resistance and the peak h_ext over the whole weather series.
    """
    r = stack.r_value_m2k_w.copy()
    r[_LAYER_ORDER.index("Cavity")] = min(r[_LAYER_ORDER.index("Cavity")], 0.16)
    face_conductance = 2.0 / (r[:-1] + r[1:])
    inner_conductance = 1.0 / (r[-1] / 2.0 + 1.0 / internal_h_w_m2k)
    conductance_sum = np.concatenate(
        [
            [h_ext_max_w_m2k + face_conductance[0]],
            face_conductance[:-1] + face_conductance[1:],
            [face_conductance[-1] + inner_conductance],
        ]
    )
    dt_limits = stack.areal_heat_capacity_j_m2k / conductance_sum
    return float(dt_limits.min())


def resolve_timestep(
    stack: RoofStack,
    h_ext_max_w_m2k: float,
    *,
    nominal_dt_s: float = HEAT_INGRESS_SOLVER_DT_S,
    safety: float = 0.9,
) -> float:
    """Nominal timestep, clamped below the stability limit if necessary."""
    dt_max = max_stable_timestep(stack, h_ext_max_w_m2k)
    if nominal_dt_s < dt_max:
        return float(nominal_dt_s)
    clamped = safety * dt_max
    logger.warning(
        "Solver dt %.1f s exceeds the stability limit %.2f s (h_ext_max %.2f W/m²K); "
        "clamping to %.2f s.",
        nominal_dt_s, dt_max, h_ext_max_w_m2k, clamped,
    )
    return clamped


# ── Transient march ─────────────────────────────────────────────────────────
# The forward-Euler recursion over ~90 sub-steps/hour cannot be vectorised along
# the time axis. Two implementations of the same arithmetic:
#   * _march_kernel  — scalar nested loops, JIT-compiled + parallelised over
#     buildings by numba when it is installed (≈50× faster);
#   * _march_numpy   — the same march vectorised across buildings, used as the
#     fallback when numba is unavailable.
# Both are float64 and produce identical results (see tests).


@njit(cache=True, parallel=True, fastmath=False)
def _march_kernel(
    temp_k,          # (H,)
    incident,        # (H,)
    h_ext,           # (H,)
    absorptance,     # (B,)
    r0, r1, r2, r3,  # layer resistances
    cap0, cap1, cap2, cap3,  # areal heat capacities ρ·Cp·t
    dt,
    substeps,
    internal_h,
    t_inside_k,
    rad_coeff,
    sky_dep,
    mid_init,        # (4, B)
    out,             # (H-1, B)
):
    n_intervals = temp_k.shape[0] - 1
    n_buildings = absorptance.shape[0]
    fr0 = r0 + r1
    fr1 = r1 + r2
    fr2 = r2 + r3
    inner_denom = r3 / 2.0 + 1.0 / internal_h
    dt_over_3600 = dt / 3600.0
    denom_frac = 1.0 / (substeps - 1) if substeps > 1 else 0.0

    for b in prange(n_buildings):
        a = absorptance[b]
        m0 = mid_init[0, b]
        m1 = mid_init[1, b]
        m2 = mid_init[2, b]
        m3 = mid_init[3, b]
        for h in range(n_intervals):
            t0 = temp_k[h]
            dt_out = temp_k[h + 1] - t0
            s0 = incident[h]
            d_inc = incident[h + 1] - s0
            he0 = h_ext[h]
            d_he = h_ext[h + 1] - he0
            acc = 0.0
            for s in range(substeps):
                f = s * denom_frac
                t_out = t0 + dt_out * f
                inc = s0 + d_inc * f
                hx = he0 + d_he * f
                sky = t_out - sky_dep

                q_outer = (
                    a * inc
                    + hx * (t_out - m0)
                    + rad_coeff * (sky * sky * sky * sky - m0 * m0 * m0 * m0)
                )
                qf0 = 2.0 * (m0 - m1) / fr0
                qf1 = 2.0 * (m1 - m2) / fr1
                qf2 = 2.0 * (m2 - m3) / fr2
                q_inner = (m3 - t_inside_k) / inner_denom

                acc += q_inner * dt_over_3600

                m0 += dt * (q_outer - qf0) / cap0
                m1 += dt * (qf0 - qf1) / cap1
                m2 += dt * (qf1 - qf2) / cap2
                m3 += dt * (qf2 - q_inner) / cap3
            out[h, b] = acc


def _march_numpy(
    temp_k, incident, h_ext, absorptance, r_values, areal_capacity,
    dt, substeps, internal_h, t_inside_k, rad_coeff, sky_dep, mid,
):
    n_intervals = temp_k.shape[0] - 1
    n_layers = mid.shape[0]
    face_r = (r_values[:-1] + r_values[1:])[:, None]
    inner_denom = r_values[-1] / 2.0 + 1.0 / internal_h
    cap = areal_capacity[:, None]
    frac = np.linspace(0.0, 1.0, substeps)
    out = np.zeros((n_intervals, absorptance.size), dtype=float)

    for hour in range(n_intervals):
        t_out = temp_k[hour] + (temp_k[hour + 1] - temp_k[hour]) * frac
        inc = incident[hour] + (incident[hour + 1] - incident[hour]) * frac
        hx = h_ext[hour] + (h_ext[hour + 1] - h_ext[hour]) * frac
        acc = np.zeros(absorptance.size)
        for s in range(substeps):
            sky = t_out[s] - sky_dep
            q_outer = (
                absorptance * inc[s]
                + hx[s] * (t_out[s] - mid[0])
                + rad_coeff * (sky * sky * sky * sky - mid[0] * mid[0] * mid[0] * mid[0])
            )
            q_face = 2.0 * (mid[:-1] - mid[1:]) / face_r
            q_inner = (mid[-1] - t_inside_k) / inner_denom
            acc += q_inner * dt / 3600.0
            net = np.empty((n_layers, absorptance.size))
            net[0] = q_outer - q_face[0]
            net[1:-1] = q_face[:-1] - q_face[1:]
            net[-1] = q_face[-1] - q_inner
            mid += dt * net / cap
        out[hour] = acc
    return out


def march_interior_flux(
    weather: HourlyWeather,
    stack: RoofStack,
    absorptance,
    *,
    dt_s: float | None = None,
    internal_h_w_m2k: float = HEAT_INGRESS_INTERNAL_H_W_M2K,
    indoor_setpoint_c: float = HEAT_INGRESS_INDOOR_SETPOINT_C,
    emissivity: float = HEAT_INGRESS_ROOF_EMISSIVITY,
    initial_temps_k=None,
) -> np.ndarray:
    """
    March the transient model and return hourly plaster→interior heat energy.

    Args:
        weather: Suburb-uniform hourly forcing.
        stack: Roof construction (exactly 4 layers).
        absorptance: Scalar or length-``B`` array of roof solar absorptances.
        dt_s: Solver timestep. Defaults to ``HEAT_INGRESS_SOLVER_DT_S`` (caller
            should pass a stability-checked value via ``resolve_timestep``).
        initial_temps_k: Optional ``(L,)`` or ``(L, B)`` starting layer mid-plane
            temperatures. Default: every layer at the first outdoor temperature
            (the run should discard a spin-up window — see ``annual_benefit``).

    Returns:
        ``(n_hours - 1, B)`` array of interior heat energy per m² of roof, in
        Wh/m², one column per building. Positive = heat into the interior.
    """
    absorptance = np.ascontiguousarray(
        np.atleast_1d(np.asarray(absorptance, dtype=float))
    )
    n_buildings = absorptance.size
    n_layers = stack.thickness_m.size
    if n_layers != 4:
        raise ValueError("The heat-ingress model expects exactly 4 roof layers.")

    dt = float(dt_s if dt_s is not None else HEAT_INGRESS_SOLVER_DT_S)
    substeps = int(np.ceil(3600.0 / dt))

    temp_k = np.ascontiguousarray(weather.outdoor_temp_c + _KELVIN)
    incident = np.ascontiguousarray(weather.shortwave_incident_w_m2)
    h_ext = np.ascontiguousarray(weather.h_ext_w_m2k)
    n_intervals = weather.n_hours - 1
    if n_intervals < 1:
        raise ValueError("Weather series needs at least two hourly rows.")

    r = stack.r_value_m2k_w
    cap = stack.areal_heat_capacity_j_m2k
    t_inside_k = indoor_setpoint_c + _KELVIN
    rad_coeff = emissivity * _STEFAN_BOLTZMANN_W_M2K4

    if initial_temps_k is None:
        mid = np.full((n_layers, n_buildings), temp_k[0], dtype=float)
    else:
        mid = np.array(
            np.broadcast_to(
                np.asarray(initial_temps_k, dtype=float).reshape(n_layers, -1),
                (n_layers, n_buildings),
            ),
            dtype=float,
        )

    if _HAS_NUMBA:
        out = np.zeros((n_intervals, n_buildings), dtype=float)
        _march_kernel(
            temp_k, incident, h_ext, absorptance,
            r[0], r[1], r[2], r[3],
            cap[0], cap[1], cap[2], cap[3],
            dt, substeps, internal_h_w_m2k, t_inside_k, rad_coeff,
            _SKY_DEPRESSION_K, np.ascontiguousarray(mid), out,
        )
    else:
        out = _march_numpy(
            temp_k, incident, h_ext, absorptance, r, cap,
            dt, substeps, internal_h_w_m2k, t_inside_k, rad_coeff,
            _SKY_DEPRESSION_K, mid,
        )

    if not np.all(np.isfinite(out)):
        raise FloatingPointError(
            "Transient march produced non-finite heat flux — dt may be above the "
            "stability limit (use resolve_timestep())."
        )
    return out


# ── Aggregation to per-building benefit ──────────────────────────────────────
_OUTPUT_COLUMNS = (
    "roof_heat_ingress_base_kwh_m2_yr",
    "roof_heat_ingress_cool_kwh_m2_yr",
    "cooling_season_heat_avoided_kwh_yr",
    "heating_season_heat_added_kwh_yr",
    "cooling_fraction_applied",
    "hvac_cop",
    "electricity_saved_kwh_yr",
    "heating_penalty_electricity_kwh_yr",
    "net_electricity_saved_kwh_yr",
    "co2_electricity_saved_kg_yr",
    "net_co2_electricity_saved_kg_yr",
)


def annual_benefit(
    flux_base_wh_m2: np.ndarray,
    flux_cool_wh_m2: np.ndarray,
    weather: HourlyWeather,
    roof_surface_area_m2,
    building_type=None,
    *,
    spin_up_hours: int = HEAT_INGRESS_SPINUP_HOURS,
    cdd_base_temp_c: float = CDD_BASE_TEMP,
    cooling_fraction: float = COOLING_FRACTION,
    heating_fraction: float = HEATING_FRACTION,
    co2_factor_kg_kwh: float = GRID_EMISSIONS_FACTOR_KG_KWH,
) -> pd.DataFrame:
    """
    Difference the two marches and roll them up to per-building annual figures.

    ``flux_*`` are ``(n_intervals, B)`` Wh/m² arrays from ``march_interior_flux``
    (base = current absorptance, cool = ``COOL_ROOF_ABSORPTANCE``). The first
    ``spin_up_hours`` intervals are discarded as thermal spin-up.

    Returns a ``B``-row DataFrame with :data:`_OUTPUT_COLUMNS`.
    """
    flux_base = np.asarray(flux_base_wh_m2, dtype=float)
    flux_cool = np.asarray(flux_cool_wh_m2, dtype=float)
    if flux_base.shape != flux_cool.shape:
        raise ValueError("base and cool flux arrays must have the same shape")

    n_buildings = flux_base.shape[1]
    area = np.asarray(roof_surface_area_m2, dtype=float)
    area = np.broadcast_to(area, (n_buildings,)).astype(float)

    if building_type is None:
        cop = np.full(n_buildings, HVAC_COP_RESIDENTIAL, dtype=float)
    else:
        bt = list(building_type) if not np.isscalar(building_type) else [building_type] * n_buildings
        cop = np.array([hvac_cop(b) for b in bt], dtype=float)

    # Outdoor temp at the start of each hourly interval, aligned to the flux rows.
    interval_temp_c = weather.outdoor_temp_c[:-1]

    spin = max(0, int(spin_up_hours))
    sl = slice(spin, None)
    base = flux_base[sl]
    cool = flux_cool[sl]
    temp = interval_temp_c[spin : spin + base.shape[0]]

    delta_wh_m2 = base - cool  # >0 : heat the cool roof kept out of the interior
    cooling_mask = temp >= cdd_base_temp_c

    cooling_wh_m2 = delta_wh_m2[cooling_mask].sum(axis=0)
    heating_wh_m2 = delta_wh_m2[~cooling_mask].sum(axis=0)

    # Wh/m² → kWh/m², then × roof surface area → kWh/building. Clamp ≥ 0: an
    # already-reflective roof (absorptance ≤ target) yields no saving/penalty.
    cooling_kwh = np.maximum(0.0, cooling_wh_m2 / 1000.0) * area
    heating_kwh = np.maximum(0.0, heating_wh_m2 / 1000.0) * area

    electricity_saved = cooling_kwh * cooling_fraction / cop
    heating_penalty_elec = heating_kwh * heating_fraction / cop
    net_electricity = electricity_saved - heating_penalty_elec

    base_kwh_m2 = flux_base[sl].sum(axis=0) / 1000.0
    cool_kwh_m2 = flux_cool[sl].sum(axis=0) / 1000.0

    return pd.DataFrame(
        {
            "roof_heat_ingress_base_kwh_m2_yr": np.round(base_kwh_m2, 2),
            "roof_heat_ingress_cool_kwh_m2_yr": np.round(cool_kwh_m2, 2),
            "cooling_season_heat_avoided_kwh_yr": np.round(cooling_kwh, 1),
            "heating_season_heat_added_kwh_yr": np.round(heating_kwh, 1),
            "cooling_fraction_applied": cooling_fraction,
            "hvac_cop": cop,
            "electricity_saved_kwh_yr": np.round(electricity_saved, 1),
            "heating_penalty_electricity_kwh_yr": np.round(heating_penalty_elec, 1),
            "net_electricity_saved_kwh_yr": np.round(net_electricity, 1),
            "co2_electricity_saved_kg_yr": np.round(electricity_saved * co2_factor_kg_kwh, 1),
            "net_co2_electricity_saved_kg_yr": np.round(net_electricity * co2_factor_kg_kwh, 1),
        }
    )


def run_model(
    df: pd.DataFrame,
    weather_df: pd.DataFrame,
    *,
    roof_stack: RoofStack | None = None,
    absorptance_col: str = "absorptance_before",
    area_col: str = "roof_surface_area_m2",
    building_type_col: str = "building_type",
    cool_absorptance: float = COOL_ROOF_ABSORPTANCE,
) -> pd.DataFrame:
    """
    End-to-end Stage 3 engine: per-building transient benefit for one suburb.

    Marches every building once at its ``absorptance_before`` and once at
    ``cool_absorptance`` (stacked into a single vectorised march), then rolls up
    to the annual per-building columns.

    Returns a DataFrame indexed like ``df`` with :data:`_OUTPUT_COLUMNS`.
    """
    stack = roof_stack if roof_stack is not None else load_roof_layers()
    weather = build_hourly_weather(weather_df)
    dt = resolve_timestep(stack, float(np.max(weather.h_ext_w_m2k)))

    n = len(df)
    absorptance_before = (
        pd.to_numeric(df[absorptance_col], errors="coerce")
        .fillna(1.0 - COOL_ROOF_ABSORPTANCE)  # unknown → conservative dark roof
        .to_numpy(float)
    )
    area = pd.to_numeric(df[area_col], errors="coerce").fillna(0.0).to_numpy(float)
    building_type = (
        df[building_type_col].tolist() if building_type_col in df.columns else [None] * n
    )

    # One march for both scenarios: columns [0:n] = current, [n:2n] = cool roof.
    stacked_absorptance = np.concatenate(
        [absorptance_before, np.full(n, float(cool_absorptance))]
    )
    logger.info(
        "Marching %d buildings × 2 scenarios over %d h at dt=%.1f s (%d substeps/h)...",
        n, weather.n_hours, dt, int(np.ceil(3600.0 / dt)),
    )
    flux = march_interior_flux(weather, stack, stacked_absorptance, dt_s=dt)

    result = annual_benefit(
        flux[:, :n], flux[:, n:], weather, area, building_type
    )
    result.index = df.index
    return result
