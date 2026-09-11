# Raising Rooves - Claude Project Guide

This file is the working memory and operating guide for Claude in the
Raising Rooves Model repository. Use it together with `README.md`, which is
the public project guide.

## Project Snapshot

Raising Rooves is a Monash University Final Year Project for modelling the
benefits of cool roof interventions across Melbourne suburbs.

- Team: Ryan, Seamus, Angus, Flynn, Maggie, Gabrielle
- Supervisor: Stuart
- Current phase: Stages 1-3 all run end-to-end. Focus has shifted from
  building the pipeline to validating it (Stage 3 constants, HSV classifier,
  suburb boundaries) for the final FYP report
- Persistence model: no database; store outputs as CSV, Parquet, JSON, and
  images under `data/`
- Main goal: produce defensible per-building estimates of cool roof benefit,
  then improve the model enough for final FYP reporting

## Start Every Session Here

1. Read `README.md` for the current user-facing state of the project.
2. Read this file for agent workflow, project plan, and memory rules.
3. Check the worktree before editing:
   ```bash
   git status --short
   ```
4. Inspect the relevant module before proposing or changing code.
5. Preserve user work. Do not revert unrelated files or delete generated data
   unless Ryan explicitly asks.
6. When the task affects project scope, outputs, CLI flags, or known
   limitations, update `README.md`.

## Current Project Plan

### Completed

- Stage 1 roof segmentation using OpenStreetMap building footprints via
  Overpass API.
- Optional VicMap building polygon merge.
- HSV roof material and colour classifier for missing OSM roof tags.
- Annotated Stage 1 visualisation PNG output.
- Stage 1 polygon sidecar JSON, used by `tools.visualise_results` for map
  overlays.

### Completed (recently)

- Stage 2 cool roof delta calculation with per-building irradiance join.
  Physics: `energy_saved = GHI * footprint_area * (absorptance_before - 0.20)`.
  Irradiance priority: BARRA2 OPeNDAP (live since Aug 2026 — no NCI auth
  needed) → BARRA2 hourly CSV (`--barra-csv`) → user CSV → NASA POWER →
  Melbourne default. Output carries an `irradiance_source` column.
- Stage 3 thermal modelling: per-building **transient 1-D finite-volume**
  heat-ingress model (`stage3_thermal/heat_ingress_model.py`, vectorised port
  of `heat_ingress_model.ipynb`). Marches a layered roof at current vs cool
  absorptance over a full BARRA2 year; reports cooling-season electricity
  saved AND the winter heating penalty separately. Replaced the inferred
  `R_roof` calculator 2026-09-10 (`thermal_calculator.py` deleted). Hourly
  weather via `tools.fetch_heat_ingress_weather` (cached under
  `data/raw/barra/`), offline fallback `data/samples/heat_ingress_carlton_2007.csv`.
  See `DECISION_LOG.md`.
- Gemini validation database: 507 buildings (Clayton 302, Carlton 205)
  stored at `data/output/experiments/`. Resume-safe, no repeat API cost.
- Seasonal analysis tool (`tools.seasonal_analysis`): monthly cooling
  benefit vs heating penalty with R_roof sweeps. Key finding: they nearly
  cancel in Melbourne.
- Pitch defaults recalibrated against Gemini validation (residential 22.5°
  → 12°). Per-suburb classifier quality multipliers in
  `SUBURB_CLASSIFIER_QUALITY`.
- DSM/LiDAR roof pitch extraction (`dsm_processor.py`, `pitch_extractor.py`,
  `tools.extract_pitch`) removed 2026-08-17 — elevation data wasn't precise
  enough for defensible per-building plane fits. Pitch is assumed-only for
  every building; Stage 1 output now carries a `pitch_basis` column recording
  which rule (`roof_shape:*`, `levels>=4`, `building_type:*`,
  `residential_default`) produced each `pitch_deg` value. See
  `DECISION_LOG.md`.
- Team-shared satellite tiles on Google Drive ("Raising Rooves - Shared
  Data"), read-only for teammate Monash accounts — teammates download zips
  manually from Drive (no Maps API key). `tools.download_tiles` only works
  when the files are link-shared, which the team declined.
- Tracked sample fixture `data/samples/stage1_carlton.parquet` so a fresh
  clone runs Stages 2-3 with no API keys.

### Next Priorities (ranked — keep in sync with README Roadmap)

1. Validate the Stage 3 transient model inputs against Stuart's NatHERS runs /
   AS-NZS 4859.1: the single `Regular_Roof.csv` layer stack, the 18°C
   cooling/heating hour split, `COOLING_FRACTION`/`HEATING_FRACTION` (0.70),
   `T_sky = T_out - 10 K`, one COP for cooling and heating. Publish a
   sensitivity analysis (constants in `config/settings.py`).
2. Map `roof_material` to distinct per-building roof layer stacks (metal deck /
   tile-on-batten / default) instead of one stack for every building.
3. Replace rectangular bboxes with true ABS SA2 suburb polygons and an
   `inside_suburb` flag; report in-boundary totals.
4. Filter non-building footprints from Stage 1 (Gemini found 24% of Clayton
   OSM footprints aren't roofs — car parks, sheds, canopies).
5. Expand to 3+ suburbs and use `tools.compare_suburbs` for FYP reporting.
6. Run BARRA2 for a full climate normal (1990–2020); current runs use 2007.
7. Move remaining in-module constants (absorptance tables in
   `cool_roof_calculator.py`) into `config/settings.py` for sensitivity
   sweeps.

## Architecture Rules

- Each pipeline stage must be independently runnable via:
  `python -m stageN_module.run_stageN`
- Do not introduce a database without an explicit project decision.
- Load all API keys from `.env` through `python-dotenv`.
- Never hardcode secrets, tokens, API keys, or private paths.
- Use the `logging` module, not `print`, for application logging.
- Logging should use `shared/logging_config.py` where possible.
- Type-hint every function signature.
- Keep module functions self-contained and testable in isolation.
- Prefer existing shared utilities over new helper code.
- Keep outputs in `data/output/` unless the user asks for a different path.

## Data Conventions

- Coordinates: EPSG:4326 / WGS84 latitude and longitude.
- Suburb identification: use ABS SA2 codes where possible.
- Tile naming: `{suburb}_{zoom}_{x}_{y}.png`.
- Area: square metres; prefer `m2` in agent docs and code comments unless
  user-facing docs already use another safe convention.
- Irradiance: W/m2 for instantaneous values; kWh/m2/day or kWh/m2/year for
  aggregated values.
- Temperature: degrees Celsius.

## File Conventions

- Python files: `snake_case.py`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`, usually in `config/settings.py`
- Tests: place focused tests under `tests/`
- Research notes: place sourced findings under `research/findings/`

## Codebase Check Workflow

When asked to understand or change the project:

1. Identify the pipeline stage involved.
2. Read the CLI entry point first.
3. Follow the orchestrator/pipeline file.
4. Read the domain modules used by that stage.
5. Check tests and existing sample outputs.
6. Confirm whether the change affects output columns, CLI flags, or README
   claims.
7. Run the narrowest useful verification command.

Useful inspection commands:

```bash
rg --files
rg -n "def |class |argparse|click|typer|annual_ghi|pitch_deg" .
python -m pytest tests/
```

## Run Commands

All `python -m ...` entry points are documented in `README.md` — run `--help`
on any entry point for the full flag list. The canonical invocation per stage:

```bash
python -m stage1_segmentation.run_stage1 --suburb "<name>"
python -m stage2_irradiance.run_stage2 --suburb "<name>"
python -m stage3_thermal.run_stage3 --suburb "<name>"
python -m tools.visualise_results --suburb <name>
python -m tools.compare_suburbs
python -m pytest tests/
```

## External APIs And Data Sources

Use this as the quick checklist when Ryan asks "what APIs/data do we use?"

| Purpose | Source | Access | Current use |
| --- | --- | --- | --- |
| Satellite imagery | Google Maps Static API | `GOOGLE_MAPS_API_KEY` in `.env` | Active tile download |
| Building footprints | OpenStreetMap Overpass API | No key | Active Stage 1 footprint source |
| Building footprints supplement | VicMap BUILDING_POLYGON | Manual SHP download from DataShare | Optional merge |
| Irradiance (active) | BARRA2 via NCI THREDDS/OPeNDAP | No key needed (public OPeNDAP) | Active Stage 2 source |
| Irradiance (fallback) | NASA POWER REST API | No key; cached under `data/raw/nasa_power/` | Fallback Stage 2 source |
| Hourly weather (Stage 3) | BARRA2 OPeNDAP via `tools.fetch_heat_ingress_weather` (`rsds`, `rsdsdir`, `tas`, `hurs`, `sfcWind`) | No key; needs `xarray`+`pydap`; cached under `data/raw/barra/` | Transient heat-ingress model forcing |
| Suburb boundaries | ABS SA2 shapefiles / manual bbox | Manual data prep | Needed for robust coverage |

DSM/LiDAR pitch sources (ELVIS 1 m, City of Melbourne Open Data, OpenTopography
COP30) are no longer used — trialled and dropped 2026-08-17 as insufficiently
precise. Pitch is assumed only; see `pitch_basis` in Stage 1 output.

Never paste API keys into notes, commits, chat, or screenshots.

## Stage Notes

Per-stage detail (output columns, physics limitations) is in
the lazy-loaded `stage-notes` skill — invoke it when working on a pipeline stage.
Quick reference:

- **Stage 1:** OSM + VicMap footprints, HSV pixel classifier. `energy_saved_kwh_yr`
  is absorbed solar reduction — NOT electricity savings. Stage 3 handles that.
- **Stage 3:** per-building transient finite-volume roof model
  (`stage3_thermal/heat_ingress_model.py`). Marches `Regular_Roof.csv` layers at
  current vs cool absorptance over a full hourly BARRA2 year; splits the delta
  by 18°C outdoor temp into cooling saving vs heating penalty. Constants in
  `config/settings.py` (`HEAT_INGRESS_*`, `COOLING_FRACTION`, COP) are
  unvalidated Melbourne defaults — the #1 roadmap item.

## README Update Rules

Update `README.md` when:

- A CLI flag is added, removed, or changed.
- Output files or output columns change.
- A pipeline stage is started, completed, or materially redesigned.
- A known limitation is resolved or a new important limitation is discovered.
- Setup steps, API keys, or data-source requirements change.

Do not update `README.md` for internal-only refactors or comment cleanup.

## Reporting Basis

When producing any report, comparison, or artifact from the pipeline outputs, lead
with **per-building** and **per-m² of roof** figures (e.g.
`electricity_saved_kwh_yr / roof_surface_area_m2`). Suburb-wide totals (GWh/yr,
t CO2/yr, equivalent households) are supporting context only, never the headline —
they mostly track study-bbox size and OSM footprint count, not whether a cool roof
is worthwhile. Cross-suburb comparisons must be normalised per m² / per building.
See DECISION_LOG.md 2026-08-28.

## Research Workflow

When Ryan asks to research a topic:

1. Use web search and collect 5-10 relevant sources.
2. Write a markdown summary to:
   `research/findings/{topic_slug}_{YYYY-MM-DD}.md`
3. Include source URLs, key findings, relevance to Raising Rooves, and
   recommended next steps.
4. Focus on datasets, pretrained models, API access, benchmarks, and practical
   integration cost.
5. Note uncertainty clearly when sources are weak, stale, or not Melbourne
   specific.

## ChoongyOS Vault / Personal Brain Workflow

Ryan wants FYP planning and decisions linked into his personal knowledge base,
referred to as "ChoongyOS Vault".

Preferred vault location, if available:

```text
C:\Users\choon\ChoongyOS Vault
```

If that path is not accessible, ask Ryan for the exact vault path before
writing outside this repository.

When asked to sync project knowledge into the vault:

1. Keep repo docs as the source of truth for runnable code instructions.
2. Keep the vault as the source of truth for study notes, planning decisions,
   meeting notes, research summaries, and FYP wiki pages.
3. Create or update a Raising Rooves area in the vault, preferably:
   ```text
   Projects/Raising Rooves/
   ```
4. Suggested vault notes (see step 3 for preferred directory):
   - `Overview.md`, `Current Plan.md`, `API and Data Sources.md`
   - `Decisions.md`, `Research Questions.md`, `Meeting Notes.md`
   - One per pipeline stage (e.g. `Stage 1 - Roof Segmentation.md`)
5. Add backlinks from project notes to relevant FYP or university notes if
   those notes already exist.
6. Do not move secrets, raw datasets, huge outputs, or generated tiles into the
   vault.
7. When a decision changes code behaviour, update both the repo docs and the
   relevant vault planning note.

Suggested decision entry format:

```markdown
## YYYY-MM-DD - Decision title

Decision:

Why:

Tradeoffs:

Code/docs affected:

Follow-up:
```

## Git Workflow

- Use `git add` and `git commit` only.
- Do not push unless Ryan explicitly asks.
- Commit after each meaningful unit of work.
- Commit messages should explain why the change exists, not only what changed.
- Before committing, check:
  ```bash
  git status --short
  ```

## Debugging Rules

- All CLI entry points should accept `--debug`.
- Debug mode should set logging to DEBUG.
- Logs should write to console and `logs/{module}_{date}.log`.
- MVP and pipeline tools should save useful outputs to `data/output/`.
- Prefer narrow reproducible commands in bug reports.

## Quality Bar

- Keep changes small and traceable.
- Prefer tests around physics, data joins, coordinate logic, and CLI argument
  behaviour.
- Avoid silent fallbacks for scientific calculations; log assumptions and mark
  output columns clearly.
- Treat unit confusion as a serious bug. Check W/m2 versus kWh/m2/year and
  footprint area versus roof surface area.
- Treat CRS confusion as a serious bug. Confirm EPSG:4326 inputs before spatial
  joins or distance calculations.
