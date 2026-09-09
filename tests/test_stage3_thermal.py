"""
Tests for stage3_thermal/thermal_calculator.py.

Covers:
- Zero saving when absorbed energy is zero (already cool roof in Stage 2)
- R_roof inference from building attributes (commercial, residential, metal, default)
- Heat-transfer fraction derived from R_roof via U/(U+h_out), and its monotonicity
- Wind-dependent h_out (McAdams correlation) and its fallback when wind is None
- Correct COP adjustment for commercial buildings
- Multistorey attenuation for tall (4+ storey) buildings
- Output columns all present
- Physics chain arithmetic is self-consistent
- Negative energy_saved_kwh_yr is clamped to zero
"""

import pytest

from stage3_thermal.thermal_calculator import (
    COOLING_FRACTION,
    H_OUT_WIND_INTERCEPT_W_M2K,
    H_OUT_WIND_SLOPE_W_M2K_PER_MS,
    H_OUTSIDE_W_M2K,
    HVAC_COP_COMMERCIAL,
    HVAC_COP_RESIDENTIAL,
    MULTISTOREY_ATTENUATION,
    R_ROOF_BY_CATEGORY,
    R_ROOF_DEFAULT,
    R_ROOF_METAL_RESIDENTIAL,
    _h_out_from_wind,
    _heat_fraction_from_r_roof,
    _normalize_label,
    _r_roof_for_building,
    calculate_thermal_benefit,
)


EXPECTED_KEYS = {
    "roof_r_value_m2k",
    "h_out_w_m2k",
    "heat_transfer_fraction",
    "heat_to_interior_kwh_yr",
    "cooling_load_reduction_kwh_yr",
    "electricity_saved_kwh_yr",
    "co2_electricity_saved_kg_yr",
}

# Base fraction for the default (residential, R2.5) building — the value that
# reproduces the previous single 0.016 constant.
_DEFAULT_FRACTION = _heat_fraction_from_r_roof(R_ROOF_DEFAULT)


class TestOutputKeys:
    def test_all_keys_present_default(self):
        result = calculate_thermal_benefit(energy_saved_kwh_yr=1000.0)
        assert EXPECTED_KEYS == set(result.keys())

    def test_all_keys_present_commercial(self):
        result = calculate_thermal_benefit(
            energy_saved_kwh_yr=500.0, building_type="commercial"
        )
        assert EXPECTED_KEYS == set(result.keys())


class TestZeroSaving:
    def test_zero_input_gives_zero_outputs(self):
        """A building that already meets cool roof spec has zero Stage 2 saving → zero Stage 3."""
        result = calculate_thermal_benefit(energy_saved_kwh_yr=0.0)
        assert result["heat_to_interior_kwh_yr"] == 0.0
        assert result["cooling_load_reduction_kwh_yr"] == 0.0
        assert result["electricity_saved_kwh_yr"] == 0.0
        assert result["co2_electricity_saved_kg_yr"] == 0.0

    def test_negative_input_clamped_to_zero(self):
        """Negative absorbed saving (shouldn't happen, but must not produce negative electricity)."""
        result = calculate_thermal_benefit(energy_saved_kwh_yr=-500.0)
        assert result["electricity_saved_kwh_yr"] == 0.0


class TestNormalizeLabel:
    def test_none_and_nan_become_empty(self):
        assert _normalize_label(None) == ""
        assert _normalize_label(float("nan")) == ""

    def test_lower_and_strip(self):
        assert _normalize_label("  Commercial ") == "commercial"
        assert _normalize_label("METAL_DARK") == "metal_dark"


class TestRRoofInference:
    def test_commercial_uses_commercial_r_roof(self):
        assert _r_roof_for_building("commercial", None) == R_ROOF_BY_CATEGORY["commercial"]
        assert _r_roof_for_building("warehouse", None) == R_ROOF_BY_CATEGORY["commercial"]

    def test_residential_uses_residential_r_roof(self):
        assert _r_roof_for_building("house", "terracotta") == R_ROOF_BY_CATEGORY["residential"]
        assert _r_roof_for_building("residential", "concrete_tile") == R_ROOF_BY_CATEGORY["residential"]

    def test_metal_residential_nudged_down(self):
        """Metal-roofed residential stock is treated as less insulated."""
        assert _r_roof_for_building("house", "metal") == R_ROOF_METAL_RESIDENTIAL
        assert _r_roof_for_building("house", "metal_dark") == R_ROOF_METAL_RESIDENTIAL

    def test_metal_nudge_does_not_leak_to_non_residential(self):
        """
        Regression: a metal roof only nudges *residential* stock down to R1.5.
        Non-residential, non-commercial tags (school, church, hospital) must fall
        to the default, not inherit the older-residential R-value.
        """
        assert _r_roof_for_building("school", "metal") == R_ROOF_DEFAULT
        assert _r_roof_for_building("church", "metal_dark") == R_ROOF_DEFAULT
        assert _r_roof_for_building("hospital", "corrugated_iron") == R_ROOF_DEFAULT

    def test_unknown_falls_back_to_default(self):
        assert _r_roof_for_building("", None) == R_ROOF_DEFAULT
        assert _r_roof_for_building("garage", None) == R_ROOF_DEFAULT

    def test_r_roof_surfaced_in_output(self):
        result = calculate_thermal_benefit(1000.0, building_type="commercial")
        assert result["roof_r_value_m2k"] == pytest.approx(R_ROOF_BY_CATEGORY["commercial"])


class TestHeatFractionFromRRoof:
    def test_default_fraction_matches_legacy_constant(self):
        """R2.5 default should reproduce the previous ~0.016 heat-transfer fraction."""
        assert _heat_fraction_from_r_roof(R_ROOF_DEFAULT) == pytest.approx(0.016, abs=0.001)

    def test_higher_r_roof_gives_lower_fraction(self):
        """Better insulation (higher R) → less heat reaches the interior."""
        assert _heat_fraction_from_r_roof(3.2) < _heat_fraction_from_r_roof(2.5)
        assert _heat_fraction_from_r_roof(2.5) < _heat_fraction_from_r_roof(0.5)

    def test_fraction_bounded(self):
        for r in (0.3, 0.5, 1.5, 2.5, 3.2, 5.0):
            frac = _heat_fraction_from_r_roof(r)
            assert 0.0 < frac < 1.0

    def test_zero_or_negative_r_roof_uses_default(self):
        assert _heat_fraction_from_r_roof(0.0) == pytest.approx(_DEFAULT_FRACTION)
        assert _heat_fraction_from_r_roof(-1.0) == pytest.approx(_DEFAULT_FRACTION)


class TestWindDependentHOut:
    def test_none_wind_uses_fixed_fallback(self):
        """No wind data → h_out reproduces the pre-wind-model fixed constant exactly."""
        assert _h_out_from_wind(None) == pytest.approx(H_OUTSIDE_W_M2K)

    def test_nan_wind_uses_fixed_fallback(self):
        assert _h_out_from_wind(float("nan")) == pytest.approx(H_OUTSIDE_W_M2K)

    def test_zero_wind_uses_intercept_only(self):
        assert _h_out_from_wind(0.0) == pytest.approx(H_OUT_WIND_INTERCEPT_W_M2K)

    def test_wind_arithmetic(self):
        v = 4.0
        expected = H_OUT_WIND_INTERCEPT_W_M2K + H_OUT_WIND_SLOPE_W_M2K_PER_MS * v
        assert _h_out_from_wind(v) == pytest.approx(expected)

    def test_negative_wind_clamped_to_zero(self):
        """Defensive: a bad negative wind reading shouldn't lower h_out below the intercept."""
        assert _h_out_from_wind(-2.0) == pytest.approx(H_OUT_WIND_INTERCEPT_W_M2K)

    def test_higher_wind_increases_h_out(self):
        assert _h_out_from_wind(1.0) < _h_out_from_wind(5.0) < _h_out_from_wind(10.0)

    def test_default_call_omits_wind_reproduces_legacy_behaviour(self):
        """calculate_thermal_benefit() with no wind_speed_ms arg must be unchanged
        from before wind support was added (default parameter value is None)."""
        result = calculate_thermal_benefit(energy_saved_kwh_yr=1000.0)
        assert result["h_out_w_m2k"] == pytest.approx(H_OUTSIDE_W_M2K)
        assert result["heat_transfer_fraction"] == pytest.approx(_DEFAULT_FRACTION, abs=0.0005)


class TestWindMonotonicityEndToEnd:
    def test_more_wind_means_less_heat_reaches_interior(self):
        """
        More wind -> higher h_out -> the roof sheds absorbed heat back outside more
        efficiently -> a SMALLER fraction conducts inward -> less cooling benefit.
        Holding R_roof fixed isolates the wind effect.
        """
        calm = calculate_thermal_benefit(1000.0, wind_speed_ms=0.5)
        windy = calculate_thermal_benefit(1000.0, wind_speed_ms=10.0)
        assert windy["h_out_w_m2k"] > calm["h_out_w_m2k"]
        assert windy["heat_transfer_fraction"] < calm["heat_transfer_fraction"]
        assert windy["electricity_saved_kwh_yr"] < calm["electricity_saved_kwh_yr"]


class TestResidentialPhysics:
    def test_physics_chain_arithmetic(self):
        """
        Manual walk-through with round numbers for the default residential (R2.5) case.
          heat_to_interior  = 1000 * fraction(R2.5)
          cooling_load      = heat_to_interior * 0.70
          electricity_saved = cooling_load / 3.0
        """
        result = calculate_thermal_benefit(energy_saved_kwh_yr=1000.0)
        assert result["heat_to_interior_kwh_yr"] == pytest.approx(
            1000.0 * _DEFAULT_FRACTION, abs=0.2
        )
        assert result["cooling_load_reduction_kwh_yr"] == pytest.approx(
            1000.0 * _DEFAULT_FRACTION * COOLING_FRACTION, abs=0.2
        )
        assert result["electricity_saved_kwh_yr"] == pytest.approx(
            1000.0 * _DEFAULT_FRACTION * COOLING_FRACTION / HVAC_COP_RESIDENTIAL,
            abs=0.2,
        )

    def test_electricity_less_than_absorbed_solar(self):
        """Electricity saving must always be less than absorbed solar saving."""
        result = calculate_thermal_benefit(energy_saved_kwh_yr=2000.0)
        assert result["electricity_saved_kwh_yr"] < 2000.0


class TestRRoofMonotonicityEndToEnd:
    def test_less_insulated_residential_saves_more(self):
        """
        Holding COP fixed (both residential), a metal roof (R1.5) is less insulated
        than a tiled roof (R2.5), so more heat reaches the interior and more
        electricity is saved.
        """
        tiled = calculate_thermal_benefit(
            1000.0, building_type="house", roof_material="terracotta"
        )
        metal = calculate_thermal_benefit(
            1000.0, building_type="house", roof_material="metal"
        )
        assert metal["electricity_saved_kwh_yr"] > tiled["electricity_saved_kwh_yr"]


class TestCommercialCopAdjustment:
    def test_commercial_cop_arithmetic(self):
        """Verify exact R_roof + COP used for commercial buildings."""
        result = calculate_thermal_benefit(
            energy_saved_kwh_yr=1000.0, building_type="commercial"
        )
        commercial_fraction = _heat_fraction_from_r_roof(R_ROOF_BY_CATEGORY["commercial"])
        expected_elec = (
            1000.0 * commercial_fraction * COOLING_FRACTION / HVAC_COP_COMMERCIAL
        )
        assert result["electricity_saved_kwh_yr"] == pytest.approx(expected_elec, abs=0.2)

    def test_case_insensitive_building_type(self):
        """Building type lookup must be case-insensitive."""
        lower = calculate_thermal_benefit(1000.0, building_type="commercial")
        upper = calculate_thermal_benefit(1000.0, building_type="COMMERCIAL")
        assert lower["electricity_saved_kwh_yr"] == upper["electricity_saved_kwh_yr"]


class TestMultistoreyHeatFraction:
    def test_4_storey_uses_reduced_fraction(self):
        """Buildings with 4+ levels get extra attenuation on the heat path."""
        low_rise = calculate_thermal_benefit(energy_saved_kwh_yr=1000.0, levels=2)
        high_rise = calculate_thermal_benefit(energy_saved_kwh_yr=1000.0, levels=4)
        assert high_rise["heat_to_interior_kwh_yr"] < low_rise["heat_to_interior_kwh_yr"]

    def test_4_storey_fraction_arithmetic(self):
        result = calculate_thermal_benefit(energy_saved_kwh_yr=1000.0, levels=5)
        assert result["heat_to_interior_kwh_yr"] == pytest.approx(
            1000.0 * _DEFAULT_FRACTION * MULTISTOREY_ATTENUATION, abs=0.2
        )

    def test_3_storey_uses_standard_fraction(self):
        result = calculate_thermal_benefit(energy_saved_kwh_yr=1000.0, levels=3)
        assert result["heat_to_interior_kwh_yr"] == pytest.approx(
            1000.0 * _DEFAULT_FRACTION, abs=0.2
        )


class TestCo2Chain:
    def test_co2_positive_when_electricity_positive(self):
        result = calculate_thermal_benefit(energy_saved_kwh_yr=500.0)
        assert result["co2_electricity_saved_kg_yr"] > 0.0

    def test_co2_zero_when_electricity_zero(self):
        result = calculate_thermal_benefit(energy_saved_kwh_yr=0.0)
        assert result["co2_electricity_saved_kg_yr"] == 0.0


class TestNoneInputs:
    def test_none_building_type_uses_residential_defaults(self):
        none_type = calculate_thermal_benefit(1000.0, building_type=None)
        # No building_type → default R_roof and residential COP.
        assert none_type["roof_r_value_m2k"] == pytest.approx(R_ROOF_DEFAULT)
        expected = 1000.0 * _DEFAULT_FRACTION * COOLING_FRACTION / HVAC_COP_RESIDENTIAL
        assert none_type["electricity_saved_kwh_yr"] == pytest.approx(expected, abs=0.2)

    def test_none_levels_treated_as_low_rise(self):
        explicit_1 = calculate_thermal_benefit(1000.0, levels=1)
        none_levels = calculate_thermal_benefit(1000.0, levels=None)
        assert explicit_1["electricity_saved_kwh_yr"] == none_levels["electricity_saved_kwh_yr"]
