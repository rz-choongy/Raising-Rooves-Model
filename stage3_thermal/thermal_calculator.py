"""
Stage 3 thermal calculator for the Raising Rooves pipeline.

Converts absorbed solar reduction (from Stage 2) into cooling electricity
savings using a three-step building thermal physics chain:

    energy_saved_absorbed (kWh/yr, from Stage 2)
        → heat_to_interior_kwh_yr     (roof thermal resistance / conductance)
        → cooling_load_reduction_kwh_yr  (fraction reaching the cooling system)
        → electricity_saved_kwh_yr    (HVAC coefficient of performance)

The roof-to-interior step derives its conductance PER BUILDING from an inferred
roof thermal resistance R_roof (m²·K/W), following the roof-only heat-ingress
framing in Maggie's model:

    U_roof   = 1 / R_roof
    fraction = U_roof / (U_roof + h_out)

h_out (the outdoor surface film coefficient) is itself wind-dependent when a
local BARRA2 wind speed is available (McAdams simple forced-convection
correlation); it falls back to the fixed H_OUTSIDE_W_M2K constant otherwise.
See H_OUT_WIND_INTERCEPT_W_M2K / H_OUT_WIND_SLOPE_W_M2K_PER_MS in
config/settings.py for the correlation and its citation.

Stage 1 provides no construction-age field, so R_roof is inferred from the
attributes we have (building_type, levels, roof_material). All parameter
defaults and their sources are documented in config/settings.py.
"""

import math

from config.settings import (
    COOLING_FRACTION,
    GRID_EMISSIONS_FACTOR_KG_KWH,
    H_OUT_WIND_INTERCEPT_W_M2K,
    H_OUT_WIND_SLOPE_W_M2K_PER_MS,
    H_OUTSIDE_W_M2K,
    HVAC_COP_COMMERCIAL,
    HVAC_COP_RESIDENTIAL,
    MULTISTOREY_ATTENUATION,
    R_ROOF_BY_CATEGORY,
    R_ROOF_DEFAULT,
    R_ROOF_METAL_RESIDENTIAL,
)

_CO2_FACTOR = GRID_EMISSIONS_FACTOR_KG_KWH

# Building types treated as commercial: higher HVAC COP *and* lower roof R-value.
# COP selection and R_roof selection currently use the same set; split them if
# they ever need to diverge (e.g. institutional stock).
_COMMERCIAL_TYPES = frozenset(
    {"commercial", "office", "retail", "industrial", "warehouse"}
)

# Building types treated as residential stock for R_roof selection. The metal-roof
# nudge below only applies to these — a metal-roofed school/church is NOT residential.
_RESIDENTIAL_TYPES = frozenset(
    {"residential", "house", "detached", "apartments", "yes"}
)

# roof_material labels (from Stage 1) that denote a metal roof.
_METAL_MATERIALS = frozenset({"metal", "metal_light", "metal_dark", "corrugated_iron"})


def _normalize_label(value: str | None) -> str:
    """Lower-case and strip an OSM label; treat None/NaN as an empty string."""
    if not value or isinstance(value, float):
        return ""
    return str(value).lower().strip()


def _r_roof_for_building(btype: str, roof_material: str | None) -> float:
    """
    Infer a roof thermal resistance R_roof (m²·K/W) from Stage 1 attributes.

    Priority: commercial category → residential (with a metal-roof nudge for
    likely older/less-insulated stock) → default. Non-residential, non-commercial
    tags (school, church, hospital, university, ...) and unknown fall to the
    default. Stage 1 has no construction-age field, so this is a documented
    proxy, not a measured value.

    Args:
        btype: Building type from Stage 1, already normalised via _normalize_label.
        roof_material: Roof material label from Stage 1 (may be None).

    Returns:
        R_roof in m²·K/W.
    """
    if btype in _COMMERCIAL_TYPES:
        return R_ROOF_BY_CATEGORY["commercial"]

    if btype in _RESIDENTIAL_TYPES:
        # Metal-roofed residential skews older / less-insulated.
        if _normalize_label(roof_material) in _METAL_MATERIALS:
            return R_ROOF_METAL_RESIDENTIAL
        return R_ROOF_BY_CATEGORY["residential"]

    return R_ROOF_DEFAULT


def _h_out_from_wind(wind_speed_ms: float | None) -> float:
    """
    Outdoor surface film coefficient h_out (W/m²K) as a function of local wind speed.

    Uses the McAdams (1954) simple forced-convection correlation for an
    exterior building surface (also used by EnergyPlus's "SimpleCombined"
    exterior convection algorithm):

        h_out = H_OUT_WIND_INTERCEPT_W_M2K + H_OUT_WIND_SLOPE_W_M2K_PER_MS * V

    More wind → higher h_out → the roof sheds absorbed heat back to the
    outside air more efficiently → a SMALLER fraction of it conducts inward
    (see _heat_fraction_from_r_roof). Falls back to the fixed H_OUTSIDE_W_M2K
    default (ISO 6946 still-air surface coefficient) when no wind speed is
    available — this reproduces the pre-wind-model behaviour exactly.

    Args:
        wind_speed_ms: Local 10 m wind speed in m/s (BARRA2 sfcWind), or None.

    Returns:
        h_out in W/m²K.
    """
    if wind_speed_ms is None or (isinstance(wind_speed_ms, float) and math.isnan(wind_speed_ms)):
        return H_OUTSIDE_W_M2K
    v = max(0.0, float(wind_speed_ms))
    return H_OUT_WIND_INTERCEPT_W_M2K + H_OUT_WIND_SLOPE_W_M2K_PER_MS * v


def _heat_fraction_from_r_roof(r_roof: float, h_out: float = H_OUTSIDE_W_M2K) -> float:
    """
    Fraction of the absorbed-solar delta that conducts to the interior.

    fraction = U_roof / (U_roof + h_out),  U_roof = 1 / R_roof.

    Args:
        r_roof: Roof thermal resistance in m²·K/W (must be > 0).
        h_out: Outdoor surface film coefficient in W/m²K (default: the fixed
            H_OUTSIDE_W_M2K fallback — pass a wind-derived value from
            _h_out_from_wind() to make this wind-dependent).

    Returns:
        Unitless heat-transfer fraction in (0, 1).
    """
    r = r_roof if r_roof and r_roof > 0 else R_ROOF_DEFAULT
    u_roof = 1.0 / r
    return u_roof / (u_roof + h_out)


def calculate_thermal_benefit(
    energy_saved_kwh_yr: float,
    roof_material: str | None = None,
    building_type: str | None = None,
    levels: int | None = None,
    wind_speed_ms: float | None = None,
) -> dict:
    """
    Convert absorbed solar reduction into cooling electricity savings.

    Takes the Stage 2 ``energy_saved_kwh_yr`` (absorbed solar delta due to cool
    roof treatment) and propagates it through:
      1. Roof-to-interior heat transfer (fraction derived from R_roof and h_out)
      2. Fraction driving active cooling demand (``COOLING_FRACTION``)
      3. HVAC efficiency (``HVAC_COP``)

    Building-type adjustments applied:
    - Commercial/office buildings: higher HVAC COP (4.0 vs 3.0).
    - R_roof inferred per building drives the heat-transfer fraction
      (lower R_roof → more heat reaches interior → more benefit).
    - 4+ storey buildings: extra attenuation multiplier on the heat path.

    Args:
        energy_saved_kwh_yr: Absorbed solar reduction from Stage 2 (kWh/yr).
            Zero or negative → all output columns are zero (no benefit).
        roof_material: Roof material tag from Stage 1. Used (with building_type)
            to infer R_roof — metal residential roofs are treated as less insulated.
        building_type: Building type string from Stage 1 (e.g. "residential",
            "commercial", "office"). Used to select HVAC COP and R_roof.
        levels: Number of building storeys from Stage 1. Used to apply the
            multistorey heat-path attenuation for tall buildings.
        wind_speed_ms: Local mean 10 m wind speed in m/s (BARRA2 sfcWind, from
            Stage 2's ``mean_wind_speed_ms`` column), or None. Drives h_out via
            _h_out_from_wind() — None reproduces the pre-wind-model behaviour
            (fixed H_OUTSIDE_W_M2K).

    Returns:
        Dict with keys:
            roof_r_value_m2k              (float, inferred R_roof)
            h_out_w_m2k                   (float, wind-derived or fallback outdoor
                                            surface coefficient)
            heat_transfer_fraction        (float, effective fraction incl. multistorey)
            heat_to_interior_kwh_yr       (float, rounded to 1 dp)
            cooling_load_reduction_kwh_yr (float, rounded to 1 dp)
            electricity_saved_kwh_yr      (float, rounded to 1 dp)
            co2_electricity_saved_kg_yr   (float, rounded to 1 dp)
    """
    # Clamp: no negative savings (already a cool roof produces zero in Stage 2,
    # but guard here in case of floating-point residuals)
    energy_saved = max(0.0, energy_saved_kwh_yr)

    # ── Parameter selection ───────────────────────────────────────────────────
    btype = _normalize_label(building_type)
    is_commercial = btype in _COMMERCIAL_TYPES

    hvac_cop = HVAC_COP_COMMERCIAL if is_commercial else HVAC_COP_RESIDENTIAL

    try:
        n_levels = int(levels) if levels is not None else 1
    except (ValueError, TypeError):
        n_levels = 1

    # Per-building roof insulation drives the base conductance fraction;
    # h_out (wind-dependent when available) sets the outdoor side of it.
    r_roof = _r_roof_for_building(btype, roof_material)
    h_out = _h_out_from_wind(wind_speed_ms)
    base_fraction = _heat_fraction_from_r_roof(r_roof, h_out)

    # Tall buildings have more thermal mass; less roof heat reaches occupants.
    multistorey = MULTISTOREY_ATTENUATION if n_levels >= 4 else 1.0
    heat_fraction = base_fraction * multistorey

    # ── Physics chain ─────────────────────────────────────────────────────────
    heat_to_interior = energy_saved * heat_fraction
    cooling_load_reduction = heat_to_interior * COOLING_FRACTION
    electricity_saved = cooling_load_reduction / hvac_cop
    co2_electricity_saved = electricity_saved * _CO2_FACTOR

    return {
        "roof_r_value_m2k": round(r_roof, 2),
        "h_out_w_m2k": round(h_out, 2),
        "heat_transfer_fraction": round(heat_fraction, 4),
        "heat_to_interior_kwh_yr": round(heat_to_interior, 1),
        "cooling_load_reduction_kwh_yr": round(cooling_load_reduction, 1),
        "electricity_saved_kwh_yr": round(electricity_saved, 1),
        "co2_electricity_saved_kg_yr": round(co2_electricity_saved, 1),
    }
