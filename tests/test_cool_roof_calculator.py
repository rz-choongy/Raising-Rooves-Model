"""Stage 2 cool-roof delta: the coating applied must match the roof type."""

import math

import pytest

from config.settings import COOL_ROOF_ABSORPTANCE_BY_STACK
from stage2_irradiance.cool_roof_calculator import (
    calculate_building_benefit,
    cool_absorptance_for_material,
)
from stage3_thermal.heat_ingress_model import cool_absorptance_for_stack, stack_for_material


@pytest.mark.parametrize(
    "material, stack",
    [
        ("metal_dark", "metal"),
        ("metal_light", "metal"),
        ("concrete_tile", "concrete"),
        ("roof_tiles", "concrete"),
        ("terracotta", "terracotta"),
        ("Slate", "slate"),
    ],
)
def test_coating_per_material(material: str, stack: str) -> None:
    assert cool_absorptance_for_material(material) == COOL_ROOF_ABSORPTANCE_BY_STACK[stack]


@pytest.mark.parametrize("material", [None, float("nan"), "other", "glass"])
def test_unknown_material_uses_default_stack_coating(material) -> None:
    assert cool_absorptance_for_material(material) == COOL_ROOF_ABSORPTANCE_BY_STACK["metal"]


@pytest.mark.parametrize(
    "material", ["metal_dark", "metal_light", "concrete_tile", "terracotta", "slate", None, "other"]
)
def test_stage2_and_stage3_agree(material) -> None:
    # The Stage 2 absorbed-solar proxy and the Stage 3 march must coat a
    # building with the same absorptance, or the two parquet columns disagree.
    assert cool_absorptance_for_material(material) == cool_absorptance_for_stack(
        stack_for_material(material)
    )


def test_energy_saved_uses_material_coating() -> None:
    out = calculate_building_benefit(
        area_m2=100.0, pitch_deg=0.0, annual_ghi_kwh_m2=1000.0,
        roof_colour=None, roof_material="terracotta", absorptance_estimate=0.75,
    )
    alpha_after = COOL_ROOF_ABSORPTANCE_BY_STACK["terracotta"]
    assert out["absorptance_after"] == alpha_after
    assert math.isclose(out["energy_saved_kwh_yr"], round(100_000 * (0.75 - alpha_after), 1))
