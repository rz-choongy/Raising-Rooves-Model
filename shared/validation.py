"""
Data and environment validation helpers for the Raising Rooves pipeline.

All validation functions return True on success or raise ValueError with
a clear message on failure.
"""

import os

from config.settings import AUSTRALIA_BBOX


def validate_bbox(bbox: tuple[float, float, float, float]) -> bool:
    """
    Check that a bounding box is valid and within Australia (sanity check only).

    Args:
        bbox: (south, west, north, east) in EPSG:4326.

    Raises:
        ValueError: If bbox is malformed or outside Australia.
    """
    if len(bbox) != 4:
        raise ValueError(f"Bounding box must have 4 values (south, west, north, east), got {len(bbox)}")

    south, west, north, east = bbox
    if south >= north:
        raise ValueError(f"South ({south}) must be less than north ({north})")
    if west >= east:
        raise ValueError(f"West ({west}) must be less than east ({east})")

    # Check within Australia bounds (with margin) -- a loose sanity check, not
    # a state restriction. config/suburbs.py is mostly Victorian but can carry
    # interstate comparison suburbs (e.g. Parramatta, NSW).
    au_south, au_west, au_north, au_east = AUSTRALIA_BBOX
    margin = 0.5  # degrees
    if south < au_south - margin or north > au_north + margin:
        raise ValueError(f"Latitude ({south}, {north}) outside Australia range")
    if west < au_west - margin or east > au_east + margin:
        raise ValueError(f"Longitude ({west}, {east}) outside Australia range")

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
