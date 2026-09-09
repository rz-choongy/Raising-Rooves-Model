"""
Roof material and colour classifier for the Raising Rooves pipeline.

MVP approach: colour-based heuristic using mean RGB/HSV values of each
segmented roof region. Maps to broad categories based on known Victorian
roof material distributions (CSR data).

Future upgrade: fine-tune a small image classifier on labelled examples.
"""

from dataclasses import dataclass
from enum import Enum

import numpy as np

from shared.logging_config import setup_logging

logger = setup_logging("roof_classifier")


class RoofMaterial(str, Enum):
    METAL_LIGHT = "metal_light"
    METAL_DARK = "metal_dark"
    TERRACOTTA = "terracotta"
    CONCRETE_TILE = "concrete_tile"
    OTHER = "other"


class RoofColour(str, Enum):
    WHITE = "white"
    LIGHT_GREY = "light_grey"
    DARK_GREY = "dark_grey"
    RED = "red"
    BROWN = "brown"
    BLUE = "blue"
    GREEN = "green"
    OTHER = "other"


@dataclass
class RoofClassification:
    """Classification result for a single roof segment."""

    material: RoofMaterial
    colour: RoofColour
    mean_rgb: tuple[float, float, float]
    mean_hsv: tuple[float, float, float]
    confidence: float           # 0.0 to 1.0 — label classification confidence
    absorptance_estimate: float = 0.75   # direct HSV → absorptance (0–1)
    absorptance_uncertainty: float = 0.15  # ±1σ range around estimate


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """Convert RGB array (0-255) to HSV (H: 0-360, S: 0-1, V: 0-1)."""
    rgb_norm = rgb.astype(float) / 255.0
    r, g, b = rgb_norm[..., 0], rgb_norm[..., 1], rgb_norm[..., 2]

    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    # Hue
    h = np.zeros_like(delta)
    mask = delta > 0
    rm = mask & (cmax == r)
    gm = mask & (cmax == g)
    bm = mask & (cmax == b)
    h[rm] = 60.0 * (((g[rm] - b[rm]) / delta[rm]) % 6)
    h[gm] = 60.0 * (((b[gm] - r[gm]) / delta[gm]) + 2)
    h[bm] = 60.0 * (((r[bm] - g[bm]) / delta[bm]) + 4)

    # Saturation
    s = np.where(cmax > 0, delta / cmax, 0)

    # Value
    v = cmax

    return np.stack([h, s, v], axis=-1)


def _hsv_to_absorptance(mean_h: float, mean_s: float, mean_v: float) -> tuple[float, float]:
    """
    Map mean HSV directly to solar absorptance without going through a material label.

    Achromatic surfaces (S < 0.15): linear fit to AS/NZS 4859.1 values.
      absorptance ≈ 0.97 − 0.77 × V
      (white V=1.0 → 0.20; light grey V=0.75 → 0.39; mid grey V=0.5 → 0.59;
       dark grey V=0.25 → 0.78; black V=0.0 → 0.97)

    Chromatic surfaces (S ≥ 0.15): hue-specific minimum enforced because
    pigmented materials absorb more even at high brightness.

    Returns (absorptance_estimate, uncertainty_1sigma).
    """
    if mean_s < 0.15:
        absorptance = 0.97 - 0.77 * mean_v
        absorptance = max(0.15, min(0.97, absorptance))
        # Extremes (near-white or near-black) are more certain than mid-grey
        uncertainty = 0.08 if (mean_v > 0.75 or mean_v < 0.25) else 0.12
        return round(absorptance, 3), uncertainty

    # Chromatic: base from V, then hue floor
    base = 0.97 - 0.77 * mean_v
    base = max(0.15, min(0.97, base))

    # Red/terracotta (H: 0–30 or 330–360)
    if mean_h < 30 or mean_h > 330:
        return round(max(base, 0.65), 3), 0.12

    # Orange/brown (H: 30–50)
    if 30 <= mean_h < 50:
        return round(max(base, 0.65), 3), 0.12

    # Green (H: 80–160) — e.g. Colorbond Pale Eucalypt, Wilderness
    if 80 <= mean_h < 160:
        return round(max(base, 0.65), 3), 0.13

    # Blue (H: 180–270) — e.g. Colorbond Deep Ocean, Night Sky
    if 180 <= mean_h < 270:
        return round(max(base, 0.70), 3), 0.13

    # Other chromatic
    return round(max(base, 0.65), 3), 0.15


def _classify_by_hsv(mean_h: float, mean_s: float, mean_v: float) -> tuple[RoofMaterial, RoofColour, float]:
    """
    Classify roof material and colour from mean HSV values.

    Heuristic rules based on typical satellite imagery appearance of
    Melbourne roofing materials.
    """
    # Very bright / white (high V, low S) → light metal or coated
    if mean_v > 0.75 and mean_s < 0.15:
        return RoofMaterial.METAL_LIGHT, RoofColour.WHITE, 0.7

    # Light grey (moderate-high V, low S)
    if mean_v > 0.5 and mean_s < 0.15:
        return RoofMaterial.CONCRETE_TILE, RoofColour.LIGHT_GREY, 0.6

    # Dark grey (low V, low S) → dark metal
    if mean_v < 0.35 and mean_s < 0.2:
        return RoofMaterial.METAL_DARK, RoofColour.DARK_GREY, 0.6

    # Red/brown hues (H: 0-30 or 330-360, moderate S) → terracotta
    if (mean_h < 30 or mean_h > 330) and mean_s > 0.2:
        if mean_v > 0.4:
            return RoofMaterial.TERRACOTTA, RoofColour.RED, 0.65
        else:
            return RoofMaterial.TERRACOTTA, RoofColour.BROWN, 0.55

    # Orange hues (H: 30-50) → browner/more orange terracotta tiles.
    # Added 2026-08-20: a Carlton "other/other" audit found tiles in this range
    # falling through to the OTHER/OTHER default instead of being recognised as
    # terracotta-adjacent (this range already had an absorptance floor in
    # _hsv_to_absorptance but no label here). Needs a higher saturation floor than
    # the 0-30 band: re-checking 107 flipped buildings against satellite imagery
    # found every false positive (driveways, beige industrial roofs, corrugated
    # sheds — not tile) at S<=0.226, and every confirmed genuine terracotta tile
    # at S>=0.243. S>0.25 sits in that empirical gap.
    if 30 <= mean_h < 50 and mean_s > 0.25:
        return RoofMaterial.TERRACOTTA, RoofColour.BROWN, 0.55

    # Blue hues (H: 200-260) → colorbond blue
    if 200 < mean_h < 260 and mean_s > 0.15:
        return RoofMaterial.METAL_DARK, RoofColour.BLUE, 0.5

    # Green hues (H: 80-160)
    if 80 < mean_h < 160 and mean_s > 0.15:
        return RoofMaterial.METAL_DARK, RoofColour.GREEN, 0.5

    # Default: mid-tone metal
    if mean_v < 0.5:
        return RoofMaterial.METAL_DARK, RoofColour.DARK_GREY, 0.4
    return RoofMaterial.OTHER, RoofColour.OTHER, 0.3


def classify_roof(
    tile_image: np.ndarray,
    mask: np.ndarray,
    segment_id: int = 0,
) -> RoofClassification:
    """
    Classify a single roof segment by material and colour.

    Args:
        tile_image: Original satellite tile as RGB numpy array (H, W, 3).
        mask: Binary mask for this specific segment (H, W), True = roof.
        segment_id: ID for logging purposes.

    Returns:
        RoofClassification with material, colour, and confidence.
    """
    # Extract pixels under the mask
    roof_pixels = tile_image[mask]

    if len(roof_pixels) == 0:
        logger.warning("Segment %d: empty mask, cannot classify", segment_id)
        return RoofClassification(
            material=RoofMaterial.OTHER,
            colour=RoofColour.OTHER,
            mean_rgb=(0, 0, 0),
            mean_hsv=(0, 0, 0),
            confidence=0.0,
        )

    # Compute mean RGB
    mean_rgb = tuple(float(v) for v in roof_pixels.mean(axis=0))

    # Compute mean HSV
    hsv_pixels = _rgb_to_hsv(roof_pixels.reshape(-1, 1, 3)).reshape(-1, 3)
    mean_hsv = tuple(float(v) for v in hsv_pixels.mean(axis=0))

    # Classify
    material, colour, confidence = _classify_by_hsv(mean_hsv[0], mean_hsv[1], mean_hsv[2])
    absorptance, uncertainty = _hsv_to_absorptance(mean_hsv[0], mean_hsv[1], mean_hsv[2])

    logger.debug(
        "Segment %d: material=%s colour=%s (conf=%.2f) absorptance=%.2f±%.2f RGB=(%.0f,%.0f,%.0f)",
        segment_id,
        material.value,
        colour.value,
        confidence,
        absorptance,
        uncertainty,
        *mean_rgb,
    )

    return RoofClassification(
        material=material,
        colour=colour,
        mean_rgb=mean_rgb,
        mean_hsv=mean_hsv,
        confidence=confidence,
        absorptance_estimate=absorptance,
        absorptance_uncertainty=uncertainty,
    )
