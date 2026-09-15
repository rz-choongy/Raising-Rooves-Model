"""
Suburb definitions for the Raising Rooves pipeline.

Each suburb has: name, ABS SA2 code (where available), centroid (lat, lon),
and bounding box (south, west, north, east) in EPSG:4326.

Covers Greater Melbourne (inner, middle, outer, diverse zone types) and
Victoria's major regional centres for statewide cool roof analysis, plus a
small number of interstate suburbs added for climate-contrast comparisons
(see DECISION_LOG.md).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Suburb:
    name: str
    sa2_code: str  # ABS SA2 code, empty string if unknown
    centroid: tuple[float, float]  # (lat, lon)
    bbox: tuple[float, float, float, float]  # (south, west, north, east)
    zone_type: str  # "residential", "industrial", "commercial", "mixed"

    @property
    def key(self) -> str:
        """Normalised slug used in filenames and directory names."""
        return self.name.lower().replace(" ", "_")


# ── Test Suburbs ─────────────────────────────────────────────────────────────
# Chosen to cover different zone types, distances from CBD, and roof profiles.

SUBURBS = {
    "richmond": Suburb(
        name="Richmond",
        sa2_code="206041122",
        centroid=(-37.8183, 144.9981),
        bbox=(-37.8300, 144.9850, -37.8050, 145.0150),
        zone_type="mixed",
    ),
    "carlton": Suburb(
        name="Carlton",
        sa2_code="206041118",
        centroid=(-37.7998, 144.9667),
        bbox=(-37.8100, 144.9550, -37.7900, 144.9780),
        zone_type="residential",
    ),
    "footscray": Suburb(
        name="Footscray",
        sa2_code="206031098",
        centroid=(-37.8000, 144.8992),
        bbox=(-37.8120, 144.8850, -37.7880, 144.9130),
        zone_type="mixed",
    ),
    "box_hill": Suburb(
        name="Box Hill",
        sa2_code="206051133",
        centroid=(-37.8190, 145.1218),
        bbox=(-37.8310, 145.1070, -37.8070, 145.1370),
        zone_type="residential",
    ),
    "dandenong": Suburb(
        name="Dandenong",
        sa2_code="206071185",
        centroid=(-37.9870, 145.2150),
        bbox=(-38.0000, 145.1950, -37.9740, 145.2350),
        zone_type="industrial",
    ),
    "tullamarine": Suburb(
        name="Tullamarine",
        sa2_code="206021068",
        centroid=(-37.7000, 144.8800),
        bbox=(-37.7200, 144.8550, -37.6800, 144.9050),
        zone_type="industrial",
    ),
    "clayton": Suburb(
        name="Clayton",
        sa2_code="206061166",
        centroid=(-37.9150, 145.1220),
        bbox=(-37.9270, 145.1060, -37.9030, 145.1380),
        zone_type="mixed",  # Monash University + residential
    ),

    # ── Greater Melbourne — additional zones ──────────────────────────────────────

    "toorak": Suburb(
        name="Toorak",
        sa2_code="206041127",
        centroid=(-37.8400, 144.9940),
        bbox=(-37.8550, 144.9740, -37.8250, 145.0140),
        zone_type="residential",  # high-income, large roofs
    ),
    "sunshine": Suburb(
        name="Sunshine",
        sa2_code="206031097",
        centroid=(-37.7890, 144.8310),
        bbox=(-37.8040, 144.8110, -37.7740, 144.8510),
        zone_type="residential",  # western suburbs density
    ),
    "frankston": Suburb(
        name="Frankston",
        sa2_code="206081215",
        centroid=(-38.1440, 145.1260),
        bbox=(-38.1590, 145.1060, -38.1290, 145.1460),
        zone_type="residential",  # outer south coastal
    ),
    "epping": Suburb(
        name="Epping",
        sa2_code="206011038",
        centroid=(-37.6460, 145.0070),
        bbox=(-37.6610, 144.9870, -37.6310, 145.0270),
        zone_type="residential",  # northern growth corridor
    ),
    "cranbourne": Suburb(
        name="Cranbourne",
        sa2_code="206091234",
        centroid=(-38.1100, 145.2830),
        bbox=(-38.1250, 145.2630, -38.0950, 145.3030),
        zone_type="residential",  # southeastern growth corridor
    ),
    "port_melbourne": Suburb(
        name="Port Melbourne",
        sa2_code="206041121",
        centroid=(-37.8330, 144.9330),
        bbox=(-37.8450, 144.9130, -37.8210, 144.9530),
        zone_type="mixed",  # inner coastal industrial conversion
    ),

    # ── Regional Victoria ──────────────────────────────────────────────────────────

    "geelong_central": Suburb(
        name="Geelong (Central)",
        sa2_code="212011370",
        centroid=(-38.1490, 144.3610),
        bbox=(-38.1700, 144.3310, -38.1280, 144.3910),
        zone_type="mixed",
    ),
    "ballarat_central": Suburb(
        name="Ballarat (Central)",
        sa2_code="207011264",
        centroid=(-37.5620, 143.8590),
        bbox=(-37.5800, 143.8290, -37.5440, 143.8890),
        zone_type="residential",
    ),
    "bendigo_central": Suburb(
        name="Bendigo (Central)",
        sa2_code="208011279",
        centroid=(-36.7570, 144.2790),
        bbox=(-36.7750, 144.2490, -36.7390, 144.3090),
        zone_type="residential",
    ),
    "shepparton": Suburb(
        name="Shepparton",
        sa2_code="214021444",
        centroid=(-36.3830, 145.3990),
        bbox=(-36.4010, 145.3690, -36.3650, 145.4290),
        zone_type="residential",
    ),
    "mildura": Suburb(
        name="Mildura",
        sa2_code="215011453",
        centroid=(-34.1850, 142.1620),
        bbox=(-34.2030, 142.1320, -34.1670, 142.1920),
        zone_type="residential",
    ),
    "traralgon": Suburb(
        name="Traralgon",
        sa2_code="213021422",
        centroid=(-38.1950, 146.5380),
        bbox=(-38.2130, 146.5080, -38.1770, 146.5680),
        zone_type="industrial",
    ),
    "wodonga": Suburb(
        name="Wodonga",
        sa2_code="214011437",
        centroid=(-36.1210, 146.8880),
        bbox=(-36.1390, 146.8580, -36.1030, 146.9180),
        zone_type="residential",
    ),
    "warrnambool": Suburb(
        name="Warrnambool",
        sa2_code="209011343",
        centroid=(-38.3830, 142.4810),
        bbox=(-38.4010, 142.4510, -38.3650, 142.5110),
        zone_type="residential",
    ),
    # ── Interstate comparison suburbs ────────────────────────────────────────
    # Not Victorian -- included for climate contrast in cross-suburb Stage 3
    # analysis (western Sydney basin vs. Victoria). See DECISION_LOG 2026-09-14.
    "parramatta": Suburb(
        name="Parramatta",
        sa2_code="125041717",  # ABS SA2 "Parramatta - North"; CBD sits on the North/South split
        centroid=(-33.8147, 151.0017),
        bbox=(-33.8262, 150.9877, -33.8032, 151.0157),
        zone_type="commercial",
    ),
    "west_end": Suburb(
        name="West End",
        sa2_code="305011112",  # ABS SA2 "West End" (Brisbane) -- subtropical climate contrast
        centroid=(-27.4820, 153.0080),
        bbox=(-27.4900, 153.0000, -27.4740, 153.0170),
        zone_type="mixed",
    ),
    "subiaco": Suburb(
        name="Subiaco",
        sa2_code="503021042",  # ABS SA2 "Subiaco - Shenton Park" -- Mediterranean/semi-arid climate contrast
        centroid=(-31.9490, 115.8268),
        bbox=(-31.9590, 115.8148, -31.9390, 115.8388),
        zone_type="residential",
    ),
    "norwood": Suburb(
        name="Norwood",
        sa2_code="401051017",  # ABS SA2 "Norwood" -- inner Adelaide, semi-arid Mediterranean climate contrast
        centroid=(-34.9230, 138.6330),
        bbox=(-34.9310, 138.6230, -34.9150, 138.6430),
        zone_type="mixed",
    ),
    "north_hobart": Suburb(
        name="North Hobart",
        sa2_code="60102",  # ABS SA2 "Hobart - North East" (North Hobart is a SAL within this SA2) -- cool-temperate climate contrast
        centroid=(-42.8722, 147.3144),
        bbox=(-42.8800, 147.3040, -42.8640, 147.3240),
        zone_type="mixed",
    ),
    "parap": Suburb(
        name="Parap",
        sa2_code="701011007",  # ABS SA2 "Parap" (Darwin) -- tropical monsoonal climate contrast
        centroid=(-12.4396, 130.8582),
        bbox=(-12.4476, 130.8482, -12.4316, 130.8682),
        zone_type="mixed",
    ),
    "braddon": Suburb(
        name="Braddon",
        sa2_code="801051051",  # ABS SA2 "Braddon" (Canberra) -- inland highland/cool-temperate climate contrast
        centroid=(-35.2721, 149.1357),
        bbox=(-35.2801, 149.1257, -35.2641, 149.1457),
        zone_type="mixed",
    ),
}


def get_suburb(name: str) -> Suburb:
    """Look up a suburb by name (case-insensitive, spaces replaced with underscores)."""
    key = name.lower().replace(" ", "_").replace("(", "").replace(")", "").replace(",", "")
    # Normalise multi-space runs that result from stripping parentheses
    import re
    key = re.sub(r"_+", "_", key).strip("_")
    if key not in SUBURBS:
        available = ", ".join(s.name for s in SUBURBS.values())
        raise ValueError(f"Unknown suburb '{name}'. Available: {available}")
    return SUBURBS[key]


def list_suburbs() -> list[str]:
    """Return list of available suburb names."""
    return [s.name for s in SUBURBS.values()]
