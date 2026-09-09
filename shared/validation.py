"""
Data and environment validation helpers for the Raising Rooves pipeline.

All validation functions return True on success or raise ValueError with
a clear message on failure.
"""

import os

from config.settings import VICTORIA_BBOX


def validate_bbox(bbox: tuple[float, float, float, float]) -> bool:
    """
    Check that a bounding box is valid and within Victoria (incl. regional centres).

    Args:
        bbox: (south, west, north, east) in EPSG:4326.

    Raises:
        ValueError: If bbox is malformed or outside Victoria.
    """
    if len(bbox) != 4:
        raise ValueError(f"Bounding box must have 4 values (south, west, north, east), got {len(bbox)}")

    south, west, north, east = bbox
    if south >= north:
        raise ValueError(f"South ({south}) must be less than north ({north})")
    if west >= east:
        raise ValueError(f"West ({west}) must be less than east ({east})")

    # Check within Victoria state bounds (with margin)
    vic_south, vic_west, vic_north, vic_east = VICTORIA_BBOX
    margin = 0.5  # degrees
    if south < vic_south - margin or north > vic_north + margin:
        raise ValueError(f"Latitude ({south}, {north}) outside Victoria range")
    if west < vic_west - margin or east > vic_east + margin:
        raise ValueError(f"Longitude ({west}, {east}) outside Victoria range")

    return True


def validate_env_vars(required: list[str]) -> dict[str, str]:
    """
    Check that all required environment variables are set and non-empty.

    Args:
        required: List of environment variable names.

    Returns:
        Dict mapping variable names to their values.

    Raises:
        ValueError: If any required variable is missing or empty.
    """
    values = {}
    missing = []
    for var in required:
        val = os.getenv(var, "").strip()
        if not val:
            missing.append(var)
        else:
            values[var] = val

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            f"Set them in your .env file (see .env.example)."
        )

    return values
