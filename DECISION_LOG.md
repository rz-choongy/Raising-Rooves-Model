# Decision Log — Raising Rooves Roof Attribution Pipeline

Each entry records a method or source choice, why it was made, and what was rejected.

---

## 2026-09-10 — Stage 3 engine: per-building transient heat-ingress model

**Decision:** Replace `stage3_thermal/thermal_calculator.py` (the algebraic
`fraction = U/(U+h_out)` calculator with an inferred `R_roof`) with a vectorised
port of the team's transient 1-D finite-volume roof model
(`stage3_thermal/heat_ingress_model.py`, from `heat_ingress_model.ipynb`). It
runs for **every building**: `MidTemps` is a `(layers, buildings)` array so a
whole suburb marches in lockstep — same forward-Euler method, `dt ≈ 40 s`
(clamped to the cavity-layer stability limit), same equations as the notebook.

Per-building cool-roof saving = march at the building's `absorptance_before`
minus march at `COOL_ROOF_ABSORPTANCE`, integrated hourly over a full BARRA2
year (2007) and split by outdoor temperature against the 18 °C base: hours
≥ 18 °C → cooling-season saving, hours < 18 °C → winter heating penalty
(reported separately). First 48 h discarded as spin-up. Per-building kWh =
per-m² result × `roof_surface_area_m2`.

**Why:**
- The inferred `R_roof` (`R_ROOF_BY_CATEGORY`, metal-residential nudge,
  `MULTISTOREY_ATTENUATION`) was always a stopgap "while we figured out this
  model" (Ryan). It guessed a number Stage 1 can't observe.
- The transient model captures roof thermal mass and diurnal lag, which the
  steady `U/(U+h)` fraction cannot, and it produces the heating penalty
  natively (roadmap item 1) instead of needing a bolt-on CDD/HDD split.
- The repo already had the BARRA2 OPeNDAP plumbing and, after
  `tools.fetch_heat_ingress_weather`, a way to get the hourly direct/diffuse/
  wind series the model needs.

**Choices locked with Ryan:**
- Vectorised NumPy port (not archetype bucketing) — true per-building output,
  ~5–10 min/suburb.
- One roof stack (`Input Tables/Regular_Roof.csv`) for every building; only
  absorptance, pitch and area vary. `roof_material`→stack mapping is future work.
- Full simulated year, cooling and heating reported separately.
- Cooling/heating hour split by outdoor temp vs `CDD_BASE_TEMP` (18 °C).
- Per-building kWh scaled by `roof_surface_area_m2` (matches the notebook's
  per-m²-of-sloped-roof solve).

**Rejected:**
- *Keep `thermal_calculator.py` as a `--fast` path* — Ryan chose a clean
  replacement; the inferred-R method is gone.
- *Archetype bucketing* — fast but quantises per-building results and degrades
  as more per-building inputs are added.
- *Summer-only / cooling-only scope* — the heating penalty is the whole point of
  roadmap item 1.

**Superseded:** "2026-07-03 — Stage 3 heat transfer: per-building R_roof instead
of one constant" and the wind-dependent-`h_out` follow-up from "2026-08-27" (the
McAdams `h_ext` is now the transient model's hourly outer film coefficient).

**Tradeoffs / follow-up:** the model's inputs are unvalidated — the single roof
stack, the 18 °C split, the 0.70 demand fractions, `T_sky = T_out − 10 K`, one
COP for cooling and heating. `azimuth_deg` is carried but numerically inert.
Validate against Stuart's NatHERS runs; add per-material stacks; sensitivity
analysis. Offline runs use a committed `data/samples/heat_ingress_carlton_2007.csv`.

**Code affected:** `stage3_thermal/{heat_ingress_model.py (new), pipeline.py,
run_stage3.py, __init__.py}`, `stage3_thermal/heat_ingress_model.ipynb` (moved
here), `config/settings.py`, `tests/test_heat_ingress_model.py` (replaces
`test_stage3_thermal.py`), `tools/fetch_heat_ingress_weather.py`, `README.md`.

---

## 2026-09-10 — Heat-ingress notebook weather: repo-generated BARRA2 extract

**Decision:** `heat_ingress_model.ipynb` (renamed from `Final_Code.ipynb`) now reads
its three input CSVs from a repo-tracked `Input Tables/` folder. The BARRA2 hourly
weather CSV is produced by `tools.fetch_heat_ingress_weather`, which pulls a single
grid point from the same public NCI THREDDS OPeNDAP endpoint Stage 2 uses
(`rsds`, `rsdsdir`, `tas`, `hurs`, `sfcWind`; diffuse = rsds − rsdsdir). Model
logic in the notebook is unchanged.

**Why:** The notebook previously loaded `../Input Tables/barra2_-37.91_145.13_2007.csv`
off a teammate's Google Drive, so it could not run from a clone. The repo already
had the BARRA2 OPeNDAP plumbing — only `rsdsdir` and `hurs` were missing from
`BARRA2_VARIABLES`.

**Fixed in passing (typos that blocked execution, physics intent unchanged, per
Ryan):** `math.pi()` → `math.pi`; `math.cos()` → `math.cos(2*math.pi*hour_local/24)`
in the Berdahl–Martin sky-temperature term, with `hour_local` = local clock hour
of the first modelled timestep.

**Rejected:**
- *Fetch via `stage2_irradiance.barra_client`* — it returns monthly summary stats,
  not the hourly direct/diffuse/RH columns the notebook needs.
- *Commit the CSV under `data/raw/barra/`* — that path is git-ignored, so a clone
  still wouldn't have the file.

**Verified:** notebook runs end-to-end (nbconvert) for the single dummy building;
plaster→indoor flux ≈ 12.9 W/m² at the first timestep, hourly heat ingress
1.5–7.2 Wh/m² over the first 10 h of 1 Jan 2007.

**Follow-up:** grid point resolves to lat −37.95 (nearest AUS-11 node), so values
differ slightly from the old hand-built CSV. Wind/geometry are still dummy inputs
in the notebook (`z_roof_m`, `roof_plan_area`, etc.) — wire to Stage 1/2 per-building
data when this model is promoted past sanity-checking.

---

## 2026-07-01 — Footprint source: OSM Overpass + VicMap/Microsoft supplement

**Decision:** Use OpenStreetMap Overpass API as the primary footprint source, merging
with a locally-indexed GeoPackage (VicMap or Microsoft AU footprints) when available.

**Why:** OSM requires no download, no API key, and covers all of Victoria via a single
HTTP request per suburb. The supplement adds ~2% more buildings in outer suburbs where
OSM coverage is thinner.

**Rejected:**
- *Microsoft Building Footprints alone* — 845 MB download with no roof tag metadata.
- *VicMap BUILDING_POLYGON alone* — authoritative but manual download per region, no
  roof material/shape tags, refreshed only every 2–3 years.
- *Overture Maps* — evaluated but schema not yet stable enough.

**Coverage achieved:** Carlton 6177 buildings, Clayton 8025 buildings. OSM coverage ~98%
of visible structures at zoom 19.

---

## 2026-07-01 — Material/colour/albedo: HSV pixel classifier

**Decision:** Use mean HSV values from satellite tile pixels within each building footprint
polygon; map to material class and solar absorptance via rule-based thresholds calibrated
to Melbourne Colorbond and terracotta distributions (CSR VIC priors).

**Why:** Fast, fully local, no per-query API cost, and sufficient for suburb-scale
aggregate analysis. The absorptance estimate has explicit uncertainty (±0.08–0.15)
that propagates into Stage 2/3 sensitivity analysis.

**Rejected:**
- *Gemini Vision API* — available (key in .env) but adds latency, per-query cost, and
  doesn't produce a numeric absorptance directly. Could improve categorical accuracy but
  not warranted without a labelled validation set for Melbourne roofs.
- *Fine-tuned image classifier* — would require 500–1000 labelled Melbourne roof images
  we don't have. Future upgrade path.

**Coverage:** 92.1% of buildings classified (7.9% outside downloaded tile bounds).
Unclassified buildings have null absorptance_estimate — flagged explicitly, not imputed.

---

## 2026-07-01 — Orientation: normal to longest footprint wall

**Decision:** Compute orientation_deg as the bearing (0–360° clockwise from North) of
the outward normal to the longest edge of the building footprint polygon.

**Why:** Requires no additional data. The longest wall approximates the building's
primary axis; its normal approximates the dominant roof slope direction. Sufficient for
suburb-scale aggregate statistics (N-S vs E-W bias analysis).

**Rejected:**
- *OSM roof:direction tag* — present on < 1% of Melbourne buildings.
- *LiDAR-derived aspect* — a `pitch_extractor.py` module once computed aspect_deg
  from DSM plane fits. Removed 2026-08-17 (see that entry) — this footprint-normal
  estimate is now the only orientation source.
- *OBB (oriented bounding box) major axis* — more robust for irregular polygons but
  adds dependency on scipy; the longest-edge method is adequate and simpler.

**Limitation:** For L-shaped or irregular buildings the longest edge may not represent
the main roof ridge. Flag: orientation_source = "footprint" (not yet in schema).

---

## 2026-07-01 — Pitch: assumed from building type / OSM roof:shape tag

**Superseded 2026-08-13** — the "typical" residential default below was recalibrated
from 22.5° to 12° against Gemini validation data. See that entry near the end of this
log; the mapping logic and rejected alternatives here are otherwise unchanged.

**Superseded 2026-08-17** — the "When LiDAR becomes available" plan below was acted
on, trialled, and reversed: DSM/LiDAR pitch extraction was removed. Assumed pitch is
now the permanent method, not an interim one. See that entry near the end of this log.

**Decision:** Use `_assumed_pitch_deg()` which maps OSM roof:shape and building_type
to typical Melbourne pitch values (0° flat, 5° industrial, 15° shallow, 22.5° typical
residential, 30° heritage/church). Flag all as pitch_source="assumed".

**Why:** No LiDAR/DSM file is currently available for Carlton or Clayton. The ELVIS
1m LiDAR download covers these suburbs but requires manual registration + download.
Stuart's guidance: demonstrate capability, not perfection — assumed typical values are
defensible for suburb-aggregate analysis.

**Pitch defaults used:**
- flat: 0° (commercial, office, retail, hospital, 4+ storey, roof:shape=flat)
- low (5°): industrial, warehouse, factory
- shallow (15°): garage, carport, shed, school
- typical (22.5°): residential "yes", gabled, hipped
- steep (30°): church, cathedral, mosque

**Rejected:**
- *COP30 coarse DSM (30 m)* — too coarse for individual building pitch; would produce
  terrain slope not roof pitch. Available via OpenTopography but not useful here.
- *Google Solar API pitch field* — not publicly documented/accessible without Business
  agreement.

**When LiDAR becomes available:** run `python -m tools.extract_pitch --suburb Carlton
--dsm-file <path>` to write pitch_deg and pitch_source="lidar" to a merged parquet.

---

## 2026-07-01 — Roof surface area: area_m2 / cos(pitch)

**Decision:** Compute `roof_surface_area_m2 = area_m2 / cos(radians(pitch_deg))` for
all non-flat roofs. For pitch < 0.5° treat as flat (surface area = footprint area).

**Why:** The irradiance model (Stage 2) needs the actual inclined area to compute absorbed
solar energy. A 22.5° pitch roof has 8.6% more surface area than its footprint. This is
a small but non-negligible correction for the aggregate across thousands of buildings.

**Note:** Because pitch is assumed for all buildings currently, roof_surface_area_m2
carries the same uncertainty as pitch_deg. The sensitivity is low: ±7.5° pitch error
at 22.5° nominal → ±4% area error.

---

## 2026-07-01 — Output formats: Parquet + CSV + GeoJSON + polygon sidecar

**Decision:** Stage 1 writes four outputs per suburb:
- `stage1_{suburb}.parquet` — typed, compressed, fast for downstream stages
- `stage1_{suburb}.csv` — human-readable, Excel-compatible
- `stage1_{suburb}.geojson` — full geometry + all attributes; loadable in QGIS/Mapbox
- `stage1_{suburb}_polygons.json` — ordered polygon list, used by
  `tools.visualise_results` for map overlays (originally also fed the DSM pitch
  extraction tool, removed 2026-08-17)

**Why:** GeoJSON enables spatial QA in any GIS tool without extra processing. Parquet is
the canonical format for Stage 2/3. CSV is for team members without Python.

---

## 2026-07-01 — Absorptance model: AS/NZS 4859.1 linear fit on HSV Value

**Decision:** `absorptance = 0.97 − 0.77 × V` for achromatic surfaces (S < 0.15);
hue-specific floor applied for chromatic surfaces (red/terracotta ≥ 0.65, blue ≥ 0.70).

**Why:** Grounded in AS/NZS 4859.1 tabulated absorptance values for common Australian
roofing products. White (V=1) → 0.20 matches a typical cool roof target. Dark iron (V=0.1)
→ 0.90 matches Colorbond Night Sky measurements.

**Uncertainty:** ±0.08 for near-white and near-black; ±0.12–0.15 for mid-tones.
These are ±1σ estimates, not worst-case bounds.

**Rejected:**
- *Lookup table on classifier material label* — requires a labelled dataset; intermediate
  label adds error. Direct HSV → absorptance is more transparent and calibratable.

---

## 2026-07-03 — Stage 3 heat transfer: per-building R_roof instead of one constant

**Decision:** Replace the single hardcoded `HEAT_TRANSFER_FRACTION` (0.016) with a
per-building fraction derived from an inferred roof thermal resistance `R_roof`,
following the roof-only heat-ingress framing in Maggie's model:
`U_roof = 1/R_roof`, `fraction = U_roof / (U_roof + h_out)`, `h_out = 25 W/m²K`.
`R_roof` is inferred from Stage 1 attributes: commercial/industrial → R1.5,
residential tiled → R2.5, residential metal → R1.5 (older-stock proxy), unknown → R2.5.
The `levels ≥ 4` case keeps a separate ×0.5 thermal-mass attenuation multiplier.

**Why:** Maggie's standalone notebook and our Stage 3 overlap less than they appear —
in her `Q_standard − Q_cool` the conductive `(Ta−Ti)/R_roof` term cancels, leaving the
absorbed-solar delta that Stage 2 already computes. The genuinely new contribution is
*explicit per-building insulation*. Folding `R_roof` into the existing annual pipeline
lets poorly-insulated stock correctly show more benefit while the R2.5 default reproduces
the previous constant, so well-insulated stock is unchanged.

**Tradeoffs:** `R_roof` is a proxy from building_type/roof_material, not measured — Stage 1
has no construction-age field. Metal-roof-as-older-stock is a weak signal. Two new audit
columns (`roof_r_value_m2k`, `heat_transfer_fraction`) are written so the assumption is
inspectable per building.

**Rejected:**
- *Full hourly roof-heat engine* (port Maggie's hourly notebook against BARRA2 hourly
  tas + irradiance) — physically richer but a much larger lift and recomputes Stage 2's
  absorbed-solar delta. Deferred; hourly `tas` plumbing already exists in
  `stage2_irradiance/temperature_processor.py` if revisited.
- *Two fixed scenarios (poorly vs well insulated)* — cleaner but produces a range rather
  than a per-building number; harder to aggregate for FYP reporting.

**Follow-up:** replace the R_roof proxy with ABS/VicMap construction-era data; validate the
fraction against Stuart's NatHERS runs.

**Code affected:** `config/settings.py`, `stage3_thermal/thermal_calculator.py`,
`stage3_thermal/pipeline.py`, `tests/test_stage3_thermal.py`, `README.md`.

---

## 2026-05-01 — Irradiance source: NASA POWER API over Melbourne constant

**Decision:** Stage 2 fetches real annual GHI from NASA POWER's free REST API (no key
required) instead of using a fixed Melbourne assumption, caching responses under
`data/raw/nasa_power/`.

**Why:** The assumed constant (1,850 kWh/m²/yr) overstated Carlton's measured GHI
(1,646 kWh/m²/yr) by 11% — a systematic bias into every downstream Stage 2/3 figure.
A free, no-key, per-location API removes that bias for negligible added complexity.

**Note:** This became the fallback tier, not the primary source, once BARRA2 OPeNDAP
went live (see 2026-08-09 entry below).

---

## 2026-05-01 — Google Sheets QA ticket system: built, later removed

**Decision (2026-05-01):** Built `tools/ticket_manager.py`, `tools/triage_agent.py`,
`tools/test_monitor.py` to auto-triage test failures into a Google Sheet as structured
tickets.

**Reversed (2026-08-09):** Removed. The ticket workflow added Google Sheets API
coupling and triage overhead that didn't pay for itself once the team moved to fixing
issues directly off `pytest` output during active development. No replacement tracker;
`git log` plus this log cover the "what changed and why" need instead.

---

## 2026-08-09 — Irradiance source priority: BARRA2 OPeNDAP as live default

**Decision:** Stage 2's irradiance source priority is now BARRA2 OPeNDAP (live,
streamed from NCI THREDDS) → BARRA2 hourly CSV (`--barra-csv`, for offline/cached runs)
→ user-supplied CSV → NASA POWER → Melbourne default constant. Every output row carries
an `irradiance_source` column recording which tier was used.

**Why:** BARRA2 is a reanalysis product built for the Australian region at higher
spatiotemporal resolution than NASA POWER, and — as of Aug 2026 — is reachable over
public OPeNDAP without NCI authentication, removing the earlier blocker to using it as
the primary source. NASA POWER and the Melbourne constant remain as fallbacks for
environments where OPeNDAP access is unavailable or slow.

**Rejected:**
- *NASA POWER as sole source* — coarser resolution, not Australia-specific; kept as
  fallback rather than replaced.
- *Requiring `--barra-csv` always* — manual CSV prep is a needless step now that the
  live OPeNDAP path works with no auth.

**Follow-up:** current BARRA2 runs use a single reference year (2007); roadmap item to
run a full 1990–2020 climate normal instead.

---

## 2026-08-09 — Gemini Vision as a validation harness, not a primary classifier

**Decision:** Use the Gemini Vision API to build a labelled validation database
(507 buildings: Clayton 302, Carlton 205, stored under `data/output/experiments/`,
resume-safe so repeat runs don't re-incur API cost) that the HSV pixel classifier and
Stage 1 pitch defaults are checked against — not to replace the HSV classifier itself.

**Why:** The 2026-07-01 material/colour decision rejected Gemini as the *primary*
classifier because it lacked a labelled Melbourne validation set to justify the added
cost/latency. Building that validation set was the missing piece, not a reason to
abandon HSV — Gemini is now used exactly where it was originally proposed as a future
upgrade path: to validate and calibrate the cheaper method, e.g. Gemini found 24% of
Clayton OSM footprints aren't roofs (car parks, sheds, canopies), and the pitch default
recalibration below was derived from this dataset.

**Rejected:** Switching Stage 1 wholesale to Gemini per-building classification —
still not warranted; per-query cost and latency don't scale to suburb-wide runs, and
the validation role captures most of the accuracy benefit at a fraction of the cost.

---

## 2026-08-09 — Seasonal analysis: cooling benefit vs heating penalty

**Decision:** Added `tools.seasonal_analysis` to compute monthly cooling benefit vs
heating penalty under R_roof sweeps, ahead of wiring a heating penalty into Stage 3
proper.

**Why:** Before spending implementation effort on `HEATING_FRACTION` in
`thermal_calculator.py` (roadmap item 1), we needed to know whether the heating penalty
would materially change the annual figure. Finding: in Melbourne's climate, the monthly
cooling benefit and heating penalty nearly cancel — informs how much to weight this
against the other unvalidated Stage 3 constants (roadmap item 2).

---

## 2026-08-13 — Pitch defaults recalibrated: residential 22.5° → 12°

**Decision:** Lowered the assumed residential pitch default from 22.5° to 12°, and
added per-suburb classifier quality multipliers (`SUBURB_CLASSIFIER_QUALITY` in
`config/settings.py`).

**Why:** The 2026-07-01 pitch assumption was a typical-Melbourne-residential estimate
with no local ground truth. The Gemini validation database (2026-08-09 entry) gave a
real, if still imperfect, check against Melbourne building stock and showed the flatter
default fits observed roofs better than the original assumption. Per-suburb quality
multipliers account for classifier accuracy varying with tile coverage and roof style
mix between Clayton and Carlton.

**Tradeoffs:** Still an assumed default, not measured pitch — the underlying limitation
from the 2026-07-01 entry (no LiDAR/DSM for these suburbs) is unchanged. The Gemini
validation set itself is 507 buildings across two suburbs, not a statistically
exhaustive ground truth.

**Code affected:** `config/settings.py`.

---

## 2026-08-17 — DSM/LiDAR pitch extraction removed; assumed pitch is permanent

**Decision:** Removed the DSM-based roof pitch extraction path entirely
(`stage1_segmentation/dsm_processor.py`, `stage1_segmentation/pitch_extractor.py`,
`tools/extract_pitch.py`, and the `rooves-extract-pitch` console entry point), along
with the `rasterio` dependency and the `OPENTOPO_API_KEY` setting. `_assumed_pitch_deg()`
in `stage1_segmentation/pipeline.py` is now the sole source of `pitch_deg` for every
building. Added a `pitch_basis` column to Stage 1 output (CSV/Parquet/GeoJSON), written
next to `pitch_deg`/`pitch_source`, recording exactly which rule produced the value:
`roof_shape:<tag>`, `levels>=4`, `building_type:<tag>`, or `residential_default`.

**Why:** The 2026-07-01 pitch entry left the door open for LiDAR to supersede the
assumption once available. ELVIS 1 m DSM data was obtained and run through the
RANSAC+SVD plane-fitting tool (`pitch_extractor.py`), but the team judged the resulting
per-building pitch measurements not precise enough to trust over the calibrated
assumption — consistent with the "orphaned output" note already in `README.md`'s Known
Limitations before this cleanup. Rather than keep dead DSM code and file-download docs
around, we removed it and made the assumption-based approach the permanent method.
`pitch_basis` replaces the DSM output as the auditability mechanism: instead of trusting
a plane fit, every assumed value can be traced back to the specific OSM tag or building
attribute that produced it.

**Rejected:**
- *Keep the DSM tool as an optional/unused path* — dead code with real maintenance cost
  (rasterio dependency, ELVIS/COP30 download docs, a whole test file) for a method the
  team won't use. Removing it is simpler than documenting "exists but don't use it."
- *Re-attempt DSM with a different resolution or provider* — not pursued; the team's
  priority list (see README Roadmap) has higher-value items (heating penalty, Stage 3
  constant validation, true suburb boundaries) than re-chasing elevation data.

**Tradeoffs:** Pitch remains unmeasured for every building. `roof_surface_area_m2`
(Stage 1) inherits the same uncertainty as before — this was already true in practice
since the DSM path was never wired into the main pipeline or Stage 2/3. The
`pitch_uncertain` Gemini QA action (renamed from `needs_dsm` in
`gemini_osm_experiment.py`) now correctly signals "low visual confidence" rather than
implying a DSM follow-up that no longer exists.

**Code affected:** `stage1_segmentation/pipeline.py`, `stage1_segmentation/gemini_osm_experiment.py`,
`config/settings.py`, `requirements.txt`, `pyproject.toml`, `.env.example`,
`tests/test_stage1_attribution.py`, `tests/test_gemini_osm_experiment.py`. Deleted:
`stage1_segmentation/dsm_processor.py`, `stage1_segmentation/pitch_extractor.py`,
`tools/extract_pitch.py`, `tests/test_extract_pitch_import.py`.

---

## 2026-08-27 — Stage 3 h_out: wind-dependent via BARRA2 sfcWind

**Decision:** Replaced the fixed outdoor surface coefficient `H_OUTSIDE_W_M2K = 25.0`
with a wind-dependent value where BARRA2 is the irradiance source:
`h_out = H_OUT_WIND_INTERCEPT_W_M2K + H_OUT_WIND_SLOPE_W_M2K_PER_MS * wind_speed_ms`
(McAdams 1954 simple forced-convection correlation for an exterior building surface;
5.7 + 3.8·V, the same form used by EnergyPlus's "SimpleCombined" exterior convection
algorithm). BARRA2's `sfcWind` (10 m wind speed) is now fetched alongside `rsds`/`tas`
in `fetch_all_climate_data()`, reduced to a suburb-level annual mean in
`wind_processor.py`, carried through Stage 2 as `mean_wind_speed_ms`, and consumed by
`thermal_calculator._h_out_from_wind()`. `H_OUTSIDE_W_M2K` remains as the fallback for
runs where BARRA2 isn't the irradiance source (NASA POWER / user CSV / Melbourne
default carry no wind data) — this reproduces pre-change results exactly for those
paths, and `calculate_thermal_benefit()` reproduces its pre-change output exactly when
`wind_speed_ms` is omitted (verified in `TestWindDependentHOut::
test_default_call_omits_wind_reproduces_legacy_behaviour`).

**Why:** Ryan asked whether the heat-ingress model accounts for wind — it didn't. Physically,
`h_out` is described in the code as "combined convective + radiative," and convective
transfer at a roof surface is genuinely wind-speed-dependent; the fixed 25 W/m²K value
is literally ISO 6946's standard external surface coefficient (Rse = 0.04 m²K/W), a
building-code default not derived from local Melbourne conditions. BARRA2 already
serves `sfcWind` in the same catalog structure as `rsds`/`tas` (confirmed against the
live THREDDS catalog), so wiring it in costs one more monthly OPeNDAP fetch per suburb
with no new data source.

**Direction check:** more wind → higher h_out → the roof sheds absorbed heat back to
outside air more efficiently → a *smaller* fraction of the Stage 2 absorbed-solar delta
conducts inward → *less* cooling benefit, all else equal. Confirmed end-to-end in
`TestWindMonotonicityEndToEnd`.

**Tradeoffs:** The McAdams correlation is a generic building-simulation default, not
re-validated for Australian roof geometries or BARRA2's ~11 km wind resolution — same
unvalidated-constant caveat that already applies to `H_OUTSIDE`, `COOLING_FRACTION`,
`HEATING_FRACTION`, and the R_roof proxy table (roadmap item 2). Wind speed, like GHI,
is applied suburb-uniformly (one BARRA2 grid cell per suburb) — no per-building wind
exposure (tree cover, terrain shielding, building height) is modelled.

**Rejected:**
- *uas/vas component vectors* — BARRA2 also serves the eastward/northward wind
  components separately; `sfcWind` is the already-resolved speed and is what the
  McAdams correlation needs, so no vector math was necessary.
- *McAdams' own high-wind power-law variant* (h = 6.47·V^0.78, for V > 5 m/s) — skipped
  for now since BARRA2 annual-mean wind speeds at Melbourne suburb centroids sit well
  under 5 m/s; worth revisiting if a windier/coastal suburb is added.

**Code/docs affected:** `config/settings.py`, `stage2_irradiance/wind_processor.py`
(new), `stage2_irradiance/barra_client.py`, `stage2_irradiance/pipeline.py`,
`stage3_thermal/thermal_calculator.py`, `stage3_thermal/pipeline.py`,
`tests/test_stage3_thermal.py`, `README.md`.

**Follow-up:** re-run Stage 2/3 for existing suburbs (Clayton, Carlton) to pick up
`mean_wind_speed_ms` and the wind-adjusted `h_out_w_m2k`/`electricity_saved_kwh_yr`,
and refresh the published comparison artifact if the numbers move materially.

---

## 2026-08-28 — Reporting basis: per-building and per-m², not suburb totals

**Decision:** Any report or comparison artifact we produce from the pipeline outputs
must lead with **per-building** and **per-m² of roof** figures. Suburb-wide totals
(GWh/yr, t CO2/yr, "equivalent households") may appear as supporting context but are
not the headline. When comparing suburbs, normalise to `kWh/m2/yr` and
`kWh/building/yr` so suburbs of different size and building count are actually
comparable.

**Why:** Ryan's call. Absolute suburb totals are dominated by how big the study
bbox happens to be and how many footprints OSM has — Tullamarine "wins" every total
purely because it has 10,632 roofs over 4.4 M m². That tells us nothing about
whether a cool roof is worth doing on a given building. The per-m² number
(`electricity_saved_kwh_yr / roof_surface_area_m2`) is the physically meaningful,
size-independent quantity and is what the FYP argument should rest on.

**How to apply:** stat tiles and comparison tables lead with per-m² / per-building;
totals are a secondary row. `tools/compare_suburbs.py` output and the
`suburb_comparison.csv` schema should carry `elec_per_m2_kwh_yr` and
`elec_per_building_kwh_yr` as primary columns.

**Code/docs affected:** future report tooling and artifacts only; no pipeline code
change. `CLAUDE.md` "README Update Rules" area notes the reporting basis.
