"""
Cool roof delta calculator for the Raising Rooves pipeline.

Estimates annual energy savings and CO2 reduction from applying a cool roof
coating to each building in a suburb, based on:
  - Roof footprint area and pitch (from Stage 1)
  - Annual solar irradiance at the building's location (from irradiance loader)
  - Current roof absorptance (estimated from roof_colour or roof_material)
  - Post-treatment absorptance: the cool coating appropriate to the roof type
    (COOL_ROOF_ABSORPTANCE_BY_STACK — same lookup Stage 3 uses)

Physics note:
  The energy intercepted by a tilted surface equals GHI × horizontal_footprint_area
  (the footprint is the horizontal shadow of the roof). Pitch doesn't change the total
  intercepted energy — it only affects how that energy is distributed per unit of actual
  roof surface. So:
      energy_incident_kwh_yr  = annual_ghi_kwh_m2 × area_m2  (footprint, not surface)
      roof_surface_area_m2    = area_m2 / cos(pitch_rad)      (actual surface for costing)
      energy_saved_kwh_yr     = energy_incident × (absorptance_before − absorptance_after)
"""

import math

import pandas as pd

from config.settings import (
    COOL_ROOF_ABSORPTANCE,
    COOL_ROOF_ABSORPTANCE_BY_STACK,
    DEFAULT_ROOF_STACK,
    GRID_EMISSIONS_FACTOR_KG_KWH,
    ROOF_STACK_BY_MATERIAL,
)

# ── Absorptance lookup tables ─────────────────────────────────────────────────
# Sources: CSIRO cool roof research; NatHERS material library; AS/NZS 4859.1

# Colour is the primary estimator — more directly observable than material category.
# Values are solar absorptance (fraction of incident solar radiation absorbed, 0–1).
ABSORPTANCE_BY_COLOUR: dict[str | None, float] = {
    "white":      0.25,
    "light_grey": 0.50,
    "dark_grey":  0.85,
    "red":        0.75,
    "brown":      0.75,
    "blue":       0.80,
    "green":      0.75,
    "other":      0.75,
    None:         0.75,  # unknown — conservative mid-range
}

# Material used as fallback when colour is None or "other".
ABSORPTANCE_BY_MATERIAL: dict[str | None, float] = {
    "metal_light":    0.45,
    "metal_dark":     0.85,
    "terracotta":     0.75,
    "concrete_tile":  0.75,
    "other":          0.75,
    None:             0.75,
}


def _absorptance_from_labels(roof_colour: str | None, roof_material: str | None) -> float:
    """Return absorptance from OSM colour/material labels (used when no HSV estimate available)."""
    if roof_colour and roof_colour != "other":
        return ABSORPTANCE_BY_COLOUR.get(roof_colour, 0.75)
    return ABSORPTANCE_BY_MATERIAL.get(roof_material, 0.75)


def cool_absorptance_for_material(roof_material: str | None) -> float:
    """Post-coating solar absorptance for a roof_material (same mapping as Stage 3)."""
    label = "" if roof_material is None or pd.isna(roof_material) else str(roof_material).lower().strip()
    stack = ROOF_STACK_BY_MATERIAL.get(label, DEFAULT_ROOF_STACK)
    return COOL_ROOF_ABSORPTANCE_BY_STACK.get(stack, COOL_ROOF_ABSORPTANCE)


def calculate_building_benefit(
    area_m2: float,
    pitch_deg: float,
    annual_ghi_kwh_m2: float,
    roof_colour: str | None,
    roof_material: str | None,
    absorptance_estimate: float | None = None,
    absorptance_uncertainty: float | None = None,
) -> dict:
    """
    Calculate cool roof benefit for a single building.

    Args:
        area_m2: Roof footprint area in m² (from Stage 1).
        pitch_deg: Assumed or measured roof pitch in degrees.
        annual_ghi_kwh_m2: Annual global horizontal irradiance at this location (kWh/m²/yr).
        roof_colour: Colour string from Stage 1 (may be None).
        roof_material: Material string from Stage 1 (may be None).

    Returns:
        Dict with keys: absorptance_before, absorptance_after, roof_surface_area_m2,
        energy_incident_kwh_yr, energy_saved_kwh_yr, co2_saved_kg_yr.
    """
    pitch_rad = math.radians(max(0.0, min(pitch_deg, 89.0)))
    cos_pitch = math.cos(pitch_rad)

    roof_surface_area_m2 = area_m2 / cos_pitch if cos_pitch > 0 else area_m2

    # Energy incident on the horizontal footprint (= energy intercepted by roof)
    energy_incident_kwh_yr = annual_ghi_kwh_m2 * area_m2

    # Use HSV-derived estimate when available (direct measurement from pixels).
    # Fall back to label lookup for OSM-tagged buildings that bypassed the classifier.
    if absorptance_estimate is not None and absorptance_estimate > 0:
        absorptance_before = absorptance_estimate
    else:
        absorptance_before = _absorptance_from_labels(roof_colour, roof_material)

    absorptance_after = cool_absorptance_for_material(roof_material)
    energy_saved_kwh_yr = energy_incident_kwh_yr * (absorptance_before - absorptance_after)
    # Clamp: if absorptance_before < absorptance_after (already a cool roof), saving = 0
    energy_saved_kwh_yr = max(0.0, energy_saved_kwh_yr)

    co2_saved_kg_yr = energy_saved_kwh_yr * GRID_EMISSIONS_FACTOR_KG_KWH

    return {
        "absorptance_before": round(absorptance_before, 3),
        "absorptance_after": round(absorptance_after, 3),
        "roof_surface_area_m2": round(roof_surface_area_m2, 1),
        "energy_incident_kwh_yr": round(energy_incident_kwh_yr, 1),
        "energy_saved_kwh_yr": round(energy_saved_kwh_yr, 1),
        "co2_saved_kg_yr": round(co2_saved_kg_yr, 1),
    }
