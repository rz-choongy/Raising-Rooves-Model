# Multi-year BARRA2 NPV — Implementation & Feasibility Plan

**Date:** 2026-10-10
**Author:** Claude (planning only — no Stage 3 run, no weather fetch, no analysis executed)
**Proposal (Ryan):** "Do a full BARRA analysis year-by-year from 2007 up to 2027, then
consider extrapolating past 2027 to 2037, so the NPV is based on real multi-year BARRA
weather instead of a single year."
**Status:** PLAN ONLY. Nothing below has been run.

---

## 1. Executive summary / recommendation

**Ship Option A now; do a lightweight Option B on two contrasting suburbs if (and only if)
there is slack before Monday; explicitly do NOT attempt Option C (future-hourly
projections) for this report.** That is consistent with the team's own meeting decision to
delete the "future climate" column because hourly projection data does not exist.

The single most important correction: **BARRA2 is a reanalysis of the *past*. "2027" and
anything beyond it is not "real BARRA data" — it is a projection.** The genuinely available
"real BARRA weather" window today (2026-10-10) is roughly **2007–~2024** (observed,
complete calendar years). So the honest re-framing of the proposal is:

> observed window **2007 → ~2024** (real reanalysis) + optional **projection 2025 → 2037**
> (NOT BARRA, must come from a climate-projection product or a statistical trend).

The deeper verdict: multi-year weather **improves defensibility** (it directly answers the
"you only used one year" critique) but is **very unlikely to change the economic
conclusion**. NPV here is cost-dominated and essentially always negative for the coatings;
the net energy saving in temperate Melbourne is a small volatile residual (Carlton 2007
0.49 vs 2019 0.35 kWh/m²/yr net). Averaging many years narrows the band around a number
that is already too small to pay back a \$35/m² coating reapplied every 5–8 years. It
mainly buys **rigor, not a different answer**. Coolmax (premium-only \$11/m², 36-yr life)
remains the one near-viable lever regardless of which year(s) are used.

---

## 2. BARRA2 coverage reality (the central correction)

`tools/fetch_heat_ingress_weather.py` builds its download URLs in `_build_url()`
(lines 69–79) from this path template:

```
output/reanalysis/{BARRA2_DOMAIN}/BOM/ERA5/historical/hres/BARRA-R2/v1/1hr/{var}/latest/
{var}_{DOMAIN}_ERA5_historical_hres_BOM_BARRA-R2_v1_1hr_{YYYYMM}-{YYYYMM}.nc
```

Note the literal `reanalysis` and `ERA5/historical` in the path. This is BARRA-R2, the
BOM regional **reanalysis** that dynamically downscales ERA5. Two consequences:

- It only exists for the **past**. There is no `2027` file, and there will not be one
  until 2027 has happened and BOM has processed and published it.
- Published coverage runs 1979 → *near present* but with a **processing/publication lag**
  (commonly a year or more). Realistically, **complete, published calendar years today run
  to about 2023–2024.** 2025 is likely partial or not yet posted; 2026 is incomplete by
  definition.

`stage3_thermal/run_stage3.py` exposes `--year` (default `HEAT_INGRESS_REFERENCE_YEAR =
2007`, `config/settings.py:221`), which flows through `pipeline.run_stage3(year=...)` into
`_resolve_weather()` and then `build_hourly_weather_table(lat, lon, year)`. So the pipeline
can already run any single observed year by changing one integer — **the per-year machinery
exists; only the aggregation-across-years and NPV-vector machinery does not** (see §6).

**Re-framed proposal:**

| Window | Years | What it actually is | Source |
| --- | --- | --- | --- |
| Observed | 2007 → ~2024 (≈18 yrs) | Real BARRA2 reanalysis | BARRA-R2 OPeNDAP (current code) |
| Projection | 2025 → 2037 | NOT BARRA; a model projection or trend | CMIP6 / CCIA / CORDEX, or a stat fit |

---

## 3. Tension with the team's 2-year decision

Today's FYP meeting chose a deliberate 2-year bracket:

- **2007 = ~20-year-median baseline** (a "typical" year).
- **2019 = hotter-than-average upper limit, ≈ predicted 2040 average climate.**
- **Deleted the "future climate" column** because hourly data for projections is not
  available.

The multi-year idea partly re-opens that decision, so be explicit about where it adds value
vs. duplicates what the team already agreed:

- **Adds real value:** replacing a single typical year with the *distribution* of ~18 real
  years gives an interannual **variability band** (mean ± spread) rather than a point. That
  is a stronger, more defensible statement than "we picked 2007", and it is the honest way
  to answer "why that year?".
- **Duplicates / conflicts:** 2019-as-"2040-proxy" is already the team's chosen way to
  represent a warmer future *without* needing future hourly data. The proposal's
  "extrapolate to 2037" step runs straight back into the **exact blocker the team already
  hit** — there is no hourly future weather — so it should NOT be reopened for this report.
- **Net:** the multi-year *observed* average (Option B) is a clean upgrade to the baseline
  year; the *future extrapolation* (Option C) is future work, not a Monday deliverable.

---

## 4. Scope options

### Option A — keep the agreed 2-year bracket (2007 + 2019)

- **Work:** none beyond what is already run (both `stage3_carlton_2007.parquet` and
  `stage3_carlton_2019.parquet` already exist on disk, as do 2019 variants for other
  suburbs). Report both as "typical / warm" sensitivity.
- **Runtime / storage / \$:** ~0. Uses existing outputs.
- **Defensibility:** moderate. Still exposed to "two cherry-picked years".

### Option B — full observed year-by-year Stage 3 (2007 → ~2024), per selected suburb

- **What:** run Stage 3 once per observed year, collect the **annual** per-building saving
  (and annual \$) into a per-year series `S_2007 … S_2024`, then drive NPV from that real
  vector with a **mean and interannual variability band**. No future projection.
- **Work:** a per-year driver loop + an aggregation sink + a modest change to
  `economic_analysis.py` to accept a multi-year vector (§6). Est. **0.5–1.5 day** of coding
  + review, plus compute wall-time (§5).
- **Defensibility:** high — directly answers the single-year critique.
- **Caveat:** only as good as the weather fetch; each year = 60 OPeNDAP requests
  (12 months × 5 variables), network-dependent and the usual bottleneck.

### Option C — Option B + extrapolation to 2037

Two sub-paths, both weaker than B:

- **C(i) statistical trend fit** on the observed `S_year` series (e.g. linear/Theil–Sen
  warming trend) extended to 2037. Cheap, but it extrapolates a *net residual* that is
  itself the small difference of two large seasonal terms — fragile, and it assumes the
  heating/cooling split scales linearly, which it does not.
- **C(ii) climate-projection downscaling** (CMIP6 / "Climate Change in Australia" / CORDEX).
  This is the methodologically correct future path, but these products **do not ship the
  hourly `rsds/rsdsdir/tas/hurs/sfcWind` fields** the transient model needs at a single
  grid point in BARRA2's format — which is precisely **the blocker the team already hit and
  resolved by deleting the future column**. Bridging it (bias-correcting/morphing an
  observed BARRA year toward a projected monthly climatology) is a research project, not a
  pre-Monday task.
- **Recommendation:** document C as **future work only**; do not attempt it now.

---

## 5. Storage / compute strategy

### The scaling problem

The `--hourly` export is the input `economic_analysis.py` consumes, and it is enormous.
Measured on disk today:

| Suburb (buildings) | Hourly parquet (1 year) |
| --- | --- |
| Carlton (6,177) | **1.53 GB** (`stage3_carlton_2019_hourly.parquet`) |
| West End (3,604) | 0.95 GB |
| Parramatta (3,365) | 0.78 GB |
| Shelly Beach (384) | 0.10 GB |

That is ~**0.25 MB per building per year**. Retaining hourly does NOT scale:

| Scenario | Hourly retained |
| --- | --- |
| Carlton alone × 18 yrs | ≈ **28 GB** |
| 2 contrasting suburbs (~6k + ~3k bldg) × 18 yrs | ≈ **40 GB** |

Wall-clock (rough, unmeasured — flag as estimate): each suburb-year is a BARRA2 fetch
(60 remote requests, minutes, the usual bottleneck) + the vectorised FV march + writing a
~1 GB parquet. Order **~0.5–1.5 h per suburb-year**; 2 suburbs × 18 yrs ≈ **~1–2 days
serial**, less if years run in parallel subagents/processes.

### Aggregation-only path (required)

Do **not** keep the hourly files. `economic_analysis.aggregate()` (lines 149–202) already
collapses each hourly file via DuckDB to **168 rows per building** (`building × dow × hour`,
`GROUP BY 1,2,3`) and caches that to a small parquet. Because the TOU tariff and occupancy
depend on time only through day-of-week and hour-of-day (`_hour_rates()`,
`ECONOMIC_OCCUPANCY_PROFILE`), this 168-row aggregate is **lossless for pricing**. So the
per-year pipeline should be:

```
for year in 2007..2024:
    run Stage 3 --hourly  ->  stage3_{suburb}_{year}_hourly.parquet   (~1 GB, TEMP)
    economic_analysis.aggregate()  ->  168-row/building cache         (~tens of MB)
    price()  ->  annual kWh + year-1 $ per building for THIS year     (~few MB)
    DELETE the hourly parquet
```

Retained artefacts across 18 years are the 168-row aggregates (or just the priced annual
vectors) — **~tens of MB per suburb total**, vs ~28 GB. Minimal code: a wrapper that calls
Stage 3, then `aggregate()`/`price()`, then unlinks the hourly file; optionally a
`--stream`/`--keep-hourly=false` flag so `economic_analysis.py` can aggregate-and-discard in
one pass. No change to the physics or the pricing is needed.

---

## 6. How the NPV method actually changes

### How it works today (single year repeated)

`economic_analysis.py` is explicit (module docstring line 22; `price()` line 282;
`config/settings.py:342–343`): **"The one indicative year is then repeated for every year
of the analysis period."** Mechanically:

- `price()` produces ONE year-1 dollar saving per building — `coating_cool_aud`,
  `coating_heat_elec_aud`/`_gas_aud`, etc. (lines 258–266), grossed to 8,760 h by
  `scale = HOURS_PER_YEAR / n_hours` (line 282).
- `_vectors()` (lines 302–342) builds length-`(years+1)` multiplier arrays: a degradation
  `activity` array and price-escalation arrays `esc_e = _escalator(REAL_GROWTH_ELEC)**t`,
  `esc_g` (lines 329–331). These encode *price* drift and coating decay — **not weather**.
- `evaluate()` (lines 368–426) takes the scalar year-1 saving `se`/`sg` (shape
  `(n_buildings, 1)`, lines 390–391), broadcasts it across all years, forms
  `benefit = se*ie + sg*ig` and `flows = benefit + c0*cost`, and discounts:
  `cols["npv_aud"] = flows @ disc` (line 401).

So weather enters as a **single scalar per building**, multiplied by a smooth price/decay
curve. There is **no per-year weather variation** anywhere.

### The minimal change for a real multi-year vector

Replace the scalar year-1 saving with a **per-year saving vector** `S_2007 … S_2024`
(optionally trend-extended to 2037 for C), aligned to analysis years 1..N, and keep the
price-escalation/degradation indices as the *multipliers on top*:

```
# today:  se shape (n_buildings, 1)  broadcast over years
# new:    se shape (n_buildings, years+1) from the real per-year weather series
benefit = se_year * ie + sg_year * ig          # ie/ig still carry price drift + decay
flows   = benefit + c0 * cost
npv     = flows @ disc                          # unchanged
```

Concretely:

1. Build a `(n_buildings, 18)` matrix of per-year annual \$ savings (cooling and heating
   split kept separate, since they escalate differently at lines 329–330). Years beyond the
   observed window either repeat the mean (B) or follow a trend (C).
2. Feed it into `evaluate()` in place of the single `s_elec`/`s_gas` broadcast. The
   escalation index `esc_*` continues to multiply each year's *real* weather saving by that
   year's price level — the inflation still cancels against the discount rate exactly as the
   current comment at lines 327–328 describes.
3. `_payback()`, break-even, and first-application logic need no structural change — they
   already operate on the per-year `flows` array.

**Does the code support multi-year input today? No.** `price()` returns one saving per
building and `evaluate()` broadcasts it. The change is contained — essentially swapping one
broadcast for a per-year lookup plus a loader that stacks the 18 annual `price()` outputs —
but it is a real (small) code change, not a config toggle. Budget it inside Option B's
0.5–1.5 day.

---

## 7. Is it worth it? (honest verdict)

- **Rigor: yes.** A mean-of-18-real-years saving with a variability band is a materially
  stronger claim than "we used 2007", and it pre-empts the obvious examiner question. This
  is the real payoff.
- **Decision: almost certainly no change.** NPV here is **cost-dominated**: \$35/m² coating
  reapplied every 5–8 yrs (`ECONOMIC_PRODUCTS`, `settings.py:453–475`) against a net saving
  that in temperate Melbourne is a small residual of a large summer gain minus a nearly-as-
  large winter penalty. Per MEMORY, the PR#18 run already found cool roofs ~0% positive NPV.
  Averaging real years narrows the band around a number that stays well short of payback; it
  will not flip the sign. Coolmax stays the only near-viable lever.
- **Who benefits most:** **temperate suburbs** (Carlton/Melbourne), where the net saving is
  the volatile small residual — that is exactly where multi-year averaging stabilises the
  headline. Hot/arid suburbs have almost no heating penalty, so their saving is larger and
  already stable year-to-year; one or two years is enough there.
- **Bottom line:** do it for *defensibility of the energy (kWh) section*, not in the hope of
  a positive NPV. Frame it in the report as "interannual robustness", not "this makes cool
  roofs pay".

---

## 8. Prerequisites & risks

- **NT / WA (and TAS) tariffs are a hard gap, not done.** `ECONOMIC_ELECTRICITY_ZONES`
  (`settings.py:365–395`) only covers **VIC / NSW / QLD / SA**; `ECONOMIC_GAS_RATE_C_PER_MJ`
  (lines 406–408) only **QLD/VIC/NSW/SA**. There is no DNSP entry for NT (no NEM
  participation), WA (Western Power / Horizon, SWIS), or TAS (TasNetworks). Any \$ figure for
  those states would silently fall back to `ECONOMIC_DEFAULT_ZONE = "CitiPower"`
  (Melbourne) with only a log warning — i.e. **wrong pricing**. Tariff data is publicly
  available online and must be added before pricing those states. Multi-year weather does
  not touch this; it is orthogonal and prerequisite.
- **Melbourne-timezone limitation.** `COL_TIME = "time_melbourne"` and the TOU windows are
  LOCAL-clock (`settings.py:357–363`, `490–492`). Non-VIC/NSW suburbs (e.g. Queensland) have
  their TOU \$ mis-timed until a per-suburb timezone fix lands (DECISION_LOG 2026-10-09).
  This affects **\$ timing, not kWh totals**, so it does not corrupt the energy section but
  does bias the economic section for those suburbs.
- **Tariff-escalation assumptions dominate NPV more than weather does.**
  `ECONOMIC_REAL_GROWTH_ELECTRICITY/GAS/INSTALL_COST` are all **0.0** today
  (`settings.py:424–426`). A plausible +0.5–1.5%/yr real electricity or install-cost drift
  moves the 30-yr NPV far more than swapping one weather year for 18. If the goal is a
  robust NPV, a sensitivity on these three growth rates is higher-leverage than multi-year
  weather. Say so in the report.
- **Compute vs deadline.** Option B is ~1–2 days of serial wall-time for 2 suburbs × 18
  years, fetch-bound and network-fragile (NCI THREDDS drops requests under load — the fetch
  already retries, `fetch_heat_ingress_weather.py:91–105`). This competes directly with a
  Monday-morning draft. If attempted, run it in the background on the two chosen suburbs
  early and treat it as best-effort; keep Option A as the guaranteed deliverable.

---

## 9. Recommended path given the FYP timeline

1. **Now / guaranteed:** ship **Option A** — report 2007 (typical) and 2019 (warm /
   2040-proxy) as the energy and \$ sensitivity bracket, using existing outputs. Lead with
   per-building and per-m² figures (energy first, then \$), per the reporting basis.
2. **If slack before Monday:** run a **lightweight Option B on two contrasting suburbs**
   (one temperate e.g. Carlton, one hot/arid), observed years only, **aggregation-only**
   (delete hourly as you go, §5), and report a **mean ± interannual band** for the energy
   section. Minimal `economic_analysis.py` change per §6. Present it as robustness, not as a
   path to positive NPV.
3. **Explicitly NOT now:** Option C / any future-hourly projection to 2037 — consistent with
   the team's decision to delete the future-climate column because hourly projection data
   does not exist. Document C (trend fit and CMIP6/CCIA/CORDEX morphing) as **future work**.
4. **Parallel, independent of weather:** before quoting \$ for any NT/WA/TAS suburb, add
   their tariffs/gas rates to `settings.py`; and add a tariff-escalation sensitivity
   (electricity/install-cost real growth), which drives NPV more than weather does.

---

## 10. Files / symbols read for this plan

- `tools/fetch_heat_ingress_weather.py` — `_build_url()` (69–79, confirms `reanalysis` /
  `ERA5/historical` path → past-only), `build_hourly_weather_table(lat, lon, year)`
  (128–189), CLI `--lat/--lon/--year/--out` (192–229).
- `stage3_thermal/run_stage3.py` — `--year` default `HEAT_INGRESS_REFERENCE_YEAR` (54–59),
  `--hourly` (110–119), output summary columns (161–169).
- `stage3_thermal/pipeline.py` — output column docstring (14–47), `_resolve_weather()`
  year→cache path (102–145), hourly sink / `stage3_{suburb}_hourly.parquet` (281–284).
- `tools/economic_analysis.py` — single-year-repeated design (docstring 15–29),
  `aggregate()` 168-row DuckDB collapse (149–202), `price()` year-1 \$ + 8,760-h gross-up
  (241–291, esp. 282), `_vectors()` escalation/degradation arrays (302–342), `evaluate()`
  scalar-saving broadcast + `flows @ disc` NPV (368–426), `_payback()` (345–365).
- `config/settings.py` — `HEAT_INGRESS_REFERENCE_YEAR=2007` (221), `COOLING_FRACTION=0.70`
  (311), `HVAC_COP_RESIDENTIAL=3.0` (321), `ECONOMIC_ELECTRICITY_ZONES` VIC/NSW/QLD/SA only
  (365–395), `ECONOMIC_GAS_RATE_C_PER_MJ` QLD/VIC/NSW/SA only (406–408),
  `ECONOMIC_INFLATION=0.025` (421), `ECONOMIC_REAL_GROWTH_* = 0.0` (424–426),
  `ECONOMIC_DISCOUNT_RATES_REAL=[0.03,0.07,0.10]` (431), `ECONOMIC_ANALYSIS_PERIOD_YEARS=30`
  (435), timezone note (490–492).
- On-disk evidence: `data/output/stage3_carlton_2019_hourly.parquet` = 1.53 GB; per-suburb
  hourly sizes and building counts (Carlton 6,177; West End 3,604; Parramatta 3,365; Shelly
  Beach 384) used for the §5 storage/compute estimates.
