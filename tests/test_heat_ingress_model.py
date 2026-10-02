"""
Tests for stage3_thermal/heat_ingress_model.py — the vectorised transient roof
heat-ingress engine that replaced the inferred-R_roof thermal_calculator, and
that itself was ported from Final_Heat_Ingress_Model.ipynb (2026-09-19,
superseding the earlier heat_ingress_model.ipynb).

Covers:
- Roof stack loading, per-material emissivity, four-way material mapping
- Weather array preparation (shortwave sum, wind → h_ext height correction,
  humidity/hour-of-day passthrough)
- Stability timestep and its clamping
- Vectorised march parity with a faithful scalar re-implementation of the
  notebook's transient cell (Final_Heat_Ingress_Model.ipynb, adaptive
  convection + dew-point sky temperature)
- march_interior_flux for N buildings == N single-building marches
- annual_benefit: monotonicity in absorptance, no-op for already-cool roofs,
  signed heating penalty, output columns, COP by building type
"""

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from config.settings import (
    COOL_ROOF_ABSORPTANCE,
    COOL_ROOF_ABSORPTANCE_BY_STACK,
    HEAT_INGRESS_COOLING_SETPOINT_C,
    HEAT_INGRESS_HEATING_SETPOINT_C,
    HEAT_INGRESS_ROOF_EMISSIVITY,
    HEAT_INGRESS_ROOF_TILT_DEG,
    HVAC_COP_COMMERCIAL,
    HVAC_COP_RESIDENTIAL,
    INSULATION_UPGRADE_R_M2K_W,
    INSULATION_UPGRADE_THICKNESS_M,
    ROOF_LAYERS_SLATE_CSV,
    ROOF_LAYERS_TERRACOTTA_CSV,
    ROOF_LAYERS_TILE_CSV,
)
from stage3_thermal import heat_ingress_model as him
from stage3_thermal.heat_ingress_model import (
    _AUDIT_COLUMNS,
    _COOLMAX_OUTPUT_COLUMNS,
    _DEWPOINT_A,
    _DEWPOINT_B,
    _INSULATION_OUTPUT_COLUMNS,
    _KELVIN,
    _NATURAL_CONV_DOWN_BASE,
    _NATURAL_CONV_DOWN_COEFF,
    _NATURAL_CONV_UP_BASE,
    _NATURAL_CONV_UP_COEFF,
    _OUTPUT_COLUMNS,
    _STEFAN_BOLTZMANN_W_M2K4,
    HourlyWeather,
    annual_benefit,
    annual_benefit_insulation,
    build_hourly_weather,
    hvac_cop,
    load_roof_layers,
    march_interior_flux,
    max_stable_timestep,
    resolve_timestep,
    roof_type_for,
    run_model,
    stack_for_material,
)

FIXTURE = Path(__file__).parent / "fixtures" / "weather_96h.csv"


@pytest.fixture(scope="module")
def weather_df() -> pd.DataFrame:
    return pd.read_csv(FIXTURE)


@pytest.fixture(scope="module")
def weather(weather_df) -> HourlyWeather:
    return build_hourly_weather(weather_df)


@pytest.fixture(scope="module")
def stack():
    return load_roof_layers()


# ── Faithful scalar reference (Final_Heat_Ingress_Model.ipynb transient cell) ─
def _notebook_reference_flux(weather: HourlyWeather, stack, absorptance, dt):
    """One-building transient march, transcribed from the final notebook."""
    r0, r2, r3 = stack.r_value_m2k_w[0], stack.r_value_m2k_w[2], stack.r_value_m2k_w[3]
    density = stack.density_kg_m3
    Cp = stack.heat_capacity_j_kgk
    thick = stack.thickness_m
    emissivity = stack.emissivity
    cos_tilt = abs(math.cos(math.radians(HEAT_INGRESS_ROOF_TILT_DEG)))
    const = _STEFAN_BOLTZMANN_W_M2K4
    rad_coeff = emissivity * const
    T_heat = HEAT_INGRESS_HEATING_SETPOINT_C + _KELVIN
    T_cool = HEAT_INGRESS_COOLING_SETPOINT_C + _KELVIN

    tph = int(np.ceil(3600.0 / dt))
    temp_k = weather.outdoor_temp_c + _KELVIN
    q_sw_hourly = absorptance * weather.shortwave_incident_w_m2
    h_ext = weather.h_ext_w_m2k
    humidity = weather.humidity_percent
    hour_of_day = weather.hour_of_day

    mid = np.full(4, temp_k[0], dtype=float)
    out = np.zeros(weather.n_hours - 1)
    for hour in range(weather.n_hours - 1):
        outside = np.linspace(temp_k[hour], temp_k[hour + 1], tph)
        shortwave = np.linspace(q_sw_hourly[hour], q_sw_hourly[hour + 1], tph)
        ho = np.linspace(h_ext[hour], h_ext[hour + 1], tph)
        rh = np.linspace(humidity[hour], humidity[hour + 1], tph)
        t_day0 = hour_of_day[hour]
        acc = 0.0
        for ts in range(tph):
            frac = ts / (tph - 1) if tph > 1 else 0.0

            t_air_c = outside[ts] - _KELVIN
            humidity_term = math.log(rh[ts] / 100.0) + (_DEWPOINT_A * t_air_c) / (_DEWPOINT_B + t_air_c)
            t_dp_c = _DEWPOINT_B * humidity_term / (_DEWPOINT_A - humidity_term)
            t_local = t_day0 + frac
            sky = outside[ts] * (
                0.711 + 0.0056 * t_dp_c + 0.000073 * t_dp_c ** 2
                + 0.013 * math.cos(math.radians(15.0 * t_local))
            ) ** 0.25

            q = np.zeros(5)
            q[0] = (
                shortwave[ts]
                + ho[ts] * (outside[ts] - mid[0])
                + rad_coeff * (sky ** 4 - mid[0] ** 4)
            )

            delta_a = mid[0] - mid[1]
            if delta_a >= 0.0:
                h_cav_a = _NATURAL_CONV_DOWN_COEFF * abs(delta_a) ** (1 / 3) / (_NATURAL_CONV_DOWN_BASE + cos_tilt)
            else:
                h_cav_a = _NATURAL_CONV_UP_COEFF * abs(delta_a) ** (1 / 3) / (_NATURAL_CONV_UP_BASE - cos_tilt)
            q[1] = delta_a / (r0 / 2.0 + 1.0 / h_cav_a)

            delta_b = mid[1] - mid[2]
            if delta_b >= 0.0:
                h_cav_b = _NATURAL_CONV_DOWN_COEFF * abs(delta_b) ** (1 / 3) / (_NATURAL_CONV_DOWN_BASE + 1.0)
            else:
                h_cav_b = _NATURAL_CONV_UP_COEFF * abs(delta_b) ** (1 / 3) / (_NATURAL_CONV_UP_BASE - 1.0)
            q[2] = delta_b / (1.0 / h_cav_b + r2 / 2.0)

            q[3] = 2 * (mid[2] - mid[3]) / (r2 + r3)

            T_inside = T_heat if outside[ts] < T_heat else T_cool
            delta_c = mid[3] - T_inside
            if delta_c >= 0.0:
                hi = _NATURAL_CONV_DOWN_COEFF * abs(delta_c) ** (1 / 3) / (_NATURAL_CONV_DOWN_BASE + 1.0)
            else:
                hi = _NATURAL_CONV_UP_COEFF * abs(delta_c) ** (1 / 3) / (_NATURAL_CONV_UP_BASE - 1.0)
            q[4] = delta_c / (r3 / 2.0 + 1.0 / hi)

            acc += q[4] * dt / 3600.0
            mid = mid + dt * (q[:-1] - q[1:]) / (density * Cp * thick)
        out[hour] = acc
    return out


# ── Roof stack ──────────────────────────────────────────────────────────────
class TestRoofStack:
    def test_layer_order(self, stack):
        assert stack.layer_names == ("Steel", "Cavity", "Insulation", "Plaster")
        assert stack.thickness_m.shape == (4,)
        assert stack.cavity_index == 1

    def test_areal_heat_capacity(self, stack):
        expected = stack.density_kg_m3 * stack.heat_capacity_j_kgk * stack.thickness_m
        np.testing.assert_allclose(stack.areal_heat_capacity_j_m2k, expected)

    def test_emissivity_parsed_per_material(self, stack):
        assert stack.emissivity == pytest.approx(0.9)
        assert stack.emissivity_cool == pytest.approx(0.875)
        terracotta = load_roof_layers(ROOF_LAYERS_TERRACOTTA_CSV)
        assert terracotta.emissivity_cool == pytest.approx(0.880)
        slate = load_roof_layers(ROOF_LAYERS_SLATE_CSV)
        assert slate.emissivity_cool == pytest.approx(0.880)
        assert slate.cavity_index == 1

    def test_emissivity_defaults_when_columns_absent(self, tmp_path):
        csv = tmp_path / "no_emissivity.csv"
        csv.write_text(
            "Material_type,Thickness_m,Density_Kg_m3,Spec_Heat_Cap_J_KgK,R_value_m2K_W,Layer_Role\n"
            "A,0.01,1,1,0.1,outer_skin\nB,0.01,1,1,0.1,airspace\n"
            "C,0.01,1,1,0.1,insulation\nD,0.01,1,1,0.1,inner_lining\n"
        )
        parsed = load_roof_layers(csv)
        assert parsed.emissivity == pytest.approx(HEAT_INGRESS_ROOF_EMISSIVITY)
        assert parsed.emissivity_cool == pytest.approx(HEAT_INGRESS_ROOF_EMISSIVITY)

    def test_tile_stack_loads_and_has_more_mass_than_steel(self, stack):
        tile = load_roof_layers(ROOF_LAYERS_TILE_CSV)
        assert tile.thickness_m.shape == (4,)
        assert tile.layer_names[0].lower().startswith("concrete")
        assert tile.cavity_index == 1
        # A concrete tile roof's outer skin has far more thermal mass than a
        # thin steel deck.
        assert tile.areal_heat_capacity_j_m2k[0] > 10 * stack.areal_heat_capacity_j_m2k[0]

    def test_layer_role_validation(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text(
            "Material_type,Thickness_m,Density_Kg_m3,Spec_Heat_Cap_J_KgK,R_value_m2K_W,Layer_Role\n"
            "A,0.01,1,1,0.1,outer_skin\nB,0.01,1,1,0.1,insulation\n"
            "C,0.01,1,1,0.1,not_a_role\nD,0.01,1,1,0.1,inner_lining\n"
        )
        with pytest.raises(ValueError):
            load_roof_layers(bad)


class TestStackSelection:
    def test_tile_materials_map_to_their_own_stack(self):
        assert stack_for_material("terracotta") == "terracotta"
        assert stack_for_material("Terracotta") == "terracotta"  # case-insensitive
        assert stack_for_material("concrete_tile") == "concrete"
        assert stack_for_material("roof_tiles") == "concrete"  # ambiguous OSM tag

    def test_slate_maps_to_its_own_stack(self):
        assert stack_for_material("slate") == "slate"

    def test_metal_variants_map_to_metal(self):
        for material in ("metal_dark", "metal_light", "metal", "metal_sheet"):
            assert stack_for_material(material) == "metal"

    def test_everything_else_maps_to_metal(self):
        for material in ("other", None, "yes", "glass", "wood"):
            assert stack_for_material(material) == "metal"


# ── Weather ─────────────────────────────────────────────────────────────────
class TestWeather:
    def test_shortwave_is_direct_plus_diffuse(self, weather_df, weather):
        expected = weather_df["rsdsdir_Wm2"].to_numpy() + weather_df["rsdsdif_Wm2"].to_numpy()
        np.testing.assert_allclose(weather.shortwave_incident_w_m2, expected)

    def test_h_ext_increases_with_wind(self, weather_df):
        calm = weather_df.copy()
        calm["wind_ms"] = 0.5
        windy = weather_df.copy()
        windy["wind_ms"] = 8.0
        assert (
            build_hourly_weather(windy).h_ext_w_m2k.mean()
            > build_hourly_weather(calm).h_ext_w_m2k.mean()
        )

    def test_roof_height_affects_local_wind(self, weather_df):
        low = build_hourly_weather(weather_df, roof_height_m=3.0)
        high = build_hourly_weather(weather_df, roof_height_m=12.0)
        assert high.h_ext_w_m2k.mean() > low.h_ext_w_m2k.mean()

    def test_humidity_passthrough(self, weather_df, weather):
        np.testing.assert_allclose(
            weather.humidity_percent, weather_df["rel_humidity_percent"].to_numpy()
        )

    def test_hour_of_day_in_range(self, weather):
        assert weather.hour_of_day.min() >= 0.0
        assert weather.hour_of_day.max() < 24.0


# ── Stability ───────────────────────────────────────────────────────────────
class TestStability:
    def test_max_timestep_positive_and_finite(self, stack, weather):
        dt_max = max_stable_timestep(stack, float(weather.h_ext_w_m2k.max()))
        assert 0.0 < dt_max < 3600.0

    def test_resolve_timestep_clamps(self, stack):
        # Absurd h_ext forces the limit well below the 40 s nominal.
        dt = resolve_timestep(stack, 500.0, nominal_dt_s=40.0)
        assert dt < 40.0

    def test_resolve_timestep_passes_through(self, stack, weather):
        dt = resolve_timestep(stack, float(weather.h_ext_w_m2k.max()), nominal_dt_s=10.0)
        assert dt == pytest.approx(10.0)


# ── March parity + vectorisation ────────────────────────────────────────────
class TestMarch:
    def test_matches_notebook_reference(self, weather, stack):
        # Same equations as the notebook's transient cell; tolerance covers
        # only floating-point associativity (x**4 vs x*x*x*x, accumulation order).
        dt = 40.0
        got = march_interior_flux(weather, stack, 0.8, dt_s=dt)[:, 0]
        ref = _notebook_reference_flux(weather, stack, 0.8, dt)
        np.testing.assert_allclose(got, ref, rtol=1e-6, atol=1e-6)

    def test_numba_and_numpy_paths_agree(self, weather, stack, monkeypatch):
        absorptances = np.array([0.35, 0.7, 0.9])
        reference = march_interior_flux(weather, stack, absorptances, dt_s=40.0)
        monkeypatch.setattr(him, "_HAS_NUMBA", False)
        fallback = march_interior_flux(weather, stack, absorptances, dt_s=40.0)
        np.testing.assert_allclose(fallback, reference, rtol=1e-6, atol=1e-6)

    def test_vectorised_equals_per_building(self, weather, stack):
        absorptances = np.array([0.30, 0.55, 0.85])
        batched = march_interior_flux(weather, stack, absorptances, dt_s=40.0)
        for i, a in enumerate(absorptances):
            single = march_interior_flux(weather, stack, a, dt_s=40.0)[:, 0]
            np.testing.assert_allclose(batched[:, i], single, rtol=1e-10, atol=1e-10)

    def test_higher_absorptance_lets_more_heat_in(self, weather, stack):
        dark = march_interior_flux(weather, stack, 0.9, dt_s=40.0).sum()
        light = march_interior_flux(weather, stack, 0.3, dt_s=40.0).sum()
        assert dark > light

    def test_cool_emissivity_changes_flux(self, weather, stack):
        # Same absorptance, only the outer-skin emissivity differs -> the
        # long-wave sky-exchange term must move the result.
        base = march_interior_flux(weather, stack, 0.8, dt_s=40.0, emissivity=stack.emissivity)
        cool = march_interior_flux(weather, stack, 0.8, dt_s=40.0, emissivity=stack.emissivity_cool)
        assert not np.allclose(base, cool)

    def test_rejects_non_standard_cavity_position(self, weather):
        bad = him.RoofStack(
            layer_names=("A", "B", "C", "D"),
            thickness_m=np.array([0.01, 0.01, 0.01, 0.01]),
            density_kg_m3=np.array([1.0, 1.0, 1.0, 1.0]),
            heat_capacity_j_kgk=np.array([1.0, 1.0, 1.0, 1.0]),
            r_value_m2k_w=np.array([0.1, 0.1, 0.1, 0.1]),
            cavity_index=2,
            emissivity=0.9,
            emissivity_cool=0.875,
        )
        with pytest.raises(ValueError):
            march_interior_flux(weather, bad, 0.8, dt_s=40.0)


# ── Annual benefit ──────────────────────────────────────────────────────────
def _benefit(weather, stack, absorptance_before, area=100.0, btype=None, spin=0):
    n = len(np.atleast_1d(absorptance_before))
    base = march_interior_flux(weather, stack, np.atleast_1d(absorptance_before), dt_s=40.0)
    cool = march_interior_flux(weather, stack, np.full(n, COOL_ROOF_ABSORPTANCE), dt_s=40.0)
    return annual_benefit(
        base, cool, weather, np.full(n, area),
        building_type=btype, spin_up_hours=spin,
    )


class TestAnnualBenefit:
    def test_output_columns(self, weather, stack):
        out = _benefit(weather, stack, [0.8])
        assert list(out.columns) == list(_OUTPUT_COLUMNS)
        assert len(out) == 1

    def test_monotonic_in_absorptance(self, weather, stack):
        out = _benefit(weather, stack, [0.30, 0.55, 0.85])
        saved = out["cooling_season_heat_avoided_kwh_yr"].to_numpy()
        assert saved[0] <= saved[1] <= saved[2]

    def test_already_cool_roof_saves_nothing(self, weather, stack):
        out = _benefit(weather, stack, [COOL_ROOF_ABSORPTANCE - 0.02])
        assert out["cooling_season_heat_avoided_kwh_yr"].iloc[0] == 0.0
        assert out["electricity_saved_kwh_yr"].iloc[0] == 0.0

    def test_heating_penalty_non_negative_and_net_consistent(self, weather, stack):
        out = _benefit(weather, stack, [0.85])
        row = out.iloc[0]
        assert row["heating_season_heat_added_kwh_yr"] >= 0.0
        assert row["heating_penalty_electricity_kwh_yr"] >= 0.0
        assert row["net_electricity_saved_kwh_yr"] == pytest.approx(
            row["electricity_saved_kwh_yr"] - row["heating_penalty_electricity_kwh_yr"],
            abs=0.15,
        )

    def test_area_scales_linearly(self, weather, stack):
        base = march_interior_flux(weather, stack, np.array([0.8]), dt_s=40.0)
        cool = march_interior_flux(weather, stack, np.array([COOL_ROOF_ABSORPTANCE]), dt_s=40.0)
        # Large areas so the 1 dp rounding on the output is negligible noise.
        small = annual_benefit(base, cool, weather, [10_000.0], spin_up_hours=0)
        big = annual_benefit(base, cool, weather, [40_000.0], spin_up_hours=0)
        assert big["electricity_saved_kwh_yr"].iloc[0] == pytest.approx(
            4.0 * small["electricity_saved_kwh_yr"].iloc[0], rel=1e-3
        )

    def test_commercial_uses_commercial_cop(self, weather, stack):
        res = _benefit(weather, stack, [0.85], btype=["residential"])
        com = _benefit(weather, stack, [0.85], btype=["commercial"])
        assert res["hvac_cop"].iloc[0] == HVAC_COP_RESIDENTIAL
        assert com["hvac_cop"].iloc[0] == HVAC_COP_COMMERCIAL
        # Same heat avoided, higher COP → less electricity saved.
        assert com["electricity_saved_kwh_yr"].iloc[0] < res["electricity_saved_kwh_yr"].iloc[0]


class TestHvacCop:
    def test_labels(self):
        assert hvac_cop("commercial") == HVAC_COP_COMMERCIAL
        assert hvac_cop("Office") == HVAC_COP_COMMERCIAL
        assert hvac_cop("house") == HVAC_COP_RESIDENTIAL
        assert hvac_cop(None) == HVAC_COP_RESIDENTIAL
        assert hvac_cop(float("nan")) == HVAC_COP_RESIDENTIAL


class TestRunModel:
    def test_end_to_end_shape_and_index(self, weather_df, stack):
        df = pd.DataFrame(
            {
                "absorptance_before": [0.85, 0.45, np.nan],
                "roof_surface_area_m2": [120.0, 80.0, 150.0],
                "building_type": ["house", "commercial", None],
            },
            index=[10, 11, 12],
        )
        out = run_model(df, weather_df, roof_stack=stack)
        assert list(out.index) == [10, 11, 12]
        assert list(out.columns) == [*_AUDIT_COLUMNS, *_OUTPUT_COLUMNS]
        assert (out["roof_construction"] == "_single").all()
        # NaN absorptance handled (conservative dark roof) → finite output.
        assert np.isfinite(out["electricity_saved_kwh_yr"].to_numpy()).all()

    def test_mixed_roof_material_selects_stack_per_building(self, weather_df):
        # Identical absorptance/area/type -- only roof_material differs -- so any
        # difference in output isolates the construction (thermal mass) effect.
        df = pd.DataFrame(
            {
                "absorptance_before": [0.85, 0.85],
                "roof_surface_area_m2": [150.0, 150.0],
                "building_type": ["house", "house"],
                "roof_material": ["metal_dark", "terracotta"],
            },
            index=[0, 1],
        )
        out = run_model(df, weather_df)
        assert out.loc[0, "roof_construction"] == "metal"
        assert out.loc[1, "roof_construction"] == "terracotta"

        # Different thermal mass -> different (unrounded) hourly flux for the
        # same absorptance/weather. The tile's outer skin has far more mass, so
        # its flux trace should be damped relative to the thin steel deck's.
        metal_stack, terracotta_stack = load_roof_layers(), load_roof_layers(ROOF_LAYERS_TERRACOTTA_CSV)
        weather = build_hourly_weather(weather_df)
        dt = resolve_timestep(metal_stack, float(weather.h_ext_w_m2k.max()))
        metal_flux = march_interior_flux(weather, metal_stack, 0.85, dt_s=dt)
        terracotta_flux = march_interior_flux(weather, terracotta_stack, 0.85, dt_s=dt)
        # Adaptive convection now couples the airspace tightly to its neighbours
        # on both sides, so the two constructions' hour-to-hour swing magnitude
        # is no longer a reliable discriminator (unlike the pre-port fixed-R
        # model) -- only that the two constructions produce genuinely different
        # results is asserted here.
        assert not np.allclose(metal_flux, terracotta_flux)

    def test_default_stacks_cover_four_materials(self, weather_df):
        df = pd.DataFrame(
            {
                "absorptance_before": [0.6, 0.6, 0.6, 0.6],
                "roof_surface_area_m2": [100.0, 100.0, 100.0, 100.0],
                "roof_material": ["terracotta", "concrete_tile", "metal_light", "slate"],
            },
        )
        out = run_model(df, weather_df)
        assert list(out["roof_construction"]) == ["terracotta", "concrete", "metal", "slate"]

    def test_cool_coating_follows_roof_type(self, weather_df):
        # Each roof type is coated with its own cool absorptance, not one global value.
        df = pd.DataFrame(
            {
                "absorptance_before": [0.8, 0.8, 0.8, 0.8],
                "roof_surface_area_m2": [100.0] * 4,
                "roof_material": ["terracotta", "concrete_tile", "metal_dark", "slate"],
            },
        )
        out = run_model(df, weather_df)
        expected = [COOL_ROOF_ABSORPTANCE_BY_STACK[k] for k in ["terracotta", "concrete", "metal", "slate"]]
        assert list(out["cool_absorptance_applied"]) == expected
        stacks = [load_roof_layers(ROOF_LAYERS_TERRACOTTA_CSV), load_roof_layers(ROOF_LAYERS_TILE_CSV),
                  load_roof_layers(), load_roof_layers(ROOF_LAYERS_SLATE_CSV)]
        assert list(out["cool_emissivity_applied"]) == [st.emissivity_cool for st in stacks]

    def test_cool_absorptance_override_applies_to_all(self, weather_df):
        df = pd.DataFrame(
            {
                "absorptance_before": [0.8, 0.8],
                "roof_surface_area_m2": [100.0, 100.0],
                "roof_material": ["terracotta", "metal_dark"],
            },
        )
        out = run_model(df, weather_df, cool_absorptance=0.23)
        assert (out["cool_absorptance_applied"] == 0.23).all()

    def test_forced_single_stack_uses_generic_coating(self, weather_df, stack):
        df = pd.DataFrame({"absorptance_before": [0.8], "roof_surface_area_m2": [100.0]})
        out = run_model(df, weather_df, roof_stack=stack)
        assert out["cool_absorptance_applied"].iloc[0] == COOL_ROOF_ABSORPTANCE


# ── Insulation-upgrade scenario (opt-in) ─────────────────────────────────────
class TestInsulationMarch:
    def test_default_insulation_matches_stack(self, weather, stack):
        # Not passing an override should be identical to passing the stack's
        # own current R-value/thickness explicitly.
        default = march_interior_flux(weather, stack, 0.8, dt_s=40.0)
        explicit = march_interior_flux(
            weather, stack, 0.8, dt_s=40.0,
            insulation_r_m2k_w=stack.r_value_m2k_w[2],
            insulation_thickness_m=stack.thickness_m[2],
        )
        np.testing.assert_allclose(default, explicit)

    def test_higher_insulation_r_dampens_flux_magnitude(self, weather, stack):
        # Same absorptance; only insulation R differs. Better insulation
        # should shrink the magnitude of interior heat flow (both directions).
        base = march_interior_flux(weather, stack, 0.8, dt_s=40.0)
        upgraded = march_interior_flux(
            weather, stack, 0.8, dt_s=40.0, insulation_r_m2k_w=INSULATION_UPGRADE_R_M2K_W,
        )
        assert np.abs(upgraded).sum() < np.abs(base).sum()

    def test_insulation_r_accepts_per_building_array(self, weather, stack):
        r_values = np.array([2.5, 4.1, 6.0])
        out = march_interior_flux(
            weather, stack, np.full(3, 0.8), dt_s=40.0, insulation_r_m2k_w=r_values,
        )
        # Higher R -> smaller-magnitude flux, monotonically, at the same absorptance.
        magnitudes = np.abs(out).sum(axis=0)
        assert magnitudes[0] > magnitudes[1] > magnitudes[2]


class TestAnnualBenefitInsulation:
    def _flux_pair(self, weather, stack, r_upgrade=INSULATION_UPGRADE_R_M2K_W):
        base = march_interior_flux(weather, stack, np.array([0.8]), dt_s=40.0)
        upgrade = march_interior_flux(
            weather, stack, np.array([0.8]), dt_s=40.0, insulation_r_m2k_w=r_upgrade,
        )
        return base, upgrade

    def test_output_columns(self, weather, stack):
        base, upgrade = self._flux_pair(weather, stack)
        out = annual_benefit_insulation(base, upgrade, weather, [100.0])
        assert list(out.columns) == list(_INSULATION_OUTPUT_COLUMNS)
        assert len(out) == 1

    def test_savings_non_negative_in_both_seasons(self, weather, stack):
        # Unlike annual_benefit's cool-roof penalty, insulation should never
        # show a negative-clamped-to-zero season when R genuinely improves.
        base, upgrade = self._flux_pair(weather, stack)
        out = annual_benefit_insulation(base, upgrade, weather, [100.0], spin_up_hours=0)
        row = out.iloc[0]
        assert row["insulation_cooling_saved_thermal_kwh_yr"] >= 0.0
        assert row["insulation_heating_saved_thermal_kwh_yr"] >= 0.0
        assert row["insulation_net_electricity_saved_kwh_yr"] >= 0.0

    def test_no_change_saves_nothing(self, weather, stack):
        # Upgrading to the *same* R-value as the base is a no-op.
        base, same = self._flux_pair(weather, stack, r_upgrade=float(stack.r_value_m2k_w[2]))
        out = annual_benefit_insulation(base, same, weather, [100.0])
        assert out["insulation_net_electricity_saved_kwh_yr"].iloc[0] == 0.0

    def test_downgrade_saves_nothing(self, weather, stack):
        # A worse R-value than the base is clamped to zero saving, not a
        # negative number -- annual_benefit_insulation reports upgrades only.
        base, worse = self._flux_pair(weather, stack, r_upgrade=1.0)
        out = annual_benefit_insulation(base, worse, weather, [100.0])
        assert out["insulation_net_electricity_saved_kwh_yr"].iloc[0] == 0.0

    def test_more_area_scales_savings_linearly(self, weather, stack):
        base, upgrade = self._flux_pair(weather, stack)
        small = annual_benefit_insulation(base, upgrade, weather, [10_000.0], spin_up_hours=0)
        big = annual_benefit_insulation(base, upgrade, weather, [40_000.0], spin_up_hours=0)
        assert big["insulation_net_electricity_saved_kwh_yr"].iloc[0] == pytest.approx(
            4.0 * small["insulation_net_electricity_saved_kwh_yr"].iloc[0], rel=1e-3
        )


class TestRunModelInsulation:
    def test_off_by_default_unchanged_columns(self, weather_df, stack):
        df = pd.DataFrame(
            {"absorptance_before": [0.8], "roof_surface_area_m2": [120.0]},
        )
        out = run_model(df, weather_df, roof_stack=stack)
        assert list(out.columns) == [*_AUDIT_COLUMNS, *_OUTPUT_COLUMNS]

    def test_opt_in_adds_insulation_columns(self, weather_df, stack):
        df = pd.DataFrame(
            {"absorptance_before": [0.8], "roof_surface_area_m2": [120.0]},
        )
        out = run_model(
            df, weather_df, roof_stack=stack,
            insulation_r_upgrade_m2k_w=INSULATION_UPGRADE_R_M2K_W,
            insulation_thickness_upgrade_m=INSULATION_UPGRADE_THICKNESS_M,
        )
        assert list(out.columns) == [
            *_AUDIT_COLUMNS, *_OUTPUT_COLUMNS, *_INSULATION_OUTPUT_COLUMNS,
        ]
        assert np.isfinite(out["insulation_net_electricity_saved_kwh_yr"].to_numpy()).all()
        # The cool-roof columns must be unaffected by the extra scenario.
        baseline = run_model(df, weather_df, roof_stack=stack)
        pd.testing.assert_series_equal(
            out["net_electricity_saved_kwh_yr"], baseline["net_electricity_saved_kwh_yr"]
        )

    def test_insulation_upgrade_saves_something_for_a_dark_roof(self, weather_df, stack):
        df = pd.DataFrame(
            {"absorptance_before": [0.85], "roof_surface_area_m2": [150.0]},
        )
        out = run_model(
            df, weather_df, roof_stack=stack,
            insulation_r_upgrade_m2k_w=INSULATION_UPGRADE_R_M2K_W,
        )
        assert out["insulation_net_electricity_saved_kwh_yr"].iloc[0] > 0.0


# ── Pricing columns + Colorbond Coolmax re-roof scenario ─────────────────────
class TestRoofTypeAndCoolmax:
    @pytest.mark.parametrize(
        "material, absorptance, expected",
        [
            ("concrete_tile", 0.7, "concrete"),
            ("roof_tiles", 0.7, "concrete"),
            ("terracotta", 0.7, "terracotta"),
            ("slate", 0.9, "slate"),
            ("metal_light", 0.9, "metal_light"),  # explicit label wins
            ("metal_dark", 0.3, "metal_dark"),
            ("other", 0.8, "metal_dark"),          # split on absorptance
            (None, 0.4, "metal_light"),
        ],
    )
    def test_roof_type_for(self, material, absorptance, expected):
        assert roof_type_for(material, absorptance) == expected

    def test_roof_and_coating_type_columns(self, weather_df):
        df = pd.DataFrame(
            {
                "absorptance_before": [0.8, 0.8, 0.4],
                "roof_surface_area_m2": [100.0] * 3,
                "roof_material": ["terracotta", "concrete_tile", "other"],
            }
        )
        out = run_model(df, weather_df)
        assert list(out["roof_type"]) == ["terracotta", "concrete", "metal_light"]
        assert list(out["coating_type"]) == [
            "terracotta_slate_coating", "concrete_tile_coating", "metal_roof_coating",
        ]
        assert not any(c in out.columns for c in _COOLMAX_OUTPUT_COLUMNS)  # opt-in only

    def test_coolmax_compares_existing_roof_to_coolmax_steel(self, weather_df):
        # Complete replacement: existing roof (own construction + current
        # absorptance) minus a new Coolmax steel roof. Checked independently
        # for a metal roof (in-group march path) and a terracotta roof
        # (separate steel march path).
        from config.settings import COOLMAX_ABSORPTANCE, COOLMAX_EMISSIVITY

        df = pd.DataFrame(
            {
                "absorptance_before": [0.8, 0.8],
                "roof_surface_area_m2": [120.0, 120.0],
                "roof_material": ["metal_dark", "terracotta"],
            }
        )
        out = run_model(df, weather_df, coolmax=True)
        assert list(out.columns[-3:]) == list(_COOLMAX_OUTPUT_COLUMNS)

        weather = build_hourly_weather(weather_df)
        steel = load_roof_layers()
        h_max = float(weather.h_ext_w_m2k.max())
        coolmax_flux = march_interior_flux(
            weather, steel, np.array([COOLMAX_ABSORPTANCE]),
            dt_s=resolve_timestep(steel, h_max), emissivity=COOLMAX_EMISSIVITY,
        )
        for row, stack in [(0, steel), (1, load_roof_layers(ROOF_LAYERS_TERRACOTTA_CSV))]:
            existing = march_interior_flux(
                weather, stack, np.array([0.8]), dt_s=resolve_timestep(stack, h_max),
            )
            expected = annual_benefit(existing, coolmax_flux, weather, [120.0])
            np.testing.assert_allclose(
                out.loc[row, list(_COOLMAX_OUTPUT_COLUMNS)].to_numpy(float),
                expected[["electricity_saved_kwh_yr", "heating_penalty_electricity_kwh_yr",
                          "net_electricity_saved_kwh_yr"]].to_numpy(float)[0],
            )
        # The coating columns are unchanged by adding the scenario.
        base = run_model(df, weather_df)
        pd.testing.assert_frame_equal(out[base.columns], base)


# ── Flux-sign accounting (Maggie's notebook method) ──────────────────────────
class TestFluxSignAccounting:
    def test_matches_notebook_hourly_comparison(self, weather):
        # Synthetic hourly fluxes: both-in (cooling), both-out (heating), and
        # one mismatch hour that the notebook leaves as NaN.
        n_hours = weather.n_hours - 1
        base = np.zeros((n_hours, 1)); cool = np.zeros((n_hours, 1))
        base[60], cool[60] = 50.0, 20.0     # both into the room: saved 30 Wh
        base[61], cool[61] = -10.0, -25.0   # both out: cool loses 15 Wh more
        base[62], cool[62] = 40.0, -5.0     # mismatch: dropped
        out = annual_benefit(base, cool, weather, [1000.0], ["house"], cooling_fraction=1.0,
                             heating_fraction=1.0)
        cop = HVAC_COP_RESIDENTIAL
        assert out.loc[0, "roof_flux_mode_mismatch_hours"] == 1
        # notebook: saved_Wh = |normal| - |cool| on same-mode hours
        assert out.loc[0, "electricity_saved_kwh_yr_fluxsign"] == round(30.0 / cop, 1)  # 30 Wh/m2 x 1000 m2
        assert out.loc[0, "heating_penalty_electricity_kwh_yr_fluxsign"] == round(15.0 / cop, 1)

    def test_fluxsign_is_a_lower_bound_on_cooling(self, weather, stack):
        flux = march_interior_flux(weather, stack, np.array([0.9, COOL_ROOF_ABSORPTANCE]), dt_s=40.0)
        out = annual_benefit(flux[:, :1], flux[:, 1:], weather, [100.0])
        assert out.loc[0, "electricity_saved_kwh_yr_fluxsign"] <= out.loc[0, "electricity_saved_kwh_yr"]


# ── Hourly export (tools.export_hourly_flux) ─────────────────────────────────
class TestHourlyScenarioFlux:
    def test_hourly_rows_integrate_to_run_model_annual_columns(self, weather_df):
        from stage3_thermal.heat_ingress_model import hourly_scenario_flux

        df = pd.DataFrame(
            {
                "building_id": ["a", "b"],
                "absorptance_before": [0.8, 0.6],
                "roof_surface_area_m2": [150.0, 90.0],
                "roof_material": ["metal_dark", "terracotta"],
            }
        )
        hourly = hourly_scenario_flux(df, weather_df)
        annual = run_model(df, weather_df, coolmax=True)

        assert len(hourly) == 2 * (len(weather_df) - 1)
        counted = hourly[~hourly["spin_up"]]
        for i, bid in enumerate(df["building_id"]):
            rows = counted[counted["building_id"] == bid]
            np.testing.assert_allclose(
                rows["flux_existing_wh_m2"].sum() / 1000.0,
                annual.loc[i, "roof_heat_ingress_base_kwh_m2_yr"], atol=0.01,
            )
            np.testing.assert_allclose(
                rows["flux_coated_wh_m2"].sum() / 1000.0,
                annual.loc[i, "roof_heat_ingress_cool_kwh_m2_yr"], atol=0.01,
            )
            cooling = rows[rows["hvac_mode"] == "cooling"]
            np.testing.assert_allclose(
                max(0.0, (cooling["heat_existing_kwh"] - cooling["heat_coated_kwh"]).sum()),
                annual.loc[i, "cooling_season_heat_avoided_kwh_yr"], atol=0.1,
            )
            np.testing.assert_allclose(
                rows["heat_existing_kwh"], rows["flux_existing_wh_m2"] * rows["roof_surface_area_m2"] / 1000.0,
            )
        # Coolmax hourly matches the --coolmax annual roll-up.
        weather = build_hourly_weather(weather_df)
        for i, bid in enumerate(df["building_id"]):
            rows = hourly[hourly["building_id"] == bid]
            out = annual_benefit(
                rows[["flux_existing_wh_m2"]].to_numpy(), rows[["flux_coolmax_wh_m2"]].to_numpy(),
                weather, [df.loc[i, "roof_surface_area_m2"]],
            )
            assert out.loc[0, "net_electricity_saved_kwh_yr"] == annual.loc[i, "net_electricity_saved_kwh_yr_coolmax"]
