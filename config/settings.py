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
    # rsdsdir = surface direct downwelling shortwave radiation flux (W/m²).
    # Diffuse is derived as rsds - rsdsdir (there is no rsdsdif file on THREDDS).
    # Used by the standalone heat-ingress weather extract (tools.fetch_heat_ingress_weather).
    "solar_irradiance_direct": "rsdsdir",
    # hurs = near-surface relative humidity (%). Used by the heat-ingress model
    # for the sky-temperature (dew-point) term.
    "relative_humidity_2m": "hurs",
}

# ── Melbourne Defaults ───────────────────────────────────────────────────────

MELBOURNE_BBOX = (-38.1, 144.5, -37.5, 145.5)  # south, west, north, east

# Victoria's real extent with a small margin. Kept for reference (most
# config/suburbs.py entries are Victorian); bbox sanity-checking itself uses
# the wider AUSTRALIA_BBOX below so an out-of-state comparison suburb (e.g.
# Parramatta, added 2026-09-14 for climate contrast) isn't rejected.
VICTORIA_BBOX = (-39.2, 140.9, -33.9, 150.2)  # south, west, north, east

# Country-wide bound for bbox sanity-checking (mainland + Tasmania, excluding
# external territories). Loose on purpose -- it exists to catch gross errors
# (wrong hemisphere, a transposed digit), not to constrain the suburb list to
# one state.
AUSTRALIA_BBOX = (-44.0, 112.0, -10.0, 154.0)  # south, west, north, east

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

# Generic solar absorptance after cool roof coating treatment (target SRI ≥ 78).
# Only a fallback now — Stages 2 and 3 use the material-specific coating below
# (COOL_ROOF_ABSORPTANCE_BY_STACK). Still used when a caller forces a single
# roof stack, and by tools/seasonal_analysis.py for buildings with no material.
COOL_ROOF_ABSORPTANCE = 0.20

# Which roof construction ("stack") each Stage 1 roof_material maps to. Shared
# by Stage 3 (picks the layered construction) and Stages 2/3 (pick the cool
# coating). `roof_tiles` is an ambiguous raw OSM tag -> concrete tile, the more
# common of the two. Anything unrecognised -> DEFAULT_ROOF_STACK.
ROOF_STACK_BY_MATERIAL: dict[str, str] = {
    "metal_dark": "metal",
    "metal_light": "metal",
    "metal": "metal",
    "metal_sheet": "metal",
    "concrete_tile": "concrete",
    "roof_tiles": "concrete",
    "terracotta": "terracotta",
    "slate": "slate",
}
DEFAULT_ROOF_STACK = "metal"

# Solar absorptance after the cool coating appropriate to each roof type. A
# cool coating on terracotta/slate cannot reach the same reflectance as one on
# steel deck or concrete tile, so one global 0.20 overstated the tile/slate
# saving and understated the metal/concrete one. Values are the
# Absorptivity_cool column of Final_Heat_Ingress_Model.ipynb's Roof_layers
# table (the team's reference), keyed like ROOF_STACK_BY_MATERIAL. The paired
# cool-coating emissivity lives in each stack's CSV (Emissivity_cool).
# NOTE: Colorbond Coolmax (SR 0.77) would be α = 0.23 for metal — swap the
# "metal" entry to model that product instead of a white coating.
COOL_ROOF_ABSORPTANCE_BY_STACK: dict[str, float] = {
    "metal": 0.068,
    "concrete": 0.068,
    "terracotta": 0.14,
    "slate": 0.14,
}

# Which cool coating product each roof stack gets — Stage 3's `coating_type`
# output column, so the economics can price the right product per building.
# Three coatings: one for metal, one for concrete tile, one shared by
# terracotta and slate (the notebook gives those two identical coated α/ε).
COOL_COATING_BY_STACK: dict[str, str] = {
    "metal": "metal_roof_coating",
    "concrete": "concrete_tile_coating",
    "terracotta": "terracotta_slate_coating",
    "slate": "terracotta_slate_coating",
}

# Stage 3 `roof_type` splits metal-stack roofs without an explicit
# metal_light/metal_dark label on current absorptance. 0.55 is the midpoint
# of the notebook's Steel_light (0.30) and Steel_dark (0.80) absorptivities.
ROOF_TYPE_METAL_DARK_MIN_ABSORPTANCE = 0.55

# Colorbond Coolmax re-roof scenario (run_stage3 --coolmax). Supplier-quoted
# solar reflectance 0.77 -> absorptance 0.23. Thermal emittance 0.85 matches
# Input Tables/material_properties_table4.csv (Cool Roof Cost Benefit Analysis
# Vol 4, cool and control roofs); kept per team decision 2026-10-01. Complete replacement: the
# baseline is each building's existing roof (own construction + current
# absorptance), the scenario a new Coolmax steel-deck roof.
COOLMAX_SOLAR_REFLECTANCE = 0.77
COOLMAX_ABSORPTANCE = round(1.0 - COOLMAX_SOLAR_REFLECTANCE, 3)
COOLMAX_EMISSIVITY = 0.85

# Victorian grid emissions intensity (kg CO2-e per kWh), AEMO 2023
GRID_EMISSIONS_FACTOR_KG_KWH = 0.79

# Melbourne annual GHI fallback (kWh/m²/yr) — used when no irradiance file provided
MELBOURNE_DEFAULT_GHI_KWH_M2_YR = 1850.0

# ── Stage 3 Thermal Physics ───────────────────────────────────────────────────
# Centralised here so sensitivity analysis can vary them without editing source.
#
# Stage 3 runs a transient 1-D finite-volume heat-ingress model through a layered
# roof for every building (stage3_thermal/heat_ingress_model.py, ported from
# heat_ingress_model.ipynb). The per-building cool-roof saving is the difference
# between marching that model at the building's current solar absorptance and at
# COOL_ROOF_ABSORPTANCE. This replaced the earlier inferred-R_roof algebraic
# calculator (see DECISION_LOG 2026-09-10).

# BARRA2 reference year for the hourly weather the model marches over.
HEAT_INGRESS_REFERENCE_YEAR = 2007

# Indoor reference temperature (°C) the transient march holds the interior at,
# switched hour-by-hour (no floating dead-band beyond this) on outdoor temp:
# outdoor < HEAT_INGRESS_HEATING_SETPOINT_C -> heating setpoint; outdoor >=
# HEAT_INGRESS_HEATING_SETPOINT_C -> cooling setpoint. Matches the CDD/HDD
# 18 °C split used later to bucket the resulting heat flow as saving vs penalty.
HEAT_INGRESS_HEATING_SETPOINT_C = 18.0
HEAT_INGRESS_COOLING_SETPOINT_C = 20.0

# Long-wave emissivity of the outer roof surface (Stephan-Boltzmann sky exchange)
# at the building's current absorptance. Identical across every committed roof
# material (see Input Tables/*.csv) — only the cool-roof value
# (RoofStack.emissivity_cool) varies by material.
HEAT_INGRESS_ROOF_EMISSIVITY = 0.9

# Roof height above ground (m) for the wind-speed height correction. Stage 1 has
# no reliable per-building height, so this is a fixed suburban assumption.
HEAT_INGRESS_ROOF_HEIGHT_M = 6.0

# Roof pitch (degrees) used only by the cavity's adaptive natural-convection
# coefficient (h_cav_a's cos(tilt) term, Final_Heat_Ingress_Model.ipynb). Stage 1
# carries a real per-building pitch_deg, but the reference notebook itself still
# treats tilt as a single placeholder pending Table 2 integration ("Later this
# should come from Table 2") — this mirrors that scoping rather than exceeding it.
HEAT_INGRESS_ROOF_TILT_DEG = 20.0

# Rooftop PV coverage (f_pv) — fraction of roof area under solar panels, ported
# from Maggie's fixed Final_Heat_Ingress_Model.ipynb (2026-10-05). The covered
# fraction gets no direct shortwave; instead it exchanges convection across the
# panel→roof air gap and long-wave with the panel underside (panel temperature
# from the Faiman model, T_pv = T_air + G/(U0 + U1*v)). Applies to every
# scenario (current, cool coating, insulation, Coolmax) — the coating only
# reaches the uncovered (1 - f_pv) of the roof. The notebook uses 0.3; the
# pipeline default is 0.0 (no PV) because Stage 1 has no per-building PV
# detection, so suburb-wide coverage is an opt-in assumption
# (`run_stage3 --pv-coverage`).
HEAT_INGRESS_PV_COVERAGE_FRACTION = 0.0
HEAT_INGRESS_PV_EMISSIVITY = 0.90       # panel underside long-wave emissivity
HEAT_INGRESS_PV_U0_W_M2K = 25.0         # Faiman constant heat-loss coefficient
HEAT_INGRESS_PV_U1_W_S_M3K = 6.84       # Faiman wind-dependent heat-loss coefficient

# Nominal forward-Euler solver timestep (s). Clamped down at run time if the
# stability limit over the actual weather series is tighter (cavity-layer bound).
HEAT_INGRESS_SOLVER_DT_S = 40

# Hours of simulation discarded as thermal spin-up before results are integrated.
HEAT_INGRESS_SPINUP_HOURS = 48

# Layered roof constructions the model marches heat through, all four committed
# to the repo so a clone runs Stage 3 offline. Which stack a building uses is
# selected by roof_material (stage3_thermal.heat_ingress_model.stack_for_material).
# Values ported from Final_Heat_Ingress_Model.ipynb's Roof_layers table
# (2026-09-19) — each material now carries its own outer-skin thickness/density/
# heat capacity/R-value/emissivity instead of the two lumped "metal" / "tile"
# constructions used previously.
ROOF_LAYERS_CSV = PROJECT_ROOT / "Input Tables" / "Regular_Roof.csv"  # steel deck (default)
ROOF_LAYERS_TILE_CSV = PROJECT_ROOT / "Input Tables" / "Tile_Roof.csv"  # concrete tile
ROOF_LAYERS_TERRACOTTA_CSV = PROJECT_ROOT / "Input Tables" / "Terracotta_Roof.csv"
ROOF_LAYERS_SLATE_CSV = PROJECT_ROOT / "Input Tables" / "Slate_Roof.csv"

# Insulation-upgrade scenario (opt-in via run_model's insulation_r_upgrade_m2k_w
# / insulation_thickness_upgrade_m -- off by default, existing behaviour and
# output columns are unchanged unless a caller asks for this). Values are
# Final_Heat_Ingress_Model.ipynb's own two named insulation rows:
# Insulation_old (R2.5, 130 mm -- the four committed stacks' current default)
# vs Insulation_new (R4.1, 215 mm, same density/heat capacity — a thicker
# batt of the same bulk material, not a different one). Neither is tied to
# any real per-building retrofit data; this lets Stage 3 compare "repaint the
# roof" against "add more ceiling insulation" as two scenario interventions,
# same status as COOL_ROOF_ABSORPTANCE.
INSULATION_UPGRADE_R_M2K_W = 4.1
INSULATION_UPGRADE_THICKNESS_M = 0.215

# Outdoor surface film coefficient h_ext is computed hour-to-hour from the local
# BARRA2 wind speed via the McAdams (1954) simple forced-convection correlation
# for an exterior building surface (also EnergyPlus "SimpleCombined"):
#   h_ext = H_OUT_WIND_INTERCEPT_W_M2K + H_OUT_WIND_SLOPE_W_M2K_PER_MS * V_local
# V_local is the 10 m BARRA2 wind speed brought to roof height (EnergyPlus Eq. 3.84).
H_OUT_WIND_INTERCEPT_W_M2K = 5.7
H_OUT_WIND_SLOPE_W_M2K_PER_MS = 3.8

# ISO 6946 still-air external surface coefficient (W/m²K). No longer used by the
# Stage 3 pipeline (the transient model derives h_ext from hourly wind) — kept
# for tools/seasonal_analysis.py, which sweeps an algebraic U/(U+h_out) fraction.
H_OUTSIDE_W_M2K = 25.0

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

# ── Economic Analysis (tools/economic_analysis.py) ──────────────────────────
#
# Prices every building's cool-roof saving into NPV/payback, from a Stage 3
# hourly export (building_id, time_melbourne, roof_construction,
# roof_surface_area_m2, flux_base_wh_m2, flux_cool_wh_m2, flux_coolmax_wh_m2,
# electricity_saved_cool_wh, electricity_saved_coolmax_wh[, electricity_zone]).
# Sourced and maintained by Angus (economics); see DECISION_LOG 2026-10-10.
#
# MONEY AND TIME CONVENTIONS — read before changing any rate:
# * The hourly export is ONE indicative year, repeated for every year of the
#   analysis period, with prices rising each year.
# * All prices below are TODAY'S prices (2026-27), GST-inclusive.
# * A price in year t is today's price x (1 + ECONOMIC_INFLATION)^t x
#   (1 + its real growth rate)^t, so cash flows are NOMINAL dollars.
# * Discount rates are REAL (Australian Government convention) and converted
#   to nominal with the Fisher equation: r_nominal = (1+r_real)(1+INFLATION)-1.
#   Nominal flows at the nominal rate give exactly the same NPV as real flows
#   at the real rate — INFLATION ALONE DOES NOT CHANGE THE NPV, it cancels.

# Electricity distribution zone -> state, tariff. Zone names must match exactly
# (a typo stops the run and lists the options). Flat rates and VIC's ToU
# reconstruction are ESC Victorian Default Offer 2026-27 (verified against the
# Final Decision Paper cost stack to within 0.01 c/kWh). NSW/QLD/SA are AER
# Default Market Offer 2026-27, Standard Time of Use (not Solar Saver).
# Hour windows are (start, end), end exclusive, may wrap midnight, LOCAL clock
# time matching the export's time_melbourne column.
_ECONOMIC_VIC_TOU = lambda peak, off, soak: [  # ESC VDO three-period structure
    ("peak", [(16, 21)], peak),
    ("solar_soak", [(11, 16)], soak),
    ("off_peak", [(21, 11)], off),
]

ECONOMIC_ELECTRICITY_ZONES: dict[str, dict] = {
    "AusNet": dict(state="VIC", flat=0.3198, verified=True,
                   tou=_ECONOMIC_VIC_TOU(0.4760, 0.2262, 0.1762)),
    "CitiPower": dict(state="VIC", flat=0.2596, verified=True,
                       tou=_ECONOMIC_VIC_TOU(0.3829, 0.2118, 0.1662)),
    "Jemena": dict(state="VIC", flat=0.2747, verified=True,
                   tou=_ECONOMIC_VIC_TOU(0.3755, 0.2177, 0.1767)),
    "Powercor": dict(state="VIC", flat=0.2822, verified=True,
                      tou=_ECONOMIC_VIC_TOU(0.4164, 0.2250, 0.1728)),
    "UnitedEnergy": dict(state="VIC", flat=0.2735, verified=True,
                          tou=_ECONOMIC_VIC_TOU(0.4029, 0.2215, 0.1728)),
    "Ausgrid": dict(state="NSW", flat=0.3314, verified=True, tou=[
        ("peak", [(15, 21)], 0.6020),
        ("off_peak", [(21, 15)], 0.2404)]),
    "Endeavour": dict(state="NSW", flat=0.3373, verified=True, tou=[
        ("peak", [(16, 20)], 0.4657),  # listed "46.57 / 47.14*"; footnote unseen
        ("solar_soak", [(10, 14)], 0.1235),
        ("off_peak", [(20, 10), (14, 16)], 0.3569)]),
    "Essential": dict(state="NSW", flat=0.3501, verified=True, tou=[
        ("peak", [(7, 10), (15, 22)], 0.4212),
        ("off_peak", [(22, 7), (10, 15)], 0.2415)]),
    # Energex (SE Qld) rates applied to ALL of Queensland, by decision.
    "Energex": dict(state="QLD", flat=0.2797, verified=True, tou=[
        ("peak", [(16, 21)], 0.4779),  # source shows "47.79.56" — typo; 47.79 used
        ("solar_soak", [(11, 16)], 0.0698),
        ("off_peak", [(21, 11)], 0.2530)]),
    "SAPN": dict(state="SA", flat=0.4191, verified=True, tou=[
        ("peak", [(6, 10), (16, 24)], 0.5622),
        ("solar_soak", [(10, 16)], 0.1704),  # source shows "17.0431" — typo; 17.04 used
        ("off_peak", [(0, 6)], 0.3255)]),
    # Horizon Power A2 residential, 1 Jul 2026. WA's Uniform Tariff Policy means
    # Horizon Power (regional/remote, incl. Broome) charges the exact same rate
    # as Synergy's metro A1 — confirmed on wa.gov.au/organisation/energy-policy-wa
    # /household-electricity-pricing, cross-checked against horizonpower.com.au
    # /for-home/home-electricity-solutions/a2-residential-tariff/. Flat only —
    # no ToU product on the standard residential tariff.
    "HorizonPower": dict(state="WA", flat=0.332621, verified=True,
                          tou=[("flat", [(0, 24)], 0.332621)]),
    # Jacana Energy standard residential (Power and Water Corp network), valid
    # 1 Jul 2026 - 30 Jun 2027 — jacanaenergy.com.au/residential/pricing. Flat
    # is the default retail plan; an opt-in smart-meter ToU plan exists
    # (peak 9am-3pm 23.48c, off-peak 30.20c) but is not the standard tariff
    # Alice Springs buildings would be defaulted onto, so not modelled.
    "Jacana": dict(state="NT", flat=0.316788, verified=True,
                   tou=[("flat", [(0, 24)], 0.316788)]),
}

# Default zone when a file carries no electricity_zone column and --zone isn't
# passed. CitiPower = Carlton, the headline suburb.
ECONOMIC_DEFAULT_ZONE = "CitiPower"

ECONOMIC_TARIFF_STRUCTURE = "tou"  # "tou" or "flat"

# c/MJ, state averages, April 2026. Source: EnergyPlans static gas dataset —
# indicative, not an AER/ESC reference price. Daily supply charge excluded.
# TODO: confirm whether GST-inclusive (electricity rates above are).
ECONOMIC_GAS_RATE_C_PER_MJ: dict[str, float] = {
    "QLD": 2.45, "VIC": 2.58, "NSW": 2.72, "SA": 3.05,
}

# Share of the gas's energy that ends up as heat in the house; the rest goes
# up the flue. Ducted gas heaters are typically 0.60-0.85 (star rating).
ECONOMIC_GAS_HEATER_EFFICIENCY = 0.80  # TODO: cite a value

# Heating fuel scenarios to run; each produces its own set of columns.
# "gas": penalty electricity -> heat lost (x HVAC_COP_RESIDENTIAL / COOLING_FRACTION,
# the same 4.2857 effective COP Angus's audit cross-checked against Stage 3,
# DECISION_LOG 2026-10-09) -> gas burnt (/ ECONOMIC_GAS_HEATER_EFFICIENCY x 3.6 MJ/kWh).
# "electric": penalty priced as electricity, exactly as simulated.
ECONOMIC_HEATING_FUELS = ["gas", "electric"]

ECONOMIC_INFLATION = 0.025  # RBA target band midpoint (2-3%)

# REAL growth per year, i.e. ABOVE inflation. 0.0 = moves with inflation.
ECONOMIC_REAL_GROWTH_ELECTRICITY = 0.0
ECONOMIC_REAL_GROWTH_GAS = 0.0
ECONOMIC_REAL_GROWTH_INSTALL_COST = 0.0  # labour has often outpaced CPI: +0.5-1.5% plausible

# One output row per building per rate. Office of Impact Analysis convention:
# 7% real central, 3% and 10% sensitivity. 3% real also approximates a
# homeowner's mortgage-rate opportunity cost (~5.5-6% nominal).
ECONOMIC_DISCOUNT_RATES_REAL = [0.03, 0.07, 0.10]

# Years the indicative year is projected over. Coatings are reapplied on cycle
# within it; Coolmax (36-yr life) gets residual credit for its last 6 years.
ECONOMIC_ANALYSIS_PERIOD_YEARS = 30

# Reapply each coating at the end of its warranty, at the then-current
# (inflated) cost. If False, one coat only and savings stop at warranty end.
ECONOMIC_REAPPLY_AT_WARRANTY_END = True

# Credit the unused life of the last application at the end of the period,
# valued at replacement cost, so an option isn't penalised for a part-cycle.
ECONOMIC_RESIDUAL_VALUE = False

# Reflectance falls with soiling/weathering. When on, the saving in year k of
# each application is multiplied by floor + (1-floor) x exp(-(k-1)/tau)
# (year 1 = 100%); reapplying resets it to 100%.
ECONOMIC_APPLY_DEGRADATION = False

# install_cost_aud_per_m2: today's cost, GST-incl, applied to roof surface
# area. Coolmax is the PREMIUM over standard Colorbond for an owner re-roofing
# anyway.
ECONOMIC_PRODUCTS: dict[str, dict] = {
    "endure30c": dict(
        label="EnduRE30C Gen 5 radiative cooling coating",
        install_cost_aud_per_m2=35.00,  # applied cost, material + labour
        warranty_years=8,       # Easyto limited warranty, authorised applicator
        degradation_floor=0.75,  # warranty covers >30% performance drop
        degradation_tau=1.5,
    ),
    "serisolar": dict(
        label="Serisolar NEW FAST White",
        install_cost_aud_per_m2=35.00,  # applied cost, material + labour
        warranty_years=5,       # slopes > 5%, Serisolar-authorised installer
        degradation_floor=0.75,  # TODO literature
        degradation_tau=1.5,
    ),
    "coolmax": dict(
        label="COLORBOND Coolmax premium over standard Colorbond",
        install_cost_aud_per_m2=11.00,  # premium only  # TODO: $10 or $11?
        warranty_years=36,      # BlueScope, up to 36 yr corrosion (roofing)
        degradation_floor=0.90,  # factory-baked finish, slower loss  # TODO
        degradation_tau=3.0,
    ),
}

# Which ECONOMIC_PRODUCTS coating each roof_construction gets (the Stage 3
# hourly export's 4-value vocabulary: concrete/metal/terracotta/slate — NOT
# the same grouping as COOL_COATING_BY_STACK above, which names the physics
# coating families rather than a commercial product).
ECONOMIC_COATING_FOR_ROOF: dict[str, str] = {
    "concrete": "endure30c",
    "metal": "endure30c",
    "slate": "serisolar",
    "terracotta": "serisolar",
}

# name -> (hourly export file, electricity zone), for `--batch`. Each gets its
# own results subfolder, plus a combined all_areas_summary.csv. The zone also
# sets the state and so the gas price. IMPORTANT: timestamps must be LOCAL
# clock time for that area so the time-of-use windows line up (see DECISION_LOG
# 2026-10-09 — Queensland exports need a per-suburb timezone fix first).
ECONOMIC_RUNS: dict[str, tuple[str, str]] = {}

# Occupancy adjustment (default off — every hour counted in full).
# factor(day, hour) = 1 - dial x (1 - profile[day][hour]); dial 0 -> no
# adjustment, dial 1 -> profile applied in full. Rows: 0=Monday..6=Sunday;
# columns: hour 0-23. Hours set to 0 overstate the reduction — an empty house
# still heats up, and the roof still cuts the load on return.
# TODO: confirm the simulation doesn't already apply a thermostat schedule.
ECONOMIC_OCCUPANCY_DIAL = 0.0
_ECONOMIC_AWAY = [0.0 if 8 <= h < 15 else 1.0 for h in range(24)]  # out 8am-3pm
ECONOMIC_OCCUPANCY_PROFILE = [_ECONOMIC_AWAY] * 5 + [[1.0] * 24] * 2  # Mon-Fri away
ECONOMIC_OCCUPANCY_APPLIES_TO_COOLING = True
ECONOMIC_OCCUPANCY_APPLIES_TO_HEATING = True

# Building whose year-by-year cash flows are written to the cashflow-example
# CSV (None = first building in the file).
ECONOMIC_EXAMPLE_BUILDING = None
