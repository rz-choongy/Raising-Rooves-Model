"""
Tests for tools/economic_analysis.py.

Builds a tiny synthetic hourly Stage 3 export (one building, one full year,
cooling-saving-only so the dollar math is easy to hand-check) and a minimal
flat-rate electricity zone, so no real pipeline outputs or DuckDB network
access are required.
"""

import pandas as pd
import pytest

import tools.economic_analysis as ea

_TEST_ZONE = "TestZone"
_FLAT_RATE = 0.30  # $/kWh
_GAS_RATE_C_PER_MJ = 2.50


def _make_hourly_df(n_hours: int = 8760, cooling_wh: float = 1000.0) -> pd.DataFrame:
    """One building, constant hourly cooling saving, zero heating penalty."""
    time = pd.date_range("2026-01-01", periods=n_hours, freq="1h")
    return pd.DataFrame({
        "building_id": ["b1"] * n_hours,
        "time_melbourne": time,
        "roof_construction": ["metal"] * n_hours,
        "roof_surface_area_m2": [100.0] * n_hours,
        "flux_base_wh_m2": [10.0] * n_hours,
        "flux_cool_wh_m2": [5.0] * n_hours,
        "flux_coolmax_wh_m2": [5.0] * n_hours,
        "electricity_saved_cool_wh": [cooling_wh] * n_hours,
        "electricity_saved_coolmax_wh": [cooling_wh] * n_hours,
    })


@pytest.fixture(autouse=True)
def _patch_economic_settings(tmp_path, monkeypatch):
    """Minimal, deterministic economic assumptions for every test in this file."""
    monkeypatch.setattr(ea.cfg, "ECONOMIC_ELECTRICITY_ZONES", {
        _TEST_ZONE: dict(state="TEST", flat=_FLAT_RATE, verified=True, tou=[]),
    })
    monkeypatch.setattr(ea.cfg, "ECONOMIC_GAS_RATE_C_PER_MJ", {"TEST": _GAS_RATE_C_PER_MJ})
    monkeypatch.setattr(ea.cfg, "ECONOMIC_TARIFF_STRUCTURE", "flat")
    monkeypatch.setattr(ea.cfg, "ECONOMIC_COATING_FOR_ROOF", {"metal": "endure30c"})
    monkeypatch.setattr(ea.cfg, "ECONOMIC_OCCUPANCY_DIAL", 0.0)
    monkeypatch.setattr(ea, "_DEFAULT_ZONE", _TEST_ZONE)
    monkeypatch.setattr(ea, "_OUTPUT_DIR", tmp_path)
    yield


def test_aggregate_splits_cooling_and_heating(tmp_path):
    df = _make_hourly_df()
    path = tmp_path / "hourly.parquet"
    df.to_parquet(path, index=False)

    agg = ea.aggregate(str(path))
    assert agg["coating_cool_kwh"].sum() == pytest.approx(1.0 * len(df))  # 1000 Wh = 1 kWh/hr
    assert agg["coating_heat_kwh"].sum() == pytest.approx(0.0)
    assert set(agg["zone"].unique()) == {_TEST_ZONE}


def test_price_applies_flat_tariff(tmp_path):
    df = _make_hourly_df()
    path = tmp_path / "hourly.parquet"
    df.to_parquet(path, index=False)

    b = ea.price(ea.aggregate(str(path)))
    assert len(b) == 1
    row = b.iloc[0]
    expected_kwh = 8760.0  # 1 kWh/hr, full year
    assert row["coating_cool_kwh"] == pytest.approx(expected_kwh, rel=1e-3)
    assert row["coating_cool_aud"] == pytest.approx(expected_kwh * _FLAT_RATE, rel=1e-3)
    assert row["coating_heat_kwh"] == pytest.approx(0.0)
    assert row["coating_product"] == "endure30c"


def test_run_end_to_end_writes_outputs_with_positive_saving(tmp_path, monkeypatch):
    monkeypatch.setattr(ea.cfg, "ECONOMIC_DISCOUNT_RATES_REAL", [0.07])
    monkeypatch.setattr(ea.cfg, "ECONOMIC_ANALYSIS_PERIOD_YEARS", 5)
    monkeypatch.setattr(ea.cfg, "ECONOMIC_HEATING_FUELS", ["electric"])
    monkeypatch.setattr(ea.cfg, "ECONOMIC_REAPPLY_AT_WARRANTY_END", False)

    df = _make_hourly_df()
    path = tmp_path / "hourly.parquet"
    df.to_parquet(path, index=False)

    res = ea.run(str(path))
    assert len(res) == 1  # one building x one discount rate

    row = res.iloc[0]
    # Cooling-only building: year-1 saving is pure positive electricity saving,
    # no heating penalty to offset it (sign convention: savings positive).
    assert row["coating_elec__year1_heating_aud"] == pytest.approx(0.0)
    assert row["coating_elec__year1_saving_aud"] > 0
    assert row["coating_elec__npv_aud"] == row["coating_elec__npv_aud"]  # not NaN

    assert (tmp_path / "economic_analysis_results.csv").exists()
    assert (tmp_path / "economic_analysis_summary.csv").exists()
    assert (tmp_path / "economic_analysis_cashflow_example.csv").exists()


def test_aggregate_rejects_missing_columns(tmp_path):
    df = _make_hourly_df(n_hours=10).drop(columns=["electricity_saved_cool_wh"])
    path = tmp_path / "bad.parquet"
    df.to_parquet(path, index=False)

    with pytest.raises(SystemExit):
        ea.aggregate(str(path))


def test_unmapped_roof_type_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(ea.cfg, "ECONOMIC_COATING_FOR_ROOF", {})  # no roof types mapped
    df = _make_hourly_df(n_hours=10)
    path = tmp_path / "hourly.parquet"
    df.to_parquet(path, index=False)

    with pytest.raises(SystemExit):
        ea.price(ea.aggregate(str(path)))
