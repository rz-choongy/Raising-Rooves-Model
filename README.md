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
- Stage 3 thermal modelling: working (per-building R_roof heat-ingress model).
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
Stage 3: thermal model
  absorbed solar delta → heat conducted → cooling load → electricity saved
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
  `data/raw/footprints/buildings_index.gpkg` (~540 MB, built locally — see below)
- Source footprint file for rebuilding the index:
  `data/raw/footprints/melbourne_overture.geojsonl` (~1 GB). This is a
  line-delimited GeoJSON export of Overture Maps / Microsoft AU building
  footprints for the Melbourne area. It is too large for git — ask a teammate
  for a copy (or the built `.gpkg` directly), then build the index once with:
  `python -m tools.build_footprint_index`
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

Stage 3 reads Stage 2 output and applies a thermal physics chain to produce
per-building cooling electricity savings.

```bash
python -m stage3_thermal.run_stage3 --suburb "Carlton"
python -m stage3_thermal.run_stage3 --suburb "Carlton" --debug
```

Prerequisites: Stage 2 output must exist (`data/output/stage2_{suburb}.parquet`).

### Stage 3 Outputs

Stage 3 appends these columns to the Stage 2 table:

| Column | Description |
| --- | --- |
| `roof_r_value_m2k` | Roof thermal resistance R_roof inferred from building attributes (m²·K/W) |
| `h_out_w_m2k` | Outdoor surface coefficient — wind-derived from `mean_wind_speed_ms` when available, else the fixed 25 W/m²K fallback |
| `heat_transfer_fraction` | Effective roof→interior fraction, `U/(U+h_out)`, incl. multistorey attenuation |
| `heat_to_interior_kwh_yr` | Solar heat conducted through roof to building interior |
| `cooling_load_reduction_kwh_yr` | Reduction in cooling load (subset of heat to interior) |
| `electricity_saved_kwh_yr` | Actual cooling electricity saved (after HVAC COP) |
| `co2_electricity_saved_kg_yr` | CO2 avoided from the electricity saving |

Output files:

- `data/output/stage3_{suburb}.parquet`
- `data/output/stage3_{suburb}.csv`

### Stage 3 Thermal Parameters

The roof-to-interior heat fraction is now derived **per building** from an
inferred roof thermal resistance `R_roof`, following the roof-only heat-ingress
framing in Maggie's model:

```
U_roof   = 1 / R_roof
fraction = U_roof / (U_roof + h_out)
```

`h_out` (outdoor surface film coefficient) is wind-dependent when BARRA2 wind
data is available, via the McAdams (1954) simple forced-convection
correlation for an exterior building surface:

```
h_out = 5.7 + 3.8 * wind_speed_ms     # McAdams; falls back to 25 W/m²K when no wind data
```

More wind → higher `h_out` → the roof sheds absorbed heat back to the outside
air more efficiently → a *smaller* fraction conducts inward. The fixed 25
W/m²K fallback (ISO 6946's standard external surface coefficient) is used when
the irradiance source isn't BARRA2 (NASA POWER, user CSV, Melbourne default) —
this reproduces the pre-wind-model results exactly for those runs.

`R_roof` is inferred from Stage 1 attributes (no construction-age field exists):

| Building | Inferred R_roof | Resulting fraction |
| --- | --- | --- |
| Commercial / industrial / warehouse | R1.5 | ≈ 0.026 |
| Residential, tiled roof (default) | R2.5 | ≈ 0.016 |
| Residential, metal roof (older-stock proxy) | R1.5 | ≈ 0.026 |
| Unknown attributes | R2.5 | ≈ 0.016 |

Other parameters:

| Parameter | Value | Description |
| --- | --- | --- |
| Outdoor surface coefficient `h_out` | `5.7 + 3.8×wind_ms` (McAdams), fallback 25 W/m²K | Wind-dependent when BARRA2 wind data is available |
| Multistorey attenuation | ×0.5 for 4+ storeys | Extra thermal-mass/slab attenuation |
| Cooling fraction | 0.70 | Fraction of interior heat gain driving active cooling |
| HVAC COP | 3.0 (residential), 4.0 (commercial) | Split system / VRF baseline |

**Known limitation:** the McAdams correlation is a standard building-energy-
simulation default (also used by EnergyPlus's "SimpleCombined" exterior
convection model), not re-validated here for Australian roof geometries or
BARRA2's ~11 km wind resolution — same unvalidated-constant caveat as the rest
of Stage 3.

The R2.5 default reproduces the previous single heat-transfer constant, so
well-insulated stock is unchanged while poorly-insulated stock now correctly
shows a larger benefit. As a result, `electricity_saved_kwh_yr` is roughly
0.3–0.6% of `energy_saved_kwh_yr` from Stage 2 (the bulk of absorbed-solar
reduction never reaches the conditioned interior through an insulated roof).

**Known limitation:** `R_roof` is a documented proxy from `building_type` /
`roof_material`, not a measured value — Stage 1 provides no construction age.
Replacing it with ABS/VicMap construction-era data is a future improvement.

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
7. Stage 3 roof insulation `R_roof` is inferred per building from
   `building_type` / `roof_material` (no construction-age data exists), then
   drives the heat-transfer fraction via `U/(U+h_out)`. `h_out` is now
   wind-dependent (BARRA2 `sfcWind`, McAdams correlation) where BARRA2 is the
   irradiance source, else the fixed 25 W/m²K fallback. COP and cooling
   fraction remain Melbourne defaults. No measured per-building insulation is
   available — the R_roof mapping is a documented proxy.
8. **Stage 3 models cooling savings only — no heating penalty.** The seasonal
   analysis (`tools.seasonal_analysis`) shows the winter heating penalty is
   the same magnitude as the summer cooling benefit in Melbourne; net annual
   effect is near zero. The `HEATING_FRACTION` constant exists but is not yet
   wired into Stage 3. This must be fixed before final FYP reporting.
9. `--max-tiles` is not a reliable spatial smoke-test cap in the current Stage 1
   pipeline because later steps still use the full tile folder/query extent.
10. Some footprint sources map large compounds as one building polygon rather
    than individual roof blocks. Those roofs need a better authoritative source
    or an explicit computer-vision/manual correction workflow.

## Roadmap — What Needs To Change For The Final Model

Ranked by impact on the defensibility of the final FYP numbers.

### High Priority

1. **Add the heating penalty to Stage 3.** The seasonal analysis proved the
   winter penalty matches the summer benefit in magnitude — shipping
   cooling-only savings is wrong for Melbourne. Wire `HEATING_FRACTION` into
   `thermal_calculator.py` with a monthly/seasonal split driven by CDD/HDD.
2. **Validate Stage 3 constants.** Every headline electricity-saving number is
   scaled by unvalidated Melbourne defaults (`H_OUTSIDE`, `COOLING_FRACTION`,
   `HEATING_FRACTION`, COP, the R_roof proxy table). Validate against Stuart's
   NatHERS runs or AS/NZS 4859.1 simulation, and publish a sensitivity
   analysis over the plausible parameter ranges (all constants live in
   `config/settings.py`).
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
8. Replace the metal-roof-as-older-stock R_roof proxy with construction-era
   data (ABS/VicMap) if obtainable.

### Done

- Wind-dependent `h_out` in Stage 3 (BARRA2 `sfcWind`, McAdams correlation) —
  Aug 2026. Falls back to the fixed 25 W/m²K constant when BARRA2 wind data
  isn't the irradiance source.
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
| Footprint supplement | VicMap BUILDING_POLYGON or Overture/Microsoft-style data | Manual download/build |
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
