"""
Transient roof heat-ingress model for Stage 3 — vectorised across buildings.

This is the pipeline engine ported from
``stage3_thermal/Final_Heat_Ingress_Model.ipynb`` (the single-building
reference; port 2026-09-19, superseding the earlier
``heat_ingress_model.ipynb``). It marches a 1-D forward-Euler finite-volume
conduction model through a layered roof:

    outside air ──[h_ext + shortwave + long-wave]── outer skin ── airspace
      ── insulation ── inner lining ──[adaptive h]── indoor air (fixed setpoint)

Four roof constructions are committed (``RoofStack``, ``load_roof_layers``):
steel deck (default), concrete tile, terracotta tile, and slate — each with its
own outer-skin thickness/density/heat-capacity/R-value/emissivity.
``stack_for_material`` picks per building from its ``roof_material``.

The notebook loops one building at a time; here ``MidTemps`` is a ``(layers,
buildings)`` array so every building sharing a roof construction is marched in
lockstep. The method, timestep, and equations are identical — only the
per-building Python loop is removed.

Two physics decisions came from the final notebook port:
  * Sky temperature uses a dew-point + time-of-day clear-sky correlation
    (Bliss 1961), not a fixed ``T_air - 10 K`` offset.
  * The airspace layer's two face conductances (outer skin↔airspace,
    airspace↔insulation) use EnergyPlus adaptive natural-convection
    coefficients (direction-dependent on the instantaneous ΔT each substep),
    as does the inner lining→indoor face — replacing the previous fixed
    cavity-R override and fixed internal-h constant.

Stage 3 cool-roof saving per building = march the model at the building's current
solar absorptance and again at the cool coating appropriate to its roof type
(``COOL_ROOF_ABSORPTANCE_BY_STACK``), then difference the
plaster→interior heat flow. Hours with outdoor temp ≥ ``CDD_BASE_TEMP`` count the
avoided heat as a cooling-season saving; colder hours count it as a heating-season
penalty (the cool roof also rejects wanted winter solar gain).

An optional, opt-in insulation-upgrade scenario (``run_model``'s
``insulation_r_upgrade_m2k_w`` / ``insulation_thickness_upgrade_m``) marches a
third column per building at the same absorptance/emissivity but a different
insulation R-value/thickness, so the ceiling-insulation lever can be compared
against the roof-coating lever. Unlike absorptance, higher insulation
resistance only ever dampens conduction — it saves energy in both seasons,
never trades one off against the other — so it gets its own aggregation,
``annual_benefit_insulation``, rather than reusing ``annual_benefit``'s
summer-gain/winter-penalty framing.

Weather is uniform across a suburb (BARRA2 ~11 km grid) and hourly; wind → h_ext
is recomputed every hour.
"""

from __future__ import annotations

import math
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
    COOL_COATING_BY_STACK,
    COOL_ROOF_ABSORPTANCE_BY_STACK,
    COOLMAX_ABSORPTANCE,
    COOLMAX_EMISSIVITY,
    COOLING_FRACTION,
    DEFAULT_ROOF_STACK,
    GRID_EMISSIONS_FACTOR_KG_KWH,
    H_OUT_WIND_INTERCEPT_W_M2K,
    H_OUT_WIND_SLOPE_W_M2K_PER_MS,
    HEAT_INGRESS_COOLING_SETPOINT_C,
    HEAT_INGRESS_HEATING_SETPOINT_C,
    HEAT_INGRESS_ROOF_EMISSIVITY,
    HEAT_INGRESS_ROOF_HEIGHT_M,
    HEAT_INGRESS_ROOF_TILT_DEG,
    HEAT_INGRESS_SOLVER_DT_S,
    HEAT_INGRESS_SPINUP_HOURS,
    HEATING_FRACTION,
    HVAC_COP_COMMERCIAL,
    HVAC_COP_RESIDENTIAL,
    INSULATION_UPGRADE_R_M2K_W,
    INSULATION_UPGRADE_THICKNESS_M,
    ROOF_LAYERS_CSV,
    ROOF_STACK_BY_MATERIAL,
    ROOF_TYPE_METAL_DARK_MIN_ABSORPTANCE,
    ROOF_LAYERS_SLATE_CSV,
    ROOF_LAYERS_TERRACOTTA_CSV,
    ROOF_LAYERS_TILE_CSV,
)
from shared.logging_config import setup_logging

logger = setup_logging("heat_ingress_model")

# ── Physical / model constants ───────────────────────────────────────────────
_STEFAN_BOLTZMANN_W_M2K4 = 5.6703e-8
_KELVIN = 273.15

# Clear-sky temperature (Bliss 1961 dew-point correlation), replacing the
# earlier fixed T_sky = T_air - 10 K assumption. A1/B1 are the Magnus dew-point
# approximation constants; the 0.013*cos(15°*hour) term is the diurnal
# correction (period 24 h). From Final_Heat_Ingress_Model.ipynb.
_DEWPOINT_A = 17.625
_DEWPOINT_B = 243.04
_DEG_TO_RAD = math.pi / 180.0

# Wind-speed height correction (BARRA2 10 m wind → roof height), EnergyPlus
# Eq. 3.84. Met station = open country; site = towns/cities. From notebook cell
# "External Convection Coefficient Calculation".
_MET_WIND_HEIGHT_M = 10.0
_MET_ALPHA = 0.14
_MET_DELTA_M = 270.0
_SITE_ALPHA = 0.33
_SITE_DELTA_M = 460.0

# EnergyPlus natural-convection correlations (McAdams-derived; Eq. 3.155
# upward / 3.156 downward heat flow) driving the airspace layer's two face
# conductances and the inner-lining→indoor face — replacing the previous fixed
# cavity-R override and fixed internal-h constant. From
# Final_Heat_Ingress_Model.ipynb's "Calculating initial midpoint temp" /
# transient cells.
_NATURAL_CONV_DOWN_COEFF = 1.81
_NATURAL_CONV_UP_COEFF = 9.482
_NATURAL_CONV_DOWN_BASE = 1.382
_NATURAL_CONV_UP_BASE = 7.238

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
# A roof stack is exactly 4 conduction layers, outermost first: outer_skin,
# airspace, insulation, inner_lining -- every committed CSV uses this order, so
# the airspace layer's Layer_Role=="airspace" row must sit at index 1
# (march_interior_flux enforces this). Its two neighbouring face conductances
# use adaptive natural convection rather than the row's own R-value (see
# march_interior_flux); the other two committed materials (metal/tile) used
# different cavity positions before this port -- now standardised so every
# stack shares the same physics.
_VALID_LAYER_ROLES = frozenset({"outer_skin", "insulation", "airspace", "inner_lining"})


@dataclass(frozen=True)
class RoofStack:
    """Layered roof construction, outermost layer first."""

    layer_names: tuple[str, ...]
    thickness_m: np.ndarray
    density_kg_m3: np.ndarray
    heat_capacity_j_kgk: np.ndarray
    r_value_m2k_w: np.ndarray
    cavity_index: int  # position of the Layer_Role == "airspace" row
    emissivity: float  # outer-skin long-wave emissivity, current absorptance
    emissivity_cool: float  # outer-skin long-wave emissivity, cool-roof coating

    @property
    def areal_heat_capacity_j_m2k(self) -> np.ndarray:
        """ρ·Cp·t per layer — the thermal mass the solver updates."""
        return self.density_kg_m3 * self.heat_capacity_j_kgk * self.thickness_m


def load_roof_layers(csv_path: str | Path | None = None) -> RoofStack:
    """
    Load a roof layer stack from a 4-row, outer-to-inner layer CSV.

    Columns: ``Material_type, Thickness_m, Density_Kg_m3, Spec_Heat_Cap_J_KgK,
    R_value_m2K_W, Layer_Role``, plus optional ``Emissivity, Emissivity_cool``
    on the outer_skin row (falls back to ``config.settings.
    HEAT_INGRESS_ROOF_EMISSIVITY`` for both when absent). Rows are used in file
    order (outer -> inner); exactly one row must have
    ``Layer_Role == "airspace"``.

    The airspace layer's own R-value is carried for reference only — the
    transient march replaces both of its face conductances with adaptive
    natural-convection coefficients (see ``march_interior_flux``), so this
    value no longer participates in conduction directly. It still bounds
    ``max_stable_timestep``'s worst case.

    Args:
        csv_path: CSV path. Defaults to ``config.settings.ROOF_LAYERS_CSV``
            (the metal-deck stack).
    """
    path = Path(csv_path) if csv_path is not None else Path(ROOF_LAYERS_CSV)
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    if len(df) != 4:
        raise ValueError(f"{path} must have exactly 4 roof layers, found {len(df)}.")
    if "Layer_Role" not in df.columns:
        raise ValueError(f"{path} is missing the Layer_Role column.")
    roles = df["Layer_Role"].str.strip().tolist()
    bad_roles = set(roles) - _VALID_LAYER_ROLES
    if bad_roles:
        raise ValueError(f"{path} has unknown Layer_Role value(s): {bad_roles}")
    airspace_rows = [i for i, r in enumerate(roles) if r == "airspace"]
    if len(airspace_rows) != 1:
        raise ValueError(f"{path} must have exactly one Layer_Role=='airspace' row.")
    cavity_index = airspace_rows[0]
    outer_row = roles.index("outer_skin")

    if "Emissivity" in df.columns and pd.notna(df["Emissivity"].iloc[outer_row]):
        emissivity = float(df["Emissivity"].iloc[outer_row])
    else:
        emissivity = HEAT_INGRESS_ROOF_EMISSIVITY
    if "Emissivity_cool" in df.columns and pd.notna(df["Emissivity_cool"].iloc[outer_row]):
        emissivity_cool = float(df["Emissivity_cool"].iloc[outer_row])
    else:
        emissivity_cool = HEAT_INGRESS_ROOF_EMISSIVITY

    return RoofStack(
        layer_names=tuple(df["Material_type"].str.strip()),
        thickness_m=df["Thickness_m"].to_numpy(float),
        density_kg_m3=df["Density_Kg_m3"].to_numpy(float),
        heat_capacity_j_kgk=df["Spec_Heat_Cap_J_KgK"].to_numpy(float),
        r_value_m2k_w=df["R_value_m2K_W"].to_numpy(float),
        cavity_index=cavity_index,
        emissivity=emissivity,
        emissivity_cool=emissivity_cool,
    )


# Which committed stack a building's roof_material maps to. Four materials now
# have their own construction (thickness/density/heat-capacity/R-value/
# emissivity) ported from Final_Heat_Ingress_Model.ipynb's Roof_layers table —
# concrete tile and terracotta tile no longer share one averaged "tile" stack.
# `roof_tiles` (an ambiguous raw OSM tag, not the classifier's own output) falls
# back to concrete tile, the more common of the two by a wide margin. Anything
# else unrecognised (`other`, `None`, stray raw OSM values like `metal`/
# `metal_sheet`/`glass`/`wood`) uses the default metal-deck stack, unchanged
# from before — per-building *current* absorptance still comes from
# `absorptance_before`, not this table, so an unrecognised material only loses
# construction-level fidelity (thermal mass, emissivity) and is costed with the
# metal cool coating. The map itself lives in config.settings so Stage 2 picks
# the same coating per building.
_MATERIAL_STACK_MAP = ROOF_STACK_BY_MATERIAL


def stack_for_material(roof_material) -> str:
    """Return which committed roof stack ('metal'/'concrete'/'terracotta'/'slate') a material uses."""
    return _MATERIAL_STACK_MAP.get(_normalize_label(roof_material), DEFAULT_ROOF_STACK)


def cool_absorptance_for_stack(stack_key: str) -> float:
    """Post-coating solar absorptance for a roof stack (falls back to ``COOL_ROOF_ABSORPTANCE``)."""
    return COOL_ROOF_ABSORPTANCE_BY_STACK.get(stack_key, COOL_ROOF_ABSORPTANCE)


# ── Weather preparation ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class HourlyWeather:
    """Suburb-uniform hourly forcing for the transient march."""

    time_melbourne: np.ndarray          # datetime64[ns], local, tz-naive
    outdoor_temp_c: np.ndarray          # (H,)
    shortwave_incident_w_m2: np.ndarray  # (H,) direct + diffuse on the roof
    h_ext_w_m2k: np.ndarray             # (H,) outdoor surface film coeff
    humidity_percent: np.ndarray        # (H,) relative humidity, for T_sky
    hour_of_day: np.ndarray             # (H,) local hour 0-23, for T_sky's diurnal term

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
    ``time_UTC, rsdsdir_Wm2, rsdsdif_Wm2, temp_C, wind_ms,
    rel_humidity_percent`` (others ignored).
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
        humidity_percent=df["rel_humidity_percent"].astype(float).to_numpy(),
        hour_of_day=(time_melb.dt.hour + time_melb.dt.minute / 60.0).to_numpy(dtype=float),
    )


# ── Stability ────────────────────────────────────────────────────────────────
def max_stable_timestep(stack: RoofStack, h_ext_max_w_m2k: float) -> float:
    """
    Largest forward-Euler timestep (s) that keeps every layer's update stable.

    Port of the notebook's stability cell (cell 19): evaluated with the peak
    h_ext over the whole weather series, and the worst-case (largest) adaptive
    natural-convection coefficients the airspace/indoor faces could reach,
    assuming a conservative 5 K driving temperature difference — the same
    bound Final_Heat_Ingress_Model.ipynb uses before marching.
    """
    r = stack.r_value_m2k_w
    cav = stack.cavity_index

    delta_t_max = 5.0  # K; matches the notebook's conservative timestep check
    h_up = _NATURAL_CONV_UP_COEFF * delta_t_max ** (1.0 / 3.0)
    cos_tilt = abs(math.cos(math.radians(HEAT_INGRESS_ROOF_TILT_DEG)))
    h_tilted_max = h_up / (_NATURAL_CONV_UP_BASE - cos_tilt)  # outer_skin <-> airspace
    h_horizontal_max = h_up / (_NATURAL_CONV_UP_BASE - 1.0)  # airspace <-> next layer; inner -> indoor

    face_conductance = 2.0 / (r[:-1] + r[1:])
    if cav - 1 >= 0:
        face_conductance[cav - 1] = 1.0 / (r[cav - 1] / 2.0 + 1.0 / h_tilted_max)
    if cav <= len(r) - 2:
        face_conductance[cav] = 1.0 / (1.0 / h_horizontal_max + r[cav + 1] / 2.0)

    inner_conductance = 1.0 / (r[-1] / 2.0 + 1.0 / h_horizontal_max)
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
    humidity,        # (H,) relative humidity %, for the T_sky correlation
    hour_of_day,     # (H,) local hour 0-23 at each weather timestamp
    absorptance,     # (B,)
    emissivity,      # (B,) outer-skin emissivity -- current vs cool-roof coating
    r0, r2, r3,      # outer_skin resistance (scalar); insulation resistance (B,) -- per-scenario upgrade; inner_lining (scalar)
    cap0, cap1, cap2, cap3,  # areal heat capacities rho*Cp*t: outer/airspace/inner_lining scalar, insulation (B,) per-scenario upgrade
    dt,
    substeps,
    heating_setpoint_k,
    cooling_setpoint_k,
    cos_tilt,        # |cos(roof tilt)|, outer_skin<->airspace face only
    mid_init,        # (4, B)
    out,             # (H-1, B)
):
    n_intervals = temp_k.shape[0] - 1
    n_buildings = absorptance.shape[0]
    dt_over_3600 = dt / 3600.0
    denom_frac = 1.0 / (substeps - 1) if substeps > 1 else 0.0

    # Weather is suburb-uniform -- every one of these per-substep quantities
    # (interpolated temp/incident/wind and the sky temperature, the latter a
    # log + cos + fractional power) is identical for every building. Compute
    # each substep ONCE here rather than once per (building, substep) pair --
    # with thousands of buildings per material group, marching them inside
    # the building loop below repeated this transcendental math thousands of
    # times over for no reason.
    total_substeps = n_intervals * substeps
    t_out_series = np.empty(total_substeps)
    inc_series = np.empty(total_substeps)
    hx_series = np.empty(total_substeps)
    sky_series = np.empty(total_substeps)
    t_inside_series = np.empty(total_substeps)
    idx = 0
    for h in range(n_intervals):
        t0 = temp_k[h]
        dt_out = temp_k[h + 1] - t0
        s0 = incident[h]
        d_inc = incident[h + 1] - s0
        he0 = h_ext[h]
        d_he = h_ext[h + 1] - he0
        rh0 = humidity[h]
        d_rh = humidity[h + 1] - rh0
        t_day0 = hour_of_day[h]
        for s in range(substeps):
            f = s * denom_frac
            t_out = t0 + dt_out * f
            rh = rh0 + d_rh * f

            # Clear-sky temperature (Bliss 1961 dew-point correlation).
            t_air_c = t_out - 273.15
            humidity_term = math.log(rh / 100.0) + (_DEWPOINT_A * t_air_c) / (_DEWPOINT_B + t_air_c)
            t_dp_c = _DEWPOINT_B * humidity_term / (_DEWPOINT_A - humidity_term)
            t_local = t_day0 + f
            sky = t_out * (
                0.711 + 0.0056 * t_dp_c + 0.000073 * t_dp_c * t_dp_c
                + 0.013 * math.cos(_DEG_TO_RAD * 15.0 * t_local)
            ) ** 0.25

            t_out_series[idx] = t_out
            inc_series[idx] = s0 + d_inc * f
            hx_series[idx] = he0 + d_he * f
            sky_series[idx] = sky
            t_inside_series[idx] = heating_setpoint_k if t_out < heating_setpoint_k else cooling_setpoint_k
            idx += 1

    for b in prange(n_buildings):
        a = absorptance[b]
        rad_coeff = emissivity[b] * _STEFAN_BOLTZMANN_W_M2K4
        r2_b = r2[b]
        cap2_b = cap2[b]
        fr2 = r2_b + r3
        m0 = mid_init[0, b]
        m1 = mid_init[1, b]
        m2 = mid_init[2, b]
        m3 = mid_init[3, b]
        for h in range(n_intervals):
            acc = 0.0
            base_idx = h * substeps
            for s in range(substeps):
                idx = base_idx + s
                t_out = t_out_series[idx]
                inc = inc_series[idx]
                hx = hx_series[idx]
                sky = sky_series[idx]
                t_inside_k = t_inside_series[idx]

                q_outer = (
                    a * inc
                    + hx * (t_out - m0)
                    + rad_coeff * (sky * sky * sky * sky - m0 * m0 * m0 * m0)
                )

                # Adaptive natural convection: outer_skin <-> airspace (tilted).
                delta_a = m0 - m1
                abs_da = abs(delta_a)
                if delta_a >= 0.0:
                    h_cav_a = _NATURAL_CONV_DOWN_COEFF * abs_da ** (1.0 / 3.0) / (_NATURAL_CONV_DOWN_BASE + cos_tilt)
                else:
                    h_cav_a = _NATURAL_CONV_UP_COEFF * abs_da ** (1.0 / 3.0) / (_NATURAL_CONV_UP_BASE - cos_tilt)
                qf0 = delta_a / (r0 / 2.0 + 1.0 / h_cav_a)

                # Adaptive natural convection: airspace <-> insulation (horizontal).
                delta_b = m1 - m2
                abs_db = abs(delta_b)
                if delta_b >= 0.0:
                    h_cav_b = _NATURAL_CONV_DOWN_COEFF * abs_db ** (1.0 / 3.0) / (_NATURAL_CONV_DOWN_BASE + 1.0)
                else:
                    h_cav_b = _NATURAL_CONV_UP_COEFF * abs_db ** (1.0 / 3.0) / (_NATURAL_CONV_UP_BASE - 1.0)
                qf1 = delta_b / (1.0 / h_cav_b + r2_b / 2.0)

                qf2 = 2.0 * (m2 - m3) / fr2

                # Adaptive natural convection: inner_lining <-> indoor (horizontal).
                delta_c = m3 - t_inside_k
                abs_dc = abs(delta_c)
                if delta_c >= 0.0:
                    hi = _NATURAL_CONV_DOWN_COEFF * abs_dc ** (1.0 / 3.0) / (_NATURAL_CONV_DOWN_BASE + 1.0)
                else:
                    hi = _NATURAL_CONV_UP_COEFF * abs_dc ** (1.0 / 3.0) / (_NATURAL_CONV_UP_BASE - 1.0)
                q_inner = delta_c / (r3 / 2.0 + 1.0 / hi)

                acc += q_inner * dt_over_3600

                m0 += dt * (q_outer - qf0) / cap0
                m1 += dt * (qf0 - qf1) / cap1
                m2 += dt * (qf1 - qf2) / cap2_b
                m3 += dt * (qf2 - q_inner) / cap3
            out[h, b] = acc


def _march_numpy(
    temp_k, incident, h_ext, humidity, hour_of_day,
    absorptance, emissivity, r0, r2, r3, cap0, cap1, cap2, cap3,
    dt, substeps, heating_setpoint_k, cooling_setpoint_k,
    cos_tilt, mid,
):
    # r2/cap2 may be scalar (shared insulation) or (B,) (per-scenario
    # insulation upgrade) -- numpy broadcasting handles both transparently.
    n_intervals = temp_k.shape[0] - 1
    fr2 = r2 + r3
    rad_coeff = emissivity * _STEFAN_BOLTZMANN_W_M2K4
    frac = np.linspace(0.0, 1.0, substeps)
    out = np.zeros((n_intervals, absorptance.size), dtype=float)

    # An exact zero delta_T (h_cav/hi -> 0, face resistance -> inf) is a real,
    # correctly-handled physical state (no convective coupling -> no flux
    # through that face), not an error -- suppress the benign warning.
    with np.errstate(divide="ignore", invalid="ignore"):
        for hour in range(n_intervals):
            t_out_arr = temp_k[hour] + (temp_k[hour + 1] - temp_k[hour]) * frac
            inc_arr = incident[hour] + (incident[hour + 1] - incident[hour]) * frac
            hx_arr = h_ext[hour] + (h_ext[hour + 1] - h_ext[hour]) * frac
            rh_arr = humidity[hour] + (humidity[hour + 1] - humidity[hour]) * frac
            t_day0 = hour_of_day[hour]
            acc = np.zeros(absorptance.size)
            for s in range(substeps):
                t_out = t_out_arr[s]
                inc = inc_arr[s]
                hx = hx_arr[s]
                rh = rh_arr[s]

                t_air_c = t_out - 273.15
                humidity_term = math.log(rh / 100.0) + (_DEWPOINT_A * t_air_c) / (_DEWPOINT_B + t_air_c)
                t_dp_c = _DEWPOINT_B * humidity_term / (_DEWPOINT_A - humidity_term)
                t_local = t_day0 + frac[s]
                sky = t_out * (
                    0.711 + 0.0056 * t_dp_c + 0.000073 * t_dp_c ** 2
                    + 0.013 * math.cos(_DEG_TO_RAD * 15.0 * t_local)
                ) ** 0.25
                t_inside_k = heating_setpoint_k if t_out < heating_setpoint_k else cooling_setpoint_k

                q_outer = (
                    absorptance * inc
                    + hx * (t_out - mid[0])
                    + rad_coeff * (sky ** 4 - mid[0] ** 4)
                )

                delta_a = mid[0] - mid[1]
                abs_da = np.abs(delta_a)
                h_cav_a = np.where(
                    delta_a >= 0.0,
                    _NATURAL_CONV_DOWN_COEFF * abs_da ** (1.0 / 3.0) / (_NATURAL_CONV_DOWN_BASE + cos_tilt),
                    _NATURAL_CONV_UP_COEFF * abs_da ** (1.0 / 3.0) / (_NATURAL_CONV_UP_BASE - cos_tilt),
                )
                qf0 = delta_a / (r0 / 2.0 + 1.0 / h_cav_a)

                delta_b = mid[1] - mid[2]
                abs_db = np.abs(delta_b)
                h_cav_b = np.where(
                    delta_b >= 0.0,
                    _NATURAL_CONV_DOWN_COEFF * abs_db ** (1.0 / 3.0) / (_NATURAL_CONV_DOWN_BASE + 1.0),
                    _NATURAL_CONV_UP_COEFF * abs_db ** (1.0 / 3.0) / (_NATURAL_CONV_UP_BASE - 1.0),
                )
                qf1 = delta_b / (1.0 / h_cav_b + r2 / 2.0)

                qf2 = 2.0 * (mid[2] - mid[3]) / fr2

                delta_c = mid[3] - t_inside_k
                abs_dc = np.abs(delta_c)
                hi = np.where(
                    delta_c >= 0.0,
                    _NATURAL_CONV_DOWN_COEFF * abs_dc ** (1.0 / 3.0) / (_NATURAL_CONV_DOWN_BASE + 1.0),
                    _NATURAL_CONV_UP_COEFF * abs_dc ** (1.0 / 3.0) / (_NATURAL_CONV_UP_BASE - 1.0),
                )
                q_inner = delta_c / (r3 / 2.0 + 1.0 / hi)

                acc += q_inner * dt / 3600.0
                mid[0] += dt * (q_outer - qf0) / cap0
                mid[1] += dt * (qf0 - qf1) / cap1
                mid[2] += dt * (qf1 - qf2) / cap2
                mid[3] += dt * (qf2 - q_inner) / cap3
            out[hour] = acc
    return out


def march_interior_flux(
    weather: HourlyWeather,
    stack: RoofStack,
    absorptance,
    *,
    dt_s: float | None = None,
    heating_setpoint_c: float = HEAT_INGRESS_HEATING_SETPOINT_C,
    cooling_setpoint_c: float = HEAT_INGRESS_COOLING_SETPOINT_C,
    emissivity=None,
    roof_tilt_deg: float = HEAT_INGRESS_ROOF_TILT_DEG,
    insulation_r_m2k_w=None,
    insulation_thickness_m=None,
    initial_temps_k=None,
) -> np.ndarray:
    """
    March the transient model and return hourly plaster→interior heat energy.

    Args:
        weather: Suburb-uniform hourly forcing.
        stack: Roof construction (exactly 4 layers, airspace at index 1 --
            outer_skin / airspace / insulation / inner_lining).
        absorptance: Scalar or length-``B`` array of roof solar absorptances.
        dt_s: Solver timestep. Defaults to ``HEAT_INGRESS_SOLVER_DT_S`` (caller
            should pass a stability-checked value via ``resolve_timestep``).
        heating_setpoint_c / cooling_setpoint_c: Indoor reference temperature
            the march holds each substep, switched on instantaneous outdoor
            temp — below ``heating_setpoint_c`` uses the heating setpoint,
            otherwise the cooling setpoint (see ``config.settings``).
        emissivity: Scalar or length-``B`` array of outer-skin long-wave
            emissivity. Defaults to ``stack.emissivity`` (the current-roof
            value) broadcast to every building -- pass ``stack.emissivity_cool``
            for a cool-roof scenario march.
        roof_tilt_deg: Roof pitch used by the airspace's outer-facing adaptive
            convection coefficient. Defaults to
            ``config.settings.HEAT_INGRESS_ROOF_TILT_DEG``.
        insulation_r_m2k_w / insulation_thickness_m: Scalar or length-``B``
            override for the insulation layer's R-value / thickness. Default
            (``None``) keeps the stack's own value for every building --
            pass a different value (see ``config.settings.
            INSULATION_UPGRADE_R_M2K_W`` / ``INSULATION_UPGRADE_THICKNESS_M``)
            for an insulation-upgrade scenario column. Density and specific
            heat capacity are always the stack's own (same bulk material,
            more or less of it).
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
    if stack.cavity_index != 1:
        raise ValueError(
            "The adaptive-convection physics assumes the airspace layer sits at "
            "index 1 (outer_skin, airspace, insulation, inner_lining) -- "
            f"got cavity_index={stack.cavity_index}."
        )

    if emissivity is None:
        emissivity_arr = np.full(n_buildings, stack.emissivity, dtype=float)
    else:
        emissivity_arr = np.ascontiguousarray(
            np.broadcast_to(np.asarray(emissivity, dtype=float), (n_buildings,))
        )

    if insulation_r_m2k_w is None:
        r2_arr = np.full(n_buildings, stack.r_value_m2k_w[2], dtype=float)
    else:
        r2_arr = np.ascontiguousarray(
            np.broadcast_to(np.asarray(insulation_r_m2k_w, dtype=float), (n_buildings,))
        )
    if insulation_thickness_m is None:
        insulation_thickness_arr = np.full(n_buildings, stack.thickness_m[2], dtype=float)
    else:
        insulation_thickness_arr = np.broadcast_to(
            np.asarray(insulation_thickness_m, dtype=float), (n_buildings,)
        )
    cap2_arr = np.ascontiguousarray(
        stack.density_kg_m3[2] * stack.heat_capacity_j_kgk[2] * insulation_thickness_arr
    )

    dt = float(dt_s if dt_s is not None else HEAT_INGRESS_SOLVER_DT_S)
    substeps = int(np.ceil(3600.0 / dt))

    temp_k = np.ascontiguousarray(weather.outdoor_temp_c + _KELVIN)
    incident = np.ascontiguousarray(weather.shortwave_incident_w_m2)
    h_ext = np.ascontiguousarray(weather.h_ext_w_m2k)
    humidity = np.ascontiguousarray(weather.humidity_percent)
    hour_of_day = np.ascontiguousarray(weather.hour_of_day)
    n_intervals = weather.n_hours - 1
    if n_intervals < 1:
        raise ValueError("Weather series needs at least two hourly rows.")

    r = stack.r_value_m2k_w
    cap = stack.areal_heat_capacity_j_m2k
    heating_setpoint_k = heating_setpoint_c + _KELVIN
    cooling_setpoint_k = cooling_setpoint_c + _KELVIN
    cos_tilt = abs(math.cos(math.radians(roof_tilt_deg)))

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
            temp_k, incident, h_ext, humidity, hour_of_day,
            absorptance, emissivity_arr,
            r[0], r2_arr, r[3],
            cap[0], cap[1], cap2_arr, cap[3],
            dt, substeps, heating_setpoint_k, cooling_setpoint_k,
            cos_tilt, np.ascontiguousarray(mid), out,
        )
    else:
        out = _march_numpy(
            temp_k, incident, h_ext, humidity, hour_of_day,
            absorptance, emissivity_arr, r[0], r2_arr, r[3],
            cap[0], cap[1], cap2_arr, cap[3],
            dt, substeps, heating_setpoint_k, cooling_setpoint_k,
            cos_tilt, mid,
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
    "roof_flux_mode_mismatch_hours",
    "electricity_saved_kwh_yr_fluxsign",
    "heating_penalty_electricity_kwh_yr_fluxsign",
    "net_electricity_saved_kwh_yr_fluxsign",
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
    (base = current absorptance, cool = the roof type's coated absorptance). The first
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

    # Flux-sign accounting, as in Final_Heat_Ingress_Model.ipynb's hourly
    # comparison: an hour is "cooling" when both roofs push heat into the room,
    # "heating" when both pull heat out, and dropped (NaN in the notebook) when
    # they disagree. Treats the roof as the room's only load, so it is a lower
    # bound next to the whole-house outdoor-temperature split above.
    both_in = (base > 0) & (cool > 0)
    both_out = (base < 0) & (cool < 0)
    mismatch_hours = (np.sign(base) != np.sign(cool)).sum(axis=0)
    fs_cooling_kwh = np.maximum(0.0, np.where(both_in, delta_wh_m2, 0.0).sum(axis=0) / 1000.0) * area
    fs_heating_kwh = np.maximum(0.0, np.where(both_out, delta_wh_m2, 0.0).sum(axis=0) / 1000.0) * area
    fs_saved = fs_cooling_kwh * cooling_fraction / cop
    fs_penalty = fs_heating_kwh * heating_fraction / cop

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
            "roof_flux_mode_mismatch_hours": mismatch_hours,
            "electricity_saved_kwh_yr_fluxsign": np.round(fs_saved, 1),
            "heating_penalty_electricity_kwh_yr_fluxsign": np.round(fs_penalty, 1),
            "net_electricity_saved_kwh_yr_fluxsign": np.round(fs_saved - fs_penalty, 1),
        }
    )


_INSULATION_OUTPUT_COLUMNS = (
    "roof_heat_ingress_insulation_kwh_m2_yr",
    "insulation_cooling_saved_thermal_kwh_yr",
    "insulation_heating_saved_thermal_kwh_yr",
    "insulation_cooling_saved_electricity_kwh_yr",
    "insulation_heating_saved_electricity_kwh_yr",
    "insulation_net_electricity_saved_kwh_yr",
    "insulation_net_co2_saved_kg_yr",
)


def annual_benefit_insulation(
    flux_base_wh_m2: np.ndarray,
    flux_upgrade_wh_m2: np.ndarray,
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
    Difference a base march against an insulation-upgrade march (same
    absorptance/emissivity, different insulation R-value/thickness).

    This is deliberately a separate function from ``annual_benefit``, not a
    call to it: a solar-absorptance change trades a summer cooling gain
    against a winter heating *penalty* (a cool roof also rejects wanted
    winter solar warmth), so ``annual_benefit`` sums the same
    ``base - scenario`` delta in both seasons and only relabels it
    saving/penalty. Higher insulation resistance only ever *dampens*
    conduction, in whichever direction it is currently flowing -- it saves
    energy in both seasons and never trades one off against the other. The
    "helps" sign therefore flips between seasons: cooling-season saving is
    ``base - upgrade`` (upgrade lets less heat in), heating-season saving is
    ``upgrade - base`` (upgrade lets less heat out).

    Every column name says explicitly whether it's *thermal* (roof heat flux,
    before the ``cooling_fraction``/``heating_fraction``/COP conversion) or
    *electricity* (after it, what actually shows up as a saving) --
    ``annual_benefit``'s equivalent columns are less consistent about this
    (``cooling_season_heat_avoided_kwh_yr`` is thermal,
    ``electricity_saved_kwh_yr`` is electricity, distinguished only by
    "heat" vs "electricity" in the name), which invited exactly this mix-up
    once results from both functions ended up in the same comparison.

    Returns a ``B``-row DataFrame with :data:`_INSULATION_OUTPUT_COLUMNS`.
    """
    flux_base = np.asarray(flux_base_wh_m2, dtype=float)
    flux_upgrade = np.asarray(flux_upgrade_wh_m2, dtype=float)
    if flux_base.shape != flux_upgrade.shape:
        raise ValueError("base and upgrade flux arrays must have the same shape")

    n_buildings = flux_base.shape[1]
    area = np.asarray(roof_surface_area_m2, dtype=float)
    area = np.broadcast_to(area, (n_buildings,)).astype(float)

    if building_type is None:
        cop = np.full(n_buildings, HVAC_COP_RESIDENTIAL, dtype=float)
    else:
        bt = list(building_type) if not np.isscalar(building_type) else [building_type] * n_buildings
        cop = np.array([hvac_cop(b) for b in bt], dtype=float)

    interval_temp_c = weather.outdoor_temp_c[:-1]

    spin = max(0, int(spin_up_hours))
    sl = slice(spin, None)
    base = flux_base[sl]
    upgrade = flux_upgrade[sl]
    temp = interval_temp_c[spin : spin + base.shape[0]]

    cooling_mask = temp >= cdd_base_temp_c
    cooling_wh_m2 = (base - upgrade)[cooling_mask].sum(axis=0)
    heating_wh_m2 = (upgrade - base)[~cooling_mask].sum(axis=0)

    # Clamp >= 0: a downgrade (worse R-value than the base) yields no saving.
    cooling_kwh = np.maximum(0.0, cooling_wh_m2 / 1000.0) * area
    heating_kwh = np.maximum(0.0, heating_wh_m2 / 1000.0) * area

    cooling_elec_saved = cooling_kwh * cooling_fraction / cop
    heating_elec_saved = heating_kwh * heating_fraction / cop
    total_elec_saved = cooling_elec_saved + heating_elec_saved

    upgrade_kwh_m2 = flux_upgrade[sl].sum(axis=0) / 1000.0

    return pd.DataFrame(
        {
            "roof_heat_ingress_insulation_kwh_m2_yr": np.round(upgrade_kwh_m2, 2),
            "insulation_cooling_saved_thermal_kwh_yr": np.round(cooling_kwh, 1),
            "insulation_heating_saved_thermal_kwh_yr": np.round(heating_kwh, 1),
            "insulation_cooling_saved_electricity_kwh_yr": np.round(cooling_elec_saved, 1),
            "insulation_heating_saved_electricity_kwh_yr": np.round(heating_elec_saved, 1),
            "insulation_net_electricity_saved_kwh_yr": np.round(total_elec_saved, 1),
            "insulation_net_co2_saved_kg_yr": np.round(total_elec_saved * co2_factor_kg_kwh, 1),
        }
    )


_COOLMAX_OUTPUT_COLUMNS = (
    "cooling_electricity_saved_kwh_yr_coolmax",
    "heating_penalty_electricity_kwh_yr_coolmax",
    "net_electricity_saved_kwh_yr_coolmax",
)


def _coolmax_columns(
    flux_existing: np.ndarray,
    flux_coolmax: np.ndarray,
    weather: HourlyWeather,
    area: np.ndarray,
    building_type: list,
    index: pd.Index,
) -> pd.DataFrame:
    """Existing-roof-vs-Coolmax-replacement electricity columns, via ``annual_benefit``."""
    out = annual_benefit(flux_existing, flux_coolmax, weather, area, building_type)
    return pd.DataFrame(
        {
            "cooling_electricity_saved_kwh_yr_coolmax": out["electricity_saved_kwh_yr"].to_numpy(),
            "heating_penalty_electricity_kwh_yr_coolmax": out["heating_penalty_electricity_kwh_yr"].to_numpy(),
            "net_electricity_saved_kwh_yr_coolmax": out["net_electricity_saved_kwh_yr"].to_numpy(),
        },
        index=index,
    )


def roof_type_for(roof_material, absorptance_before: float) -> str:
    """
    Pricing roof type: ``concrete``/``terracotta``/``slate``/``metal_light``/``metal_dark``.

    Tile/slate come straight from the stack. Metal keeps an explicit
    ``metal_light``/``metal_dark`` label; any other metal-stack building
    (bare ``metal``, ``other``, unknown) is split on its current absorptance
    at ``ROOF_TYPE_METAL_DARK_MIN_ABSORPTANCE``.
    """
    label = _normalize_label(roof_material)
    stack = stack_for_material(label)
    if stack != "metal":
        return stack
    if label in ("metal_light", "metal_dark"):
        return label
    return "metal_dark" if absorptance_before >= ROOF_TYPE_METAL_DARK_MIN_ABSORPTANCE else "metal_light"


# Per-building audit columns run_model prepends: which construction was marched,
# the pricing roof type, and the cool coating (product, absorptance, emissivity).
_AUDIT_COLUMNS = (
    "roof_construction", "roof_type", "coating_type",
    "cool_absorptance_applied", "cool_emissivity_applied",
)


def _load_default_stacks() -> dict[str, RoofStack]:
    """The four committed roof stacks, keyed the same way as ``stack_for_material``."""
    return {
        "metal": load_roof_layers(ROOF_LAYERS_CSV),
        "concrete": load_roof_layers(ROOF_LAYERS_TILE_CSV),
        "terracotta": load_roof_layers(ROOF_LAYERS_TERRACOTTA_CSV),
        "slate": load_roof_layers(ROOF_LAYERS_SLATE_CSV),
    }


def run_model(
    df: pd.DataFrame,
    weather_df: pd.DataFrame,
    *,
    roof_stack: RoofStack | dict[str, RoofStack] | None = None,
    absorptance_col: str = "absorptance_before",
    area_col: str = "roof_surface_area_m2",
    building_type_col: str = "building_type",
    roof_material_col: str = "roof_material",
    cool_absorptance: float | None = None,
    insulation_r_upgrade_m2k_w: float | None = None,
    insulation_thickness_upgrade_m: float | None = None,
    coolmax: bool = False,
) -> pd.DataFrame:
    """
    End-to-end Stage 3 engine: per-building transient benefit for one suburb.

    Each building is assigned a roof construction by ``roof_material``
    (``stack_for_material`` -- metal/concrete tile/terracotta tile/slate, each
    with its own outer-skin thickness/density/heat-capacity/R-value/
    emissivity). Buildings are grouped by stack, each group is marched once at
    its ``absorptance_before`` (current-roof emissivity) and once at the
    cool-coating absorptance appropriate to that roof type
    (``COOL_ROOF_ABSORPTANCE_BY_STACK``, with the stack's cool-roof
    emissivity) — stacked into a
    single vectorised march per group, each with its own stability-checked
    timestep — then rolled up to the annual per-building columns.

    Args:
        roof_stack: ``None`` (default: all four committed stacks, selected per
            building), a single ``RoofStack`` (forces every building onto it —
            useful for tests/back-compat), or an explicit ``{"metal": ...,
            "concrete": ...}`` dict.
        cool_absorptance: ``None`` (default) uses each stack's own coating
            from ``COOL_ROOF_ABSORPTANCE_BY_STACK`` (``COOL_ROOF_ABSORPTANCE``
            for a forced single stack). A float overrides it for every
            building (sensitivity sweeps / tests).
        insulation_r_upgrade_m2k_w: Opt-in third scenario -- ``None`` (default)
            leaves Stage 3's output exactly as before (no insulation columns,
            no extra march). Pass a value (see ``config.settings.
            INSULATION_UPGRADE_R_M2K_W``) to also march each building at its
            *current* absorptance/emissivity but the stack's insulation
            layer swapped to this R-value, so the insulation lever can be
            compared against the roof-coating lever
            (:data:`_INSULATION_OUTPUT_COLUMNS`, from
            ``annual_benefit_insulation``). ``insulation_thickness_upgrade_m``
            pairs a thickness change with it (default: keep the stack's own
            thickness, i.e. assume a more resistive material at the same
            depth) -- see ``config.settings.INSULATION_UPGRADE_THICKNESS_M``
            to instead reproduce the reference notebook's own thicker-batt
            scenario.
        coolmax: Opt-in re-roof scenario -- a complete replacement of every
            building's existing roof (its own construction and current
            absorptance: tile, steel or slate) with a Colorbond Coolmax
            steel-deck roof (``COOLMAX_ABSORPTANCE``/``COOLMAX_EMISSIVITY``).
            Adds :data:`_COOLMAX_OUTPUT_COLUMNS` (existing − Coolmax).

    Every result carries ``roof_type`` (concrete / metal_light / metal_dark /
    slate / terracotta) and ``coating_type`` (which cool coating was applied,
    ``COOL_COATING_BY_STACK``) so the coating can be priced per building.

    Returns a DataFrame indexed like ``df`` with :data:`_OUTPUT_COLUMNS`
    (plus :data:`_INSULATION_OUTPUT_COLUMNS` when
    ``insulation_r_upgrade_m2k_w`` is given).
    """
    weather = build_hourly_weather(weather_df)

    if roof_stack is None:
        stacks = _load_default_stacks()
        group_key = df[roof_material_col].apply(stack_for_material) if roof_material_col in df.columns else pd.Series("metal", index=df.index)
    elif isinstance(roof_stack, RoofStack):
        stacks = {"_single": roof_stack}
        group_key = pd.Series("_single", index=df.index)
    else:
        stacks = roof_stack
        group_key = df[roof_material_col].apply(stack_for_material)

    absorptance_before_all = (
        pd.to_numeric(df[absorptance_col], errors="coerce")
        .fillna(1.0 - COOL_ROOF_ABSORPTANCE)  # unknown → conservative dark roof
        .to_numpy(float)
    )
    area_all = pd.to_numeric(df[area_col], errors="coerce").fillna(0.0).to_numpy(float)
    building_type_all = (
        df[building_type_col].tolist() if building_type_col in df.columns else [None] * len(df)
    )

    max_h_ext = float(np.max(weather.h_ext_w_m2k))
    # Coolmax re-roof scenario: a complete replacement, so every building's
    # existing roof (its own construction and current absorptance — tile,
    # steel or slate) is compared against a new Coolmax steel-deck roof. The
    # existing-roof flux is the group's "current" march; metal-roofed groups
    # march Coolmax in the same call, other groups need one extra steel march.
    coolmax_stack = (
        (stacks.get("metal") or load_roof_layers(ROOF_LAYERS_CSV)) if coolmax else None
    )
    coolmax_results = []

    results = []
    for key, stack in stacks.items():
        mask = (group_key == key).to_numpy()
        n = int(mask.sum())
        if n == 0:
            continue
        dt = resolve_timestep(stack, max_h_ext)
        absorptance = absorptance_before_all[mask]
        area = area_all[mask]
        building_type = [building_type_all[i] for i in np.where(mask)[0]]

        group_cool_absorptance = (
            float(cool_absorptance) if cool_absorptance is not None
            else cool_absorptance_for_stack(key)
        )
        current_r2 = float(stack.r_value_m2k_w[2])
        current_thickness2 = float(stack.thickness_m[2])
        upgrade_thickness = (
            float(insulation_thickness_upgrade_m)
            if insulation_thickness_upgrade_m is not None else current_thickness2
        )

        # One march for all scenarios, n columns each:
        # (absorptance, emissivity, insulation R, insulation thickness).
        scenarios = {
            "current": (absorptance, stack.emissivity, current_r2, current_thickness2),
            "cool": (group_cool_absorptance, stack.emissivity_cool, current_r2, current_thickness2),
        }
        if insulation_r_upgrade_m2k_w is not None:
            scenarios["insulation"] = (
                absorptance, stack.emissivity, float(insulation_r_upgrade_m2k_w), upgrade_thickness
            )
        reroof_in_group = coolmax and stack is coolmax_stack
        if reroof_in_group:
            scenarios["coolmax"] = (
                COOLMAX_ABSORPTANCE, COOLMAX_EMISSIVITY, current_r2, current_thickness2
            )

        def _stack_col(i: int) -> np.ndarray:
            return np.concatenate(
                [np.broadcast_to(np.asarray(v[i], dtype=float), (n,)) for v in scenarios.values()]
            )

        logger.info(
            "Marching %d '%s'-roof buildings × %d scenarios (%s) over %d h at dt=%.1f s "
            "(%d substeps/h)...",
            n, key, len(scenarios), ", ".join(scenarios),
            weather.n_hours, dt, int(np.ceil(3600.0 / dt)),
        )
        flux = march_interior_flux(
            weather, stack, _stack_col(0), dt_s=dt,
            emissivity=_stack_col(1),
            insulation_r_m2k_w=_stack_col(2),
            insulation_thickness_m=_stack_col(3),
        )
        cols = {name: flux[:, i * n:(i + 1) * n] for i, name in enumerate(scenarios)}
        group_result = annual_benefit(cols["current"], cols["cool"], weather, area, building_type)
        if "insulation" in cols:
            insulation_result = annual_benefit_insulation(
                cols["current"], cols["insulation"], weather, area, building_type
            )
            insulation_result.index = group_result.index
            group_result = pd.concat([group_result, insulation_result], axis=1)
        group_result.insert(0, "roof_construction", key)
        group_result.insert(1, "cool_absorptance_applied", group_cool_absorptance)
        group_result.insert(2, "cool_emissivity_applied", stack.emissivity_cool)
        group_result.index = df.index[mask]
        results.append(group_result)
        if coolmax:
            if reroof_in_group:
                flux_coolmax = cols["coolmax"]
            else:
                logger.info(
                    "Marching %d '%s'-roof buildings re-roofed in Coolmax steel...", n, key,
                )
                flux_coolmax = march_interior_flux(
                    weather, coolmax_stack, np.full(n, COOLMAX_ABSORPTANCE),
                    dt_s=resolve_timestep(coolmax_stack, max_h_ext),
                    emissivity=np.full(n, COOLMAX_EMISSIVITY),
                )
            coolmax_results.append(
                _coolmax_columns(cols["current"], flux_coolmax, weather, area,
                                 building_type, df.index[mask])
            )

    result = pd.concat(results).loc[df.index]
    if coolmax:
        result = pd.concat([result, pd.concat(coolmax_results).loc[df.index]], axis=1)

    material = df[roof_material_col] if roof_material_col in df.columns else [None] * len(df)
    result.insert(1, "roof_type", [
        roof_type_for(m, a) for m, a in zip(material, absorptance_before_all)
    ])
    result.insert(
        2, "coating_type",
        result["roof_construction"].map(COOL_COATING_BY_STACK).fillna("generic_cool_coating"),
    )
    return result


def hourly_scenario_flux(
    df: pd.DataFrame,
    weather_df: pd.DataFrame,
    *,
    id_col: str = "building_id",
    absorptance_col: str = "absorptance_before",
    area_col: str = "roof_surface_area_m2",
    roof_material_col: str = "roof_material",
    spin_up_hours: int = HEAT_INGRESS_SPINUP_HOURS,
    cdd_base_temp_c: float = CDD_BASE_TEMP,
) -> pd.DataFrame:
    """
    Hourly ceiling heat flux for every building under all three roof options.

    The same marches ``run_model`` rolls up to annual columns, kept hourly so
    the economics side can inspect them. One row per building per hour (long
    format), with:

    - ``flux_existing_wh_m2`` — the roof as it is now (own construction,
      current absorptance, uncoated). ``annual_benefit``'s "base".
    - ``flux_coated_wh_m2`` — the same roof with its roof-type cool coating
      (``COOL_ROOF_ABSORPTANCE_BY_STACK``). ``annual_benefit``'s "cool".
    - ``flux_coolmax_wh_m2`` — the roof replaced with Colorbond Coolmax steel
      (``COOLMAX_ABSORPTANCE``/``COOLMAX_EMISSIVITY``), as ``--coolmax``.
    - ``heat_*_kwh`` — each flux × ``roof_surface_area_m2`` / 1000, i.e. the
      building's hourly ceiling heat in kWh (thermal, before COP).

    Flux is per m² of roof: positive = heat into the room, negative = heat
    out of it. ``hvac_mode`` is the outdoor-temperature split ``annual_benefit``
    uses for the headline; ``spin_up`` marks the hours it discards. Summing
    ``flux_existing_wh_m2 - flux_coated_wh_m2`` over non-spin-up rows of one
    ``hvac_mode`` reproduces the annual thermal columns.
    """
    weather = build_hourly_weather(weather_df)
    stacks = _load_default_stacks()
    coolmax_stack = stacks["metal"]
    group_key = df[roof_material_col].apply(stack_for_material)
    absorptance_all = (
        pd.to_numeric(df[absorptance_col], errors="coerce")
        .fillna(1.0 - COOL_ROOF_ABSORPTANCE)  # unknown → conservative dark roof, as run_model
        .to_numpy(float)
    )
    max_h_ext = float(np.max(weather.h_ext_w_m2k))

    n_intervals = weather.n_hours - 1
    time = weather.time_melbourne[:n_intervals]
    temp = weather.outdoor_temp_c[:n_intervals]
    hvac_mode = np.where(temp >= cdd_base_temp_c, "cooling", "heating")
    spin_up = np.arange(n_intervals) < max(0, int(spin_up_hours))

    frames = []
    for key, stack in stacks.items():
        mask = (group_key == key).to_numpy()
        n = int(mask.sum())
        if n == 0:
            continue
        absorptance = absorptance_all[mask]
        coated = cool_absorptance_for_stack(key)
        flux = march_interior_flux(
            weather, stack,
            np.concatenate([absorptance, np.full(n, coated)]),
            dt_s=resolve_timestep(stack, max_h_ext),
            emissivity=np.concatenate(
                [np.full(n, stack.emissivity), np.full(n, stack.emissivity_cool)]
            ),
        )
        flux_coolmax = march_interior_flux(
            weather, coolmax_stack, np.full(n, COOLMAX_ABSORPTANCE),
            dt_s=resolve_timestep(coolmax_stack, max_h_ext),
            emissivity=np.full(n, COOLMAX_EMISSIVITY),
        )
        sub = df.loc[mask]
        area = pd.to_numeric(sub[area_col], errors="coerce").fillna(0.0).to_numpy(float)
        for j in range(n):
            existing, coated_f, coolmax_f = flux[:, j], flux[:, n + j], flux_coolmax[:, j]
            frames.append(pd.DataFrame({
                id_col: sub[id_col].iloc[j],
                "time_melbourne": time,
                "outdoor_temp_c": np.round(temp, 2),
                "hvac_mode": hvac_mode,
                "spin_up": spin_up,
                "roof_construction": key,
                "roof_surface_area_m2": area[j],
                "absorptance_existing": absorptance[j],
                "absorptance_coated": coated,
                "absorptance_coolmax": COOLMAX_ABSORPTANCE,
                "flux_existing_wh_m2": existing,
                "flux_coated_wh_m2": coated_f,
                "flux_coolmax_wh_m2": coolmax_f,
                "heat_existing_kwh": existing * area[j] / 1000.0,
                "heat_coated_kwh": coated_f * area[j] / 1000.0,
                "heat_coolmax_kwh": coolmax_f * area[j] / 1000.0,
            }))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
