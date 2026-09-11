# Raising Rooves Model

Monash University Final Year Project (2026): a data pipeline for modelling
cool roof intervention benefits across Melbourne suburbs.

Team: Ryan, Seamus, Angus, Flynn, Maggie, Gabrielle  
Supervisor: Stuart

## Quickstart (no API keys needed)

New to the project? You can see real output in about five minutes. A small
Stage 1 fixture for Carlton is tracked in git under `data/samples/`:

```bash
pip install -r requirements.txt

# Copy the sample Stage 1 output into place
# PowerShell:
Copy-Item data/samples/stage1_carlton.parquet data/output/
# macOS/Linux:
cp data/samples/stage1_carlton.parquet data/output/

# Irradiance comes from NASA POWER automatically (free, no key)
python -m stage2_irradiance.run_stage2 --suburb Carlton
# Stage 3 uses the committed hourly-weather sample when a live BARRA2 fetch
# isn't possible; ~5-10 min for the transient model over all buildings
python -m stage3_thermal.run_stage3 --suburb Carlton
python -m tools.visualise_results --suburb Carlton
```

Running Stage 1 yourself (fresh tile downloads) needs a `GOOGLE_MAPS_API_KEY` —
see Setup below.

## Current Status

- Stage 1 roof segmentation: working. Pitch defaults calibrated against Gemini
  validation (507 buildings, Aug 2026): residential 12°, gabled/hipped 15°.
  Per-suburb classifier quality multipliers in `SUBURB_CLASSIFIER_QUALITY`.
- Roof pitch: assumed only, from OSM `roof:shape`/`building_type`/storeys —
  see `pitch_basis` in Stage 1 Columns below. DSM/LiDAR pitch measurement was
  trialled and dropped (elevation data wasn't precise enough); the DSM tooling
  has been removed from the codebase.
- Stage 2 irradiance and cool roof delta: working. BARRA2 via NCI THREDDS
  OPeNDAP is the primary source (no auth needed — discovered Aug 2026), with
  NASA POWER and user CSV as fallbacks. A pre-extracted hourly BARRA2 CSV can
  be ingested via `--barra-csv`.
- Stage 3 thermal modelling: working. Per-building **transient finite-volume**
  roof heat-ingress model (`stage3_thermal/heat_ingress_model.py`) over a full
  hourly BARRA2 year — reports cooling-season electricity saved and the winter
  heating penalty separately. Replaced the inferred-R_roof calculator Sep 2026.
- Seasonal analysis: `tools/seasonal_analysis.py` shows the monthly
  cooling-benefit vs heating-penalty tradeoff with R_roof sensitivity sweeps.
- Gemini validation database: 507 buildings assessed (Clayton 302, Carlton 205)
  and stored at `data/output/experiments/`. Resume-safe — never re-spends API
  credits on buildings already assessed.
- Persistence: no application database. Outputs are CSV, Parquet, JSON, PNG,
  HTML, and cached raw files under `data/`.

Important note: `data/raw/footprints/buildings_index.gpkg` is a generated
GeoPackage spatial index used for fast local footprint lookup. It is not the
project database and should not be deleted unless you are happy to rebuild it.

## What The Pipeline Does

For a configured Melbourne suburb, the pipeline:

1. Computes a satellite tile grid from the suburb bounding box.
2. Downloads or reuses Google Maps satellite tiles.
3. Queries building footprints from OpenStreetMap and/or local footprint data.
4. Classifies roof colour/material from satellite pixels where tags are missing.
5. Assigns roof pitch from assumptions (OSM `roof:shape`, `building_type`, storeys).
6. Joins buildings to annual solar irradiance (NASA POWER, user CSV, or BARRA2).
7. Estimates per-building reduction in absorbed solar energy from a cool roof
   treatment.
8. Converts absorbed solar reduction to cooling electricity savings via thermal
   model (Stage 3).
9. Produces interactive map, summary charts, and HTML report (visualise_results).

## Data Flow

```text
config/suburbs.py
  suburb centroid + bbox
        |
        v
Stage 1: roof segmentation
  compute tile grid
  download/reuse Google satellite tiles
  query OSM footprints and/or local GeoPackage/SHP/GeoJSONL
  classify roof pixels
  assign assumed pitch
        |
        v
data/output/stage1_{suburb}.parquet
data/output/stage1_{suburb}.csv
data/output/stage1_{suburb}_polygons.json
data/output/stage1_{suburb}_annotated.png
        |
        v
Stage 2: irradiance + cool roof delta
  try BARRA2 OPeNDAP → BARRA2 CSV (--barra-csv) → user CSV →
  NASA POWER → Melbourne default GHI
  calculate energy/co2 reduction
        |
        v
data/output/stage2_{suburb}.parquet
data/output/stage2_{suburb}.csv
        |
        v
Stage 3: transient roof heat-ingress model
  resolve suburb hourly BARRA2 weather (cache / OPeNDAP fetch / offline sample)
  per building: march the layered-roof model at current vs cool absorptance
  difference → cooling-season saving + winter heating penalty → electricity, CO2
        |
        v
data/output/stage3_{suburb}.parquet
data/output/stage3_{suburb}.csv
        |
        v
tools.visualise_results
  choropleth map, summary charts, HTML report
        |
        v
data/output/stage3_{suburb}_map.html      (stage2_ prefix if only Stage 2 exists)
data/output/stage3_{suburb}_summary.png
data/output/stage3_{suburb}_report.html
```

## Data Needed

### Required

- Python dependencies from `requirements.txt`.
- `.env` with `GOOGLE_MAPS_API_KEY` for fresh satellite tile downloads.
- A suburb entry in `config/suburbs.py`.

### Strongly Recommended

- Local footprint index:
  `data/raw/footprints/buildings_index.gpkg` (~250 MB, built locally — see below)
- Source footprint file for rebuilding the index:
  `data/raw/footprints/melbourne_overture.geojsonl` (~650 MB for the Greater
  Melbourne bbox). This is a line-delimited GeoJSON export of Overture Maps
  building footprints (Microsoft ML Buildings dataset) for the Melbourne area.
  It is too large for git and is not built by default.

  Fetch it directly from Overture Maps' public dataset (no API key, no
  teammate hand-off needed — Overture publishes to a public, unsigned S3
  bucket):

  ```bash
  pip install overturemaps
  python -m overturemaps download \
    --bbox=144.8110,-38.1590,145.2350,-37.6310 \
    -f geojsonseq -t building \
    -o data/raw/footprints/melbourne_overture.geojsonl
  ```

  The bbox above covers all Greater Melbourne suburbs in `config/suburbs.py`
  (excludes the regional Victoria suburbs, which would pull in a much larger,
  mostly-empty area). Adjust it if you add suburbs outside that box. This
  pulled ~980k buildings in under a minute on a normal connection.

  Then build the spatial index once:
  `python -m tools.build_footprint_index`

  (Alternative source, if you'd rather not install `overturemaps`: ask a
  teammate for a copy of `melbourne_overture.geojsonl` or the built `.gpkg`
  directly.)
- Real irradiance CSV with columns:
  `lat, lon, annual_ghi_kwh_m2`
- True suburb boundary polygon for final reporting. The current config uses
  rectangular bboxes, not real suburb polygons.

### Optional API Keys

- `GEMINI_API_KEY` for the opt-in Gemini roof-assessment experiment (free tier
  at https://aistudio.google.com/app/apikey).

## Setup

```bash
pip install -r requirements.txt

# Copy the env template, then fill in your keys
# PowerShell:
Copy-Item .env.example .env
# macOS/Linux:
cp .env.example .env
```

Optional: `pip install -e .` installs the project as a package and provides
`rooves-stage1`, `rooves-stage2`, `rooves-stage3`, `rooves-visualise`, etc. as
console commands equivalent to the `python -m ...` forms.

You need satellite tiles for Stage 1. Two options:

**Option A — no API key (teammates):** the team's pre-fetched tiles live in
the Google Drive folder "Raising Rooves - Shared Data", shared read-only
with your Monash account. Download the suburb zip manually from Drive
(Shared with me) and extract it so tiles land in `data/raw/tiles/{suburb}/`
(e.g. `data/raw/tiles/clayton/clayton_19_*.png`).

(`tools.download_tiles` automates this, but it downloads anonymously and only
works when the Drive files are link-shared — they aren't, by team decision.)

**Option B — own API key:** add a Google Maps Static API key to `.env`:

```text
GOOGLE_MAPS_API_KEY=your_key_here
```

Verify the config imports:

```bash
python -c "from config.settings import *; print('Config OK')"
```

## Running Stage 1

```bash
# Full run using OSM plus any auto-detected local supplement index
python -m stage1_segmentation.run_stage1 --suburb "Clayton"

# Debug logging
python -m stage1_segmentation.run_stage1 --suburb "Clayton" --debug

# Reuse existing tiles and skip tile download
python -m stage1_segmentation.run_stage1 --suburb "Clayton" --skip-download

# Use only a local footprint file/index and skip OSM
python -m stage1_segmentation.run_stage1 --suburb "Clayton" \
  --footprint-file data/raw/footprints/buildings_index.gpkg

# Merge a local footprint file with OSM
python -m stage1_segmentation.run_stage1 --suburb "Clayton" \
  --merge-footprint-file data/raw/footprints/buildings_index.gpkg

# List configured suburbs
python -m stage1_segmentation.run_stage1 --list-suburbs
```

Stage 1 auto-detects `data/raw/footprints/buildings_index.gpkg` when it exists
and uses it as a supplement unless `--footprint-file` is passed. In supplement
mode, Stage 1 tries OSM first, then falls back to the local index if Overpass is
blocked or rejects the query. Use `--footprint-file` to skip OSM entirely.

### Experimental Gemini + OSM Roof Assessment

This is an opt-in comparison workflow. It does not replace or modify the normal
Stage 1 outputs. It reads existing Stage 1 tables, polygon sidecars, and cached
Google satellite tiles, sends small OSM-outlined building crops to Gemini, and
writes separate comparison files under `data/output/experiments/`.

```bash
# Build crop metadata only; no Gemini API call
python -m tools.run_gemini_osm_experiment --suburb Clayton --max-buildings 5 --dry-run

# Send a small bounded sample to Gemini
python -m tools.run_gemini_osm_experiment --suburb Clayton --max-buildings 5
```

Outputs:

- `data/output/experiments/gemini_osm_stage1_{suburb}.jsonl`
- `data/output/experiments/gemini_osm_stage1_{suburb}.csv`

The Gemini pitch value is a coarse visual estimate only — nadir satellite
imagery cannot measure pitch, and we don't have a DSM/LiDAR source to cross-
check it against. The experiment defaults to high Gemini media resolution
because small roof details are important for this task. Its `qa_action`
field is the local safety gate: boundary mismatches route to manual review,
non-flat visual pitch is flagged `pitch_uncertain` (low confidence, not a
follow-up action), and flat/attribute-only results may be accepted when
confidence and image quality are high.

### Stage 1 Outputs

| File | Contents |
| --- | --- |
| `stage1_{suburb}.csv` | Per-building CSV for inspection and reports |
| `stage1_{suburb}.parquet` | Canonical Stage 1 table used by Stage 2 |
| `stage1_{suburb}_polygons.json` | Building polygon sidecar used by `tools.visualise_results` for map overlays |
| `stage1_{suburb}_annotated.png` | Stitched satellite image with building overlays |

### Stage 1 Columns

| Column | Description |
| --- | --- |
| `suburb` | Configured suburb name |
| `building_id` | Source footprint id |
| `roof_id` | Stable project roof id |
| `area_m2` | Building footprint area in square metres |
| `lat`, `lon` | Building centroid |
| `source` | Footprint source, e.g. `osm`, `vicmap`, or `msft` |
| `building_type` | Building tag/type where available |
| `levels` | Number of levels where available |
| `roof_material` | OSM/source tag or HSV classifier estimate |
| `roof_colour` | OSM/source tag or HSV classifier estimate |
| `roof_shape` | Roof shape tag where available |
| `pitch_deg` | Assumed roof pitch in degrees (see `_assumed_pitch_deg` in `stage1_segmentation/pipeline.py`) |
| `pitch_basis` | Which rule produced `pitch_deg`: `roof_shape:<tag>`, `levels>=4`, `building_type:<tag>`, or `residential_default` |
| `pitch_source` | Always `assumed` — pitch is never measured (DSM/LiDAR pitch extraction was trialled and dropped, see Known Limitations) |
| `classifier_confidence` | `1.0` for source tags, `0.0` unclassified, otherwise HSV confidence |

## Boundary And Annotation Behaviour

The current suburb definitions use rectangular bboxes. Satellite tiles are fixed
to a web-map grid, so the downloaded imagery always extends beyond the bbox.
Stage 1 then expands the footprint query to match the visible tile area so edge
buildings have overlays.

That means current Stage 1 CSV/parquet outputs can include buildings outside the
configured bbox. For the latest Clayton run, 7,762 buildings were output:

- 6,976 centroid-inside the configured Clayton bbox
- 786 centroid-outside the configured Clayton bbox

For final policy/reporting work, the better design is:

1. Keep the tile buffer for imagery and classification.
2. Use a true suburb polygon, preferably ABS SA2 or another authoritative
   boundary.
3. Add `inside_suburb` and/or intersection-area weighting.
4. Report canonical totals for buildings inside the analysis boundary.
5. Draw the suburb boundary on the annotation.
6. Show buffer buildings muted or omit them from the presentation annotation.

## Running Stage 2

```bash
# Default: BARRA2 OPeNDAP (no auth needed — NCI THREDDS serves it publicly),
# then --barra-csv, then user CSV, then NASA POWER, then Melbourne default.
python -m stage2_irradiance.run_stage2 --suburb "Clayton"

# Pre-extracted hourly BARRA2 CSV (fast, offline; 8,760 rows for 2007)
python -m stage2_irradiance.run_stage2 --suburb "Clayton" \
  --barra-csv data/raw/barra/clayton_2007_hourly.csv

# Use a prepared irradiance grid CSV
python -m stage2_irradiance.run_stage2 --suburb "Clayton" \
  --irradiance-file data/raw/barra/clayton_ghi.csv

# Debug logging
python -m stage2_irradiance.run_stage2 --suburb "Clayton" --debug
```

Irradiance CSV format:

```csv
lat,lon,annual_ghi_kwh_m2
-37.915,145.122,1850.0
```

BARRA2 hourly CSV format (one row per hour; extra columns are ignored):

```csv
time_UTC,rsds_total_Wm2,temp_C
2007-01-01T00:00:00Z,821.13,25.18
```

Annual GHI is computed from the hourly flux (mean W/m² × 8760 / 1000).
Monthly irradiance + temperature stats are saved to
`stage2_{suburb}_climate.parquet` for Stage 3 and seasonal analysis.

### Stage 2 Outputs

Stage 2 appends these columns to the Stage 1 table:

| Column | Description |
| --- | --- |
| `azimuth_deg` | Building heading (0-360° CW from North): bearing of the outward normal to the long side of the minimum-rotated-rectangle fit to the footprint. Ambiguous by 180° — footprint shape alone can't tell front from back |
| `wall_length_main_m` | Length of the rectangle's long side (assumes a rectangular footprint) |
| `wall_length_perp_m` | Length of the rectangle's short (perpendicular) side |
| `wall_ratio` | `wall_length_main_m / wall_length_perp_m` — ≥1; higher means a longer, narrower footprint |
| `annual_ghi_kwh_m2` | Annual global horizontal irradiance at/near the building |
| `absorptance_before` | Estimated pre-treatment solar absorptance |
| `roof_surface_area_m2` | Roof surface area = footprint area / cos(pitch) |
| `energy_incident_kwh_yr` | Annual incident solar energy on the footprint |
| `energy_saved_kwh_yr` | Reduced absorbed solar energy after cool roof treatment |
| `co2_saved_kg_yr` | CO2 avoided using the configured grid emissions factor |
| `mean_wind_speed_ms` | Suburb-uniform BARRA2 10 m wind speed (`sfcWind`), or empty when the irradiance source isn't BARRA2. Feeds Stage 3's wind-dependent `h_out` |

## Running Stage 3

Stage 3 runs a **transient 1-D finite-volume heat-ingress model** through a
layered roof for every building (ported from `stage3_thermal/heat_ingress_model.ipynb`),
marching it once at the building's current solar absorptance and once at the
cool-roof target, and differencing the plaster→interior heat flow.

```bash
python -m stage3_thermal.run_stage3 --suburb "Carlton"
python -m stage3_thermal.run_stage3 --suburb "Carlton" --year 2007 --debug
python -m stage3_thermal.run_stage3 --suburb "Carlton" --weather-csv path/to/hourly.csv
```

Prerequisites:

- Stage 2 output (`data/output/stage2_{suburb}.parquet`).
- An **hourly** BARRA2 weather series for the suburb, auto-resolved in this order:
  `--weather-csv` → cached `data/raw/barra/heat_ingress_{suburb}_{year}.csv` →
  BARRA2 OPeNDAP fetch (`tools.fetch_heat_ingress_weather`; needs `xarray` +
  `pydap` + network, ~5 min/suburb, then cached) → committed
  `data/samples/heat_ingress_{suburb}_{year}.csv` (offline fallback; Carlton only).

Runtime is ~5–10 min per suburb (≈790k solver steps × two scenarios, vectorised
across all buildings).

### Stage 3 Outputs

Stage 3 appends these columns to the Stage 2 table:

| Column | Description |
| --- | --- |
| `roof_heat_ingress_base_kwh_m2_yr` | Annual roof→interior heat per m² roof, at the current absorptance (signed) |
| `roof_heat_ingress_cool_kwh_m2_yr` | Same, at `COOL_ROOF_ABSORPTANCE` (0.20) |
| `cooling_season_heat_avoided_kwh_yr` | Interior heat the cool roof keeps out during hours with outdoor temp ≥ 18 °C, × roof surface area |
| `heating_season_heat_added_kwh_yr` | Wanted winter solar gain the cool roof rejects (hours < 18 °C), × roof surface area |
| `cooling_fraction_applied` / `hvac_cop` | Audit — `COOLING_FRACTION` and COP by building type |
| `electricity_saved_kwh_yr` | Cooling-season electricity saved = `cooling_season_heat_avoided × 0.70 / COP` |
| `heating_penalty_electricity_kwh_yr` | Extra winter heating electricity = `heating_season_heat_added × 0.70 / COP` |
| `net_electricity_saved_kwh_yr` | `electricity_saved − heating_penalty_electricity` |
| `co2_electricity_saved_kg_yr` | CO2 avoided from the cooling-season electricity saving |
| `net_co2_electricity_saved_kg_yr` | CO2 avoided net of the heating penalty |

Output files:

- `data/output/stage3_{suburb}.parquet`
- `data/output/stage3_{suburb}.csv`

### Stage 3 Model

Per building, one forward-Euler finite-volume march (`dt ≈ 40 s`, clamped below
the cavity-layer stability limit) through four layers — steel deck / bulk
insulation / ceiling cavity / plaster (`Input Tables/Regular_Roof.csv`, one
stack for every building). Boundary conditions:

```
outer:  q = α·(rsdsdir + rsdsdif) + h_ext·(T_out − T_steel) + ε·σ·((T_out − 10)⁴ − T_steel⁴)
h_ext:  5.7 + 3.8·V_local        (McAdams; V_local = BARRA2 10 m wind brought to roof height)
inner:  q = (T_plaster − T_indoor) / (R_plaster/2 + 1/h_i),   T_indoor = 20 °C fixed
```

The cool-roof saving is `march(α_before) − march(0.20)`, integrated per hour and
split by that hour's outdoor temperature against the 18 °C cooling/heating base.
The first 48 h are discarded as thermal spin-up. Per-building kWh =
per-m² result × `roof_surface_area_m2`.

| Parameter | Value | Source |
| --- | --- | --- |
| Roof stack | steel 0.42 mm / insulation 164 mm / cavity 300 mm / plaster 13 mm | `Input Tables/Regular_Roof.csv` |
| Cavity R (downward flow) | 0.23 m²·K/W | ISO 6946 unventilated airspace (notebook transient value) |
| Cooling / heating fraction | 0.70 / 0.70 | NatHERS 6-star Melbourne basis |
| HVAC COP | 3.0 residential, 4.0 commercial | GEMS 2019 / AIRAH DA19 |
| Sky temperature | `T_out − 10 K` | Notebook long-wave assumption |

**Known limitations:**

- All buildings share one roof construction; only absorptance, pitch and area
  vary. `roof_material` is not yet mapped to different layer stacks.
- The 18 °C hourly cooling/heating split, the 0.70 demand fractions, the sky-
  temperature depression and the McAdams `h_ext` correlation are standard
  building-simulation defaults, not validated against Stuart's NatHERS runs.
- No separate heating COP — `HVAC_COP_*` is reused for the heating penalty.
- `azimuth_deg` is carried through but numerically inert (`rsdsdir` is treated
  as already incidence-corrected); a per-plane incidence projection is future work.
- One BARRA2 reference year (2007), suburb-uniform (`~11 km grid`).

## Visualisation

Produces an interactive map, summary charts, and HTML report. Uses Stage 3
output when it exists, otherwise Stage 2 (`--stage2-only` forces Stage 2).

```bash
python -m tools.visualise_results --suburb "Carlton"
python -m tools.visualise_results --suburb "Carlton" --stage2-only
python -m tools.visualise_results --suburb "Carlton" --debug
```

Outputs written to `data/output/` (prefix is `stage3_` or `stage2_` matching
the data used):

| File | Description |
| --- | --- |
| `stage3_{suburb}_map.html` | Interactive choropleth — buildings coloured by energy saved |
| `stage3_{suburb}_summary.png` | 2×2 chart panel (distribution, by material, counts, summary stats) |
| `stage3_{suburb}_report.html` | HTML report with KPI tiles, embedded chart, and map link |

### Comparing Suburbs

For FYP reporting across every suburb with outputs:

```bash
python -m tools.compare_suburbs            # best available stage per suburb
python -m tools.compare_suburbs --stage 2  # force Stage 2 data
```

### Seasonal Analysis

Monthly cool roof benefit vs heating penalty with R_roof sensitivity sweeps.
Reads `stage2_{suburb}_climate.parquet` and `stage2_{suburb}.parquet`.

```bash
python -m tools.seasonal_analysis --suburb Clayton
python -m tools.seasonal_analysis --suburb Clayton --r-values 0.5,1.0,2.5,5.0
python -m tools.seasonal_analysis --list-suburbs
```

Writes `stage2_{suburb}_seasonal.png`. Key finding (Aug 2026): in Melbourne,
the winter heating penalty is the same magnitude as the summer cooling
benefit — net annual effect is near zero.

### Heat Ingress Model Notebook

`heat_ingress_model.ipynb` is a standalone transient (finite-volume) roof
heat-ingress model — hourly heat flux through a layered roof for a single
building, used to sanity-check the Stage 3 steady-state assumptions. It reads
three CSVs from `Input Tables/` (next to the notebook, committed to the repo):

- `material_properties_table4.csv`, `Regular_Roof.csv` — roof layer / material
  properties.
- `barra2_{lat}_{lon}_{year}.csv` — a single-point hourly BARRA2 weather
  extract (`time_UTC, rsdsdir_Wm2, rsdsdif_Wm2, temp_K, rel_humidity_percent,
  wind_ms, rsds_total_Wm2, temp_C`).

The BARRA2 CSV is regenerated straight from the same public NCI THREDDS
OPeNDAP endpoint Stage 2 uses (no login) — no Google Drive dependency:

```bash
python -m tools.fetch_heat_ingress_weather --lat -37.91 --lon 145.13 --year 2007
# writes Input Tables/barra2_-37.91_145.13_2007.csv (8,760 rows)
```

Variables pulled: `rsds`, `rsdsdir` (diffuse = rsds − rsdsdir), `tas`, `hurs`,
`sfcWind`. Needs `xarray` + `pydap` (`pip install pydap`).

### Downloading Shared Tiles

```bash
python -m tools.download_tiles --suburb clayton
python -m tools.download_tiles --all
```

### Gemini Roof Assessment

See the Experimental Gemini + OSM Roof Assessment section below. Results are
analysed with:

```bash
python tools/analyse_gemini_results.py Clayton
```

## Running The Full Pipeline

```bash
python -m stage1_segmentation.run_stage1 --suburb Carlton \
  --merge-footprint-file data/raw/footprints/buildings_index.gpkg
python -m stage2_irradiance.run_stage2 --suburb Carlton
python -m stage3_thermal.run_stage3 --suburb Carlton
python -m tools.visualise_results --suburb Carlton
```

## BARRA2 And Grid Handling

There is no fixed 12 by 12 grid assumption in the code.

Current behaviour:

- BARRA2 OPeNDAP path (active since Aug 2026) fetches hourly rsds (irradiance),
  tas (temperature), and sfcWind (10 m wind speed) for the nearest ~11 km grid
  cell to the suburb centroid. No NCI authentication needed — the NCI THREDDS
  server serves it publicly.
  Data is cached under `data/raw/barra/{solar_irradiance,temperature_2m,wind_speed_10m}/`.
  Monthly stats and annual GHI are computed from the hourly values; mean wind
  speed feeds Stage 3's wind-dependent `h_out`.
- `--barra-csv` path ingests a pre-extracted hourly BARRA2 CSV (one row per
  hour: `time_UTC, rsds_total_Wm2, temp_C`, optionally `wind_ms`) — useful
  offline or for grid cells extracted externally. Wind is used when the
  `wind_ms` column is present.
- NASA POWER (fallback): samples a grid across the suburb bbox at 0.1° spacing
  and caches results under `data/raw/nasa_power/`. At ~50 km resolution, most
  Melbourne suburbs will return one or a few data points.
- CSV irradiance input accepts any number of rows.
- Building centroids are matched to the nearest CSV row using latitude/longitude
  distance.

BARRA2 is ~11 km resolution (AUS-11 grid), so a suburb bbox usually lands on
one grid cell; the scalar GHI applies uniformly to all buildings in that suburb.

## Cool Roof Physics

Solar absorptance before treatment is estimated from `roof_colour` first, then
`roof_material`, then a conservative fallback.

| Roof colour/material | Absorptance before treatment |
| --- | --- |
| White | 0.25 |
| Light grey | 0.50 |
| Dark grey / dark metal | 0.85 |
| Red / terracotta | 0.75 |
| Light metal | 0.45 |
| Unknown | 0.75 |

Cool roof treatment target absorptance:

```text
COOL_ROOF_ABSORPTANCE = 0.20
```

Calculation:

```text
roof_surface_area_m2 = area_m2 / cos(pitch_deg)
energy_incident      = annual_ghi_kwh_m2 * area_m2
energy_saved         = energy_incident * (absorptance_before - 0.20)
co2_saved            = energy_saved * 0.79 kg/kWh
```

`energy_incident` uses footprint area, not roof surface area, because GHI is
horizontal irradiance. Roof surface area is still useful for material quantity
and cost estimates.

## QA

```bash
python -m pytest tests/ -x
```

## Latest Clayton Validation Snapshot

Latest local run: 2026-04-29.

Stage 1 was run with OSM as the primary source plus the local footprint
GeoPackage supplement. Outputs:

- `stage1_clayton.csv`: 8,024 buildings
- `stage1_clayton.parquet`
- `stage1_clayton_polygons.json`
- `stage1_clayton_annotated.png`: 12,736 x 12,224 PNG

Stage 1 validation:

- 0 duplicate `building_id`
- 0 duplicate `roof_id`
- 7,579 HSV-classified roofs
- 445 unclassified roofs
- Source mix: 2,827 `osm` rows and 5,197 `msft` supplement rows

Stage 2 was first run against NASA POWER (1,646 kWh/m²/yr — 11% lower than the
1,850 kWh/m²/yr Melbourne default). Since Aug 2026, BARRA2 OPeNDAP is the
primary source: Clayton 2007 hourly data gives 1,669 kWh/m²/yr, in good
agreement with NASA POWER. Results are cached under `data/raw/barra/` and
`data/raw/nasa_power/`.

Outputs:

- `stage2_clayton.csv`
- `stage2_clayton.parquet`

These numbers are suitable for pipeline validation, not final policy
conclusions.

## Known Limitations

1. Current suburb boundaries are rectangular bboxes, not true suburb polygons.
2. Current canonical outputs can include tile-buffer buildings outside the bbox.
3. OSM Overpass can fail or reject large bbox queries; local footprints are
   needed for reliable reruns.
4. HSV roof classification is heuristic. Validated against Gemini 2.5 Flash
   (507 buildings, Aug 2026): colour agreement 39–54% by suburb (greys work,
   blue/green/white do not); material agreement 0% — nadir imagery cannot
   distinguish material. Confidence is scaled per suburb via
   `SUBURB_CLASSIFIER_QUALITY`.
5. Pitch is assumed, not measured. Defaults calibrated against Gemini
   validation: residential 12°, gabled/hipped 15°, shallow types 10°.
   DSM/LiDAR-based pitch extraction was trialled and removed — the elevation
   data available (ELVIS 1 m, COP30) wasn't precise enough for defensible
   per-building plane fits. The `pitch_basis` column records which rule
   produced each `pitch_deg` value. Pitch only affects `roof_surface_area_m2`
   (costing), not energy numbers, which correctly use footprint area with
   horizontal irradiance.
6. Stage 2 currently uses a single-year BARRA2 climate sample (2007); a proper
   30-year climate normal requires a longer run
   (`--start-year 1990 --end-year 2020`).
7. Stage 3's transient model uses **one roof construction for every building**
   (`Input Tables/Regular_Roof.csv`: steel deck / bulk insulation / ceiling
   cavity / plaster). Only solar absorptance, pitch and area vary per building.
   `roof_material` is not yet mapped to different layer stacks, and there is no
   measured per-building construction data.
8. Stage 3's demand fractions (0.70), the 18 °C cooling/heating hour split, the
   `T_sky = T_out − 10 K` long-wave term, the McAdams `h_ext` correlation, and
   reusing one COP for both cooling and heating are standard building-simulation
   defaults, **not validated** against Stuart's NatHERS runs. It also uses a
   single BARRA2 reference year (2007), suburb-uniform at ~11 km resolution.
   `net_electricity_saved_kwh_yr` (cooling saving minus heating penalty) is the
   headline number; per the seasonal analysis the two roughly cancel in Melbourne.
9. `--max-tiles` is not a reliable spatial smoke-test cap in the current Stage 1
   pipeline because later steps still use the full tile folder/query extent.
10. Some footprint sources map large compounds as one building polygon rather
    than individual roof blocks. Those roofs need a better authoritative source
    or an explicit computer-vision/manual correction workflow.

## Roadmap — What Needs To Change For The Final Model

Ranked by impact on the defensibility of the final FYP numbers.

### High Priority

1. **Validate the Stage 3 heat-ingress model.** The transient model
   (`stage3_thermal/heat_ingress_model.py`) is now per-building, but its inputs
   are unvalidated: the single `Regular_Roof.csv` layer stack, the 18 °C
   cooling/heating split, the 0.70 demand fractions, the `T_out − 10 K` sky
   temperature, the McAdams `h_ext`, and one COP for both cooling and heating.
   Validate against Stuart's NatHERS runs / AS-NZS 4859.1 and publish a
   sensitivity analysis (all constants live in `config/settings.py`).
2. **Per-material roof construction.** Every building currently uses one roof
   stack — map `roof_material` (metal deck / tile-on-batten / …) to distinct
   layer build-ups with sourced density/Cp/thickness.
3. **True suburb boundaries.** Replace rectangular bboxes with ABS SA2
   polygons, add an `inside_suburb` flag, report canonical in-boundary totals,
   and draw the boundary on annotations.
4. **Filter non-building footprints from Stage 1.** Gemini validation found
   24% of Clayton OSM footprints are not roofs (car parks, sheds, canopies).
   Add a minimum-area / classifier-confidence gate.

### Medium Priority

5. Expand to 3+ suburbs and use `tools.compare_suburbs` for the report's
   cross-suburb comparison.
6. Run BARRA2 for a full climate normal (1990–2020) instead of the single
   2007 sample.
7. Validate the absorptance lookup against local building stock data.
8. Give the model a floating indoor dead-band (currently a fixed 20 °C
   setpoint) and a direction-dependent cavity resistance (0.23 down / 0.16 up).

### Done

- **Stage 3 rebuilt as a per-building transient heat-ingress model** — Sep 2026.
  Replaces the inferred-R_roof algebraic calculator; marches the layered-roof
  model at current vs cool absorptance over a full year of hourly BARRA2
  weather and reports the cooling-season saving and the winter heating penalty
  separately. See `DECISION_LOG.md` 2026-09-10.
- Wind-dependent `h_out` in Stage 3 (BARRA2 `sfcWind`, McAdams correlation) —
  Aug 2026. Now folded into the transient model's hourly `h_ext`.
- BARRA2 OPeNDAP is live (no NCI auth needed) — Aug 2026.
- HSV classifier validated against Gemini (507 buildings, both suburbs) —
  Aug 2026. Agreement rates documented in Known Limitations.
- Pitch defaults recalibrated against Gemini validation (22.5° → 12°
  residential) — Aug 2026.
- Seasonal analysis tool built (`tools.seasonal_analysis`) — Aug 2026.
- Gemini validation database stored (507 buildings, Clayton + Carlton) —
  Aug 2026. Resume-safe, no repeat API cost.

## Data Sources

| Data | Source | Status |
| --- | --- | --- |
| Satellite imagery | Google Maps Static API | Active; key required — or download pre-fetched tiles via `tools.download_tiles` |
| Pre-fetched tiles | Team Google Drive ("Raising Rooves - Shared Data") | Clayton 670 MB, Carlton 386 MB zips |
| Building footprints | OpenStreetMap Overpass API | Active but can fail/reject large queries |
| Local footprint index | GeoPackage built by `tools.build_footprint_index` | Active when present |
| Footprint supplement | Overture Maps (public S3, `pip install overturemaps`) or VicMap BUILDING_POLYGON | Active — no key/auth needed; build with `tools.build_footprint_index` |
| Solar irradiance (primary) | BARRA2 via NCI THREDDS/OPeNDAP | **Active; no auth needed** (Aug 2026 discovery) |
| Solar irradiance (CSV) | Pre-extracted hourly BARRA2 CSV via `--barra-csv` | Active for offline runs |
| Solar irradiance (auto) | NASA POWER REST API | No key needed; auto-fetched; cached under `data/raw/nasa_power/` |
| Irradiance fallback | User CSV or Melbourne default GHI | Active |
| Roof attribute validation | Gemini 2.5 Flash (`tools.run_gemini_osm_experiment`) | 507 buildings validated; results stored in `data/output/experiments/` |
| Suburb boundaries | ABS SA2 or authoritative polygon data | Needed for final boundary handling |

DSM/LiDAR elevation sources (ELVIS 1 m, City of Melbourne Open Data, OpenTopography
COP30) were trialled for measured roof pitch and dropped — insufficiently precise
for defensible per-building plane fits. Pitch is assumed only (see `pitch_basis`
in Stage 1 Columns).

## Project Structure

```text
Raising Rooves Model/
  config/
    settings.py
    suburbs.py
  data/
    raw/
      tiles/
      barra/
      nasa_power/
      footprints/
    output/
    samples/            # tracked fixture for the no-key quickstart
  research/
    findings/
  shared/
    file_io.py
    geo_utils.py
    logging_config.py
    validation.py
  stage1_segmentation/
    pipeline.py
    run_stage1.py
    building_footprint_segmenter.py
    roof_classifier.py
    gemini_osm_experiment.py   # opt-in HSV-validation experiment
    stage1_visualiser.py
    tile_downloader.py
  stage2_irradiance/
    pipeline.py
    run_stage2.py
    barra_client.py
    cool_roof_calculator.py
    irradiance_loader.py
    irradiance_processor.py
    nasa_power_client.py
    temperature_processor.py
  stage3_thermal/
    pipeline.py
    run_stage3.py
    thermal_calculator.py
  tools/
    analyse_coordinate.py
    analyse_gemini_results.py
    build_footprint_index.py
    compare_suburbs.py
    download_tiles.py        # fetch team-shared satellite tiles from Google Drive
    run_gemini_osm_experiment.py
    seasonal_analysis.py     # monthly cool-roof benefit/penalty + R_roof sweep
    visualise_results.py
  tests/
  AGENTS.md
  CLAUDE.md
  CONTRIBUTING.md
  README.md
  pyproject.toml
  requirements.txt
```

## Adding A New Suburb

Add an entry to `config/suburbs.py`:

```python
"my_suburb": Suburb(
    name="My Suburb",
    sa2_code="",
    centroid=(-37.850, 145.010),
    bbox=(-37.860, 144.995, -37.840, 145.025),
    zone_type="residential",
)
```

For final modelling, also add or reference a true suburb/SA2 boundary polygon
rather than relying only on the bbox.

## Tests

```bash
python -m pytest tests/
```
