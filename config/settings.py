"""
Central configuration for the Raising Rooves pipeline.

All paths, API endpoints, default parameters, and environment variable loading.
Secrets are loaded from .env via python-dotenv — never hardcoded.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ── Directory Paths ──────────────────────────────────────────────────────────

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
TILES_DIR = RAW_DIR / "tiles"
BARRA_DIR = RAW_DIR / "barra"
NASA_POWER_CACHE_DIR = RAW_DIR / "nasa_power"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = DATA_DIR / "output"
LOGS_DIR = PROJECT_ROOT / "logs"

# ── API Keys (from .env) ────────────────────────────────────────────────────

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ── Google Maps Static API ───────────────────────────────────────────────────

GOOGLE_MAPS_BASE_URL = "https://maps.googleapis.com/maps/api/staticmap"
DEFAULT_TILE_SIZE = 640  # pixels (max for free tier)
DEFAULT_ZOOM = 19  # ~0.29 m/pixel at Melbourne latitude
DEFAULT_MAP_TYPE = "satellite"

# ── BARRA2 Climate Data ─────────────────────────────────────────────────────
#
# NCI THREDDS OPeNDAP base for BARRA2.
# Path structure (confirmed from README and live catalog 2026-04-30):
#   ob53/output/reanalysis/{domain_id}/BOM/ERA5/historical/hres/BARRA-R2/v1/{freq}/{variable_id}/
# NOTE: the gdata path includes BARRA2/ but the THREDDS catalog does not.
# Domain: AUS-11 = 11 km BARRA-R2 grid (covers all of Australia).
#         AUS-04 = BARRA-C2 4 km grid (different product, limited domains).
# Variable names follow CORDEX/CF conventions — NOT UM/BOM internal names.
# av_swsfcdown and temp_scrn are the gdata variable *names inside* the file;
# the *folder/filename* uses rsds and tas respectively.
#
# NCI account required. Monash students: register at https://my.nci.org.au
# and ask your supervisor (Stuart) for project ob53 access.

# Master switch for the BARRA2 OPeNDAP path. Without NCI project ob53 access
# every fetch fails (2 variables x years x 12 months of doomed, un-timeboxed
# network calls), so the path stays off until access lands. Flip to True once
# an NCI account with ob53 is available.
BARRA2_ENABLED = True

BARRA2_THREDDS_BASE = "https://thredds.nci.org.au/thredds/dodsC/ob53"
BARRA2_CATALOG_BASE = "https://thredds.nci.org.au/thredds/catalog/ob53"

# BARRA2_DOMAIN: AUS-11 is the standard BARRA-R2 ~11 km grid.
BARRA2_DOMAIN = "AUS-11"

# Folder/filename identifiers on THREDDS (CORDEX/CF variable names).
# The NetCDF variable name *inside* each file may differ — see comments below.
#
# Currently used by the pipeline: solar_irradiance (rsds) and temperature_2m (tas).
# To add more variables when BARRA2 is enabled, add entries here and wire them
# into fetch_all_climate_data() in barra_client.py.
BARRA2_VARIABLES = {
    # rsds = surface downwelling shortwave radiation flux (W/m²).
    # NetCDF variable name inside the file: rsds.
    "solar_irradiance": "rsds",
    # tas = near-surface (2 m) air temperature (K).
    "temperature_2m": "tas",
    # sfcWind = near-surface (10 m) wind speed (m/s). Confirmed present in the
    # AUS-11 BARRA-R2 1hr catalog alongside rsds/tas (same file-per-month layout).
    "wind_speed_10m": "sfcWind",
}

# ── Melbourne Defaults ───────────────────────────────────────────────────────

MELBOURNE_BBOX = (-38.1, 144.5, -37.5, 145.5)  # south, west, north, east

# ── Cooling/Heating Degree Day Base Temperatures ─────────────────────────────

CDD_BASE_TEMP = 18.0  # °C — cooling needed above this
HDD_BASE_TEMP = 18.0  # °C — heating needed below this

# ── Building Footprint Supplement ────────────────────────────────────────────

# Spatially-indexed GeoPackage built once by tools/build_footprint_index.py.
# When present, Stage 1 automatically merges it with OSM (no extra flags needed).
# Build it with:  python -m tools.build_footprint_index
FOOTPRINT_SUPPLEMENT_GPKG = RAW_DIR / "footprints" / "buildings_index.gpkg"

# Fallback: raw GeoJSONL (slower — full linear scan ~23 s per suburb).
FOOTPRINT_SUPPLEMENT_GEOJSONL = RAW_DIR / "footprints" / "melbourne_overture.geojsonl"

# ── HSV Classifier Per-Suburb Calibration ──────────────────────────────────────
# Multiplier applied to HSV classifier confidence per suburb.  Calibrated against
# Gemini 2.5 Flash validation (507 buildings, 2026-08-11).  Suburbs with clearer
# satellite imagery get multipliers near 1.0; suburbs with shadow/blur/occlusion
# get lower multipliers to reflect higher uncertainty.
# Default 0.85 for suburbs not explicitly listed.
SUBURB_CLASSIFIER_QUALITY: dict[str, float] = {
    "carlton": 1.0,    # clear imagery, 80% Gemini confidence, 87% light_grey agree
    "clayton": 0.65,   # shadowed imagery, 31% Gemini confidence, 52% light_grey agree
}

# ── Roof Pitch ────────────────────────────────────────────────────────────────

# Pitch angle (degrees) below which a roof is classified as flat.
# Used by Stage 1 pipeline (_assumed_pitch_deg / _building_to_row).
FLAT_PITCH_THRESHOLD_DEG = 5.0

# ── Cool Roof Physics ────────────────────────────────────────────────────────

# Solar absorptance after cool roof coating treatment (target SRI ≥ 78)
COOL_ROOF_ABSORPTANCE = 0.20

# Victorian grid emissions intensity (kg CO2-e per kWh), AEMO 2023
GRID_EMISSIONS_FACTOR_KG_KWH = 0.79

# Melbourne annual GHI fallback (kWh/m²/yr) — used when no irradiance file provided
MELBOURNE_DEFAULT_GHI_KWH_M2_YR = 1850.0

# ── Stage 3 Thermal Physics ───────────────────────────────────────────────────
# Centralised here so sensitivity analysis can vary them without editing source.

# Fraction of the absorbed-solar delta (Stage 2 cool roof benefit) that conducts
# to the interior is derived PER BUILDING from its roof insulation, following the
# roof-only heat-ingress framing in Maggie's model (roof-only-heat-ingress-model):
#   U_roof   = 1 / R_roof                       (W/m²K)
#   fraction = U_roof / (U_roof + H_OUTSIDE)    (unitless)
# H_OUTSIDE is the combined convective + radiative outdoor surface coefficient.
# Worked values:  R0.5 → 0.074,  R2.5 → 0.0155,  R3.2 → 0.012.
# The R2.5 default reproduces the previous single 0.016 constant, so well-insulated
# stock is unchanged while poorly-insulated stock now correctly shows more benefit.
# Produces ~200–600 kWh/yr for a typical Melbourne house, consistent with CSIRO
# "Cool Roofs for Australian Homes" (2012).
# TODO: validate against Stuart's NatHERS runs or AS/NZS 4859.1 simulation.
#
# H_OUTSIDE_W_M2K is now the FALLBACK value used when no local wind speed is
# available (e.g. NASA POWER / Melbourne-default irradiance paths, which carry
# no BARRA2 wind data). It equals the ISO 6946 standard external surface
# coefficient (Rse = 0.04 m²K/W) — a fixed, wind-independent building-code
# default, not a Melbourne-specific measurement.
#
# When BARRA2 wind data IS available, h_out is instead computed per suburb
# from the local mean wind speed via the McAdams (1954) simple forced-
# convection correlation for an exterior building surface — widely used in
# building energy simulation (e.g. EnergyPlus's "SimpleCombined" exterior
# convection algorithm):
#   h_out = H_OUT_WIND_INTERCEPT_W_M2K + H_OUT_WIND_SLOPE_W_M2K_PER_MS * V
# Valid roughly over the 0–5 m/s range McAdams fit (typical suburban 10 m
# wind); not re-validated here for Australian roof geometries specifically.
# TODO: validate against Stuart's NatHERS runs or AS/NZS 4859.1 simulation.
H_OUTSIDE_W_M2K = 25.0
H_OUT_WIND_INTERCEPT_W_M2K = 5.7
H_OUT_WIND_SLOPE_W_M2K_PER_MS = 3.8

# Per-building roof thermal resistance R_roof (m²·K/W). Stage 1 gives us no
# construction-age field, so R_roof is inferred from the attributes we do have
# (building_type, levels, roof_material). This is a documented assumption for
# sensitivity analysis, NOT a measured value — see README known limitations.
R_ROOF_DEFAULT = 2.5  # unknown / missing attributes → assume modern insulated stock

R_ROOF_BY_CATEGORY: dict[str, float] = {
    "commercial":  1.5,  # metal deck, variable insulation
    "residential": 2.5,  # modern detached/low-rise default
}

# Metal-roofed residential stock skews older/less-insulated — nudge R_roof down
# one step. Weak proxy (material, not age); documented as an assumption.
R_ROOF_METAL_RESIDENTIAL = 1.5

# Extra attenuation multiplier for 4+ storey buildings — greater thermal mass and
# multiple floor slabs further reduce the roof-to-occupant heat path, on top of
# the R_roof fraction.
MULTISTOREY_ATTENUATION = 0.5

# Fraction of interior heat gain from the roof that drives active cooling demand.
# The remainder is offset by natural ventilation, thermal mass buffering, or night
# purging. Based on NatHERS 6-star house modelling for Melbourne climate.
COOLING_FRACTION = 0.70

# Fraction of interior heat LOSS through the roof (in winter) that drives active
# heating demand.  Same NatHERS basis as COOLING_FRACTION — the other 30 % is
# assumed offset by thermal mass, internal gains, and solar gain through windows.
# TODO: validate against Stuart's NatHERS runs for heating season.
HEATING_FRACTION = 0.70

# COP for typical residential split-system AC at Melbourne summer conditions.
# GEMS Determination 2019 minimum for a 3.5 kW unit.
HVAC_COP_RESIDENTIAL = 3.0

# COP for commercial/office buildings with VRF or central chiller plant.
# AIRAH DA19 commercial baseline for Melbourne office stock.
HVAC_COP_COMMERCIAL = 4.0

# ── Rate Limiting ────────────────────────────────────────────────────────────

TILE_DOWNLOAD_DELAY = 0.1  # seconds between API calls
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # exponential backoff multiplier
