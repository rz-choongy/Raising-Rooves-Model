"""
Tests for stage3_thermal/heat_ingress_model.py — the vectorised transient roof
heat-ingress engine that replaced the inferred-R_roof thermal_calculator.

Covers:
- Roof stack loading + cavity-R override
- Weather array preparation (shortwave sum, wind → h_ext height correction)
- Stability timestep and its clamping
- Vectorised march parity with a faithful scalar re-implementation of the
  notebook's transient cell (heat_ingress_model.ipynb, cell 23bd2f98)
- march_interior_flux for N buildings == N single-building marches
- annual_benefit: monotonicity in absorptance, no-op for already-cool roofs,
  signed heating penalty, output columns, COP by building type
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from config.settings import (
    COOL_ROOF_ABSORPTANCE,
    HEAT_INGRESS_COOLING_SETPOINT_C,
    HEAT_INGRESS_HEATING_SETPOINT_C,
    HVAC_COP_COMMERCIAL,
    HVAC_COP_RESIDENTIAL,
    ROOF_LAYERS_TILE_CSV,
)
from stage3_thermal import heat_ingress_model as him
from stage3_thermal.heat_ingress_model import (
    _KELVIN,
    _OUTPUT_COLUMNS,
    _SKY_DEPRESSION_K,
    _STEFAN_BOLTZMANN_W_M2K4,
    HourlyWeather,
    annual_benefit,
    build_hourly_weather,
    hvac_cop,
    load_roof_layers,
    march_interior_flux,
    max_stable_timestep,
    resolve_timestep,
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
    # Explicit cavity-R override (matches the notebook's transient run).
    return load_roof_layers(cavity_r_m2k_w=0.23)


# ── Faithful scalar reference (notebook cell 23bd2f98) ───────────────────────
def _notebook_reference_flux(weather: HourlyWeather, stack, absorptance, dt):
    """One-building transient march, transcribed from the notebook."""
    R = stack.r_value_m2k_w
    density = stack.density_kg_m3
    Cp = stack.heat_capacity_j_kgk
    thick = stack.thickness_m
    hi = 3.0
    emissivity = 0.9
    const = _STEFAN_BOLTZMANN_W_M2K4
    T_heat = HEAT_INGRESS_HEATING_SETPOINT_C + _KELVIN
    T_cool = HEAT_INGRESS_COOLING_SETPOINT_C + _KELVIN

    tph = int(np.ceil(3600.0 / dt))
    temp_k = weather.outdoor_temp_c + _KELVIN
    q_sw_hourly = absorptance * weather.shortwave_incident_w_m2
    h_ext = weather.h_ext_w_m2k

    mid = np.full(4, temp_k[0], dtype=float)
    out = np.zeros(weather.n_hours - 1)
    for hour in range(weather.n_hours - 1):
        outside = np.linspace(temp_k[hour], temp_k[hour + 1], tph)
        shortwave = np.linspace(q_sw_hourly[hour], q_sw_hourly[hour + 1], tph)
        ho = np.linspace(h_ext[hour], h_ext[hour + 1], tph)
        acc = 0.0
        for ts in range(tph):
            q = np.zeros(5)
            sky = outside[ts] - _SKY_DEPRESSION_K
            q[0] = (
                shortwave[ts]
                + ho[ts] * (outside[ts] - mid[0])
                + emissivity * const * (sky ** 4 - mid[0] ** 4)
            )
            for j in range(1, 4):
                q[j] = 2 * (mid[j - 1] - mid[j]) / (R[j - 1] + R[j])
            T_inside = T_heat if outside[ts] < T_heat else T_cool
            q[4] = (mid[3] - T_inside) / (R[3] / 2 + 1 / hi)
            acc += q[4] * dt / 3600.0
            mid = mid + dt * (q[:-1] - q[1:]) / (density * Cp * thick)
        out[hour] = acc
    return out


# ── Roof stack ──────────────────────────────────────────────────────────────
class TestRoofStack:
    def test_layer_order(self, stack):
        assert stack.layer_names == ("Steel", "Insulation", "Cavity", "Plaster")
        assert stack.thickness_m.shape == (4,)
        assert stack.cavity_index == 2

    def test_cavity_override(self):
        overridden = load_roof_layers(cavity_r_m2k_w=0.23)
        raw = load_roof_layers(cavity_r_m2k_w=None)
        assert overridden.r_value_m2k_w[2] == pytest.approx(0.23)
        assert raw.r_value_m2k_w[2] != pytest.approx(0.23)

    def test_areal_heat_capacity(self, stack):
        expected = stack.density_kg_m3 * stack.heat_capacity_j_kgk * stack.thickness_m
        np.testing.assert_allclose(stack.areal_heat_capacity_j_m2k, expected)

    def test_tile_stack_loads_and_has_more_mass_than_steel(self, stack):
        tile = load_roof_layers(ROOF_LAYERS_TILE_CSV, cavity_r_m2k_w=0.23)
        assert tile.thickness_m.shape == (4,)
        assert tile.layer_names[0].lower().startswith("concrete") or "tile" in tile.layer_names[0].lower()
        assert tile.cavity_index == 1  # roof space sits directly under the tile
        # A tile roof's outer skin has far more thermal mass than a thin steel deck.
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
    def test_tile_materials_map_to_tile(self):
        assert stack_for_material("terracotta") == "tile"
        assert stack_for_material("concrete_tile") == "tile"
        assert stack_for_material("Terracotta") == "tile"  # case-insensitive

    def test_everything_else_maps_to_metal(self):
        for material in ("metal_dark", "metal_light", "other", None, "yes"):
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


# ── Stability ───────────────────────────────────────────────────────────────
class TestStability:
    def test_max_timestep_positive_and_cavity_limited(self, stack, weather):
        dt_max = max_stable_timestep(stack, float(weather.h_ext_w_m2k.max()))
        assert 20.0 < dt_max < 60.0  # cavity layer bound, ~42 s in the notebook

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
        assert list(out.columns) == ["roof_construction", *_OUTPUT_COLUMNS]
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
        assert out.loc[1, "roof_construction"] == "tile"

        # Different thermal mass -> different (unrounded) hourly flux for the
        # same absorptance/weather. The tile's outer skin has far more mass, so
        # its flux trace should be damped relative to the thin steel deck's.
        metal_stack, tile_stack = load_roof_layers(), load_roof_layers(ROOF_LAYERS_TILE_CSV)
        weather = build_hourly_weather(weather_df)
        dt = resolve_timestep(metal_stack, float(weather.h_ext_w_m2k.max()))
        metal_flux = march_interior_flux(weather, metal_stack, 0.85, dt_s=dt)
        tile_flux = march_interior_flux(weather, tile_stack, 0.85, dt_s=dt)
        assert not np.allclose(metal_flux, tile_flux)
        # steel swings harder hour-to-hour (less thermal mass to damp the response);
        # hour-to-hour deltas isolate that better than overall std, which also
        # carries the mean level and is sensitive to the heating/cooling setpoint mix.
        assert np.diff(metal_flux, axis=0).std() > np.diff(tile_flux, axis=0).std()

    def test_default_stacks_cover_only_terracotta_and_concrete(self, weather_df):
        df = pd.DataFrame(
            {
                "absorptance_before": [0.6, 0.6, 0.6],
                "roof_surface_area_m2": [100.0, 100.0, 100.0],
                "roof_material": ["terracotta", "concrete_tile", "metal_light"],
            },
        )
        out = run_model(df, weather_df)
        assert list(out["roof_construction"]) == ["tile", "tile", "metal"]
