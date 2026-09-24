# NatHERS climate-zone data: what it does and doesn't tell us about Stage 3's fractions

**Date:** 2026-09-19 (corrected same day — see "Correction" below)
**Original question:** Can real NatHERS data give us climate-zone-specific
cooling/heating fractions (or at least a sanity check on the current uniform
`COOLING_FRACTION = HEATING_FRACTION = 0.70` used in
`stage3_thermal/heat_ingress_model.py`) for the 14 suburbs modelled by this
project, without Stuart's own project-specific NatHERS runs (which don't exist
in this repo — confirmed by search)?

**Answer: no, not this data.** The initial version of this document claimed the
cooling/heating load-limit ratio below was "supporting evidence" that
`COOLING_FRACTION`/`HEATING_FRACTION` should vary by suburb. That claim doesn't
hold up and has been withdrawn — see **Correction** for why, before reading the
data as if it validates anything about those two constants. The suburb-zone
lookup and load-limit table themselves are still accurate and potentially
useful for other purposes (noted at the end), which is why this doc is kept
rather than deleted.

## Correction: why the load-limit ratio is not the same thing as the fractions

`COOLING_FRACTION`/`HEATING_FRACTION` are each a **single-season conversion
efficiency**: of the heat that physically conducts through *the roof* this
season, what fraction shows up as real HVAC electrical demand (the rest
absorbed by thermal mass, ventilation, occupancy behaviour). Each fraction
describes one season in isolation — it says nothing about how that season
compares to the other.

The cooling÷heating ratio computed below compares two *different* totals —
whole-building heating-limit MJ/m² vs whole-building cooling-limit MJ/m²
(walls, glazing, floor, infiltration, *and* roof combined) — for a compliance-
minimum reference house. That's a statement about **relative climate
severity** (does this climate need more total heating or cooling energy
overall), not about what fraction of any single pathway's raw heat flow
converts to demand.

Concretely: a suburb needing 10x more heating than cooling (per NatHERS) says
nothing about whether 40%, 70%, or 95% of *the roof's own* winter heat loss
actually reaches the heater — those are independent questions. A brutally
heating-dominated climate could have a low heating fraction (a lot of that
loss absorbed by thermal mass) or a high one; the load-limit ratio doesn't
constrain it either way. Scaling `HEATING_FRACTION` by this ratio, as an
earlier draft of this document's "recommended next steps" suggested, would
therefore test a made-up relationship, not validate anything — that
suggestion has been removed.

## Method

1. Looked up each suburb's postcode against NatHERS's official postcode → climate
   zone table (`NatHERSclimatezonesSept2025.pdf`, dated Sept 2025 — the WebFetch
   tool could not parse this table-heavy PDF; downloaded it directly and parsed
   with `pdfplumber` instead).
2. Looked up each zone's **heating and cooling load limit** (MJ/m².annum) — the
   maximum allowed predicted energy demand for a 7-star-compliant Class 1
   dwelling on a concrete slab — from the ABCB's *NatHERS Heating and Cooling
   Load Limits Standard 2022* (same download-and-parse approach).

## Suburb → NatHERS zone → load limits

| Suburb | State | NatHERS zone (primary) | Heating limit (MJ/m²·yr) | Cooling limit (MJ/m²·yr) | Cooling ÷ Heating |
|---|---|---|---|---|---|
| Carlton | VIC | 21 | 48 | 41 | 0.85 |
| Tullamarine | VIC | 60 | 95 | 27 | 0.28 |
| Clayton | VIC | 62 | 80 | 22 | 0.28 |
| Frankston | VIC | 62 | 80 | 22 | 0.28 |
| Epping | VIC | 60 | 95 | 27 | 0.28 |
| Mildura | VIC | 27 | 71 | 43 | 0.61 |
| Warrnambool | VIC | 63 | 116 | 11 | **0.09** |
| Parramatta | NSW | 56 | N/A | N/A | — |
| West End | QLD | 10 | 16 | 39 | **2.44** |
| Subiaco | WA | 13 | 53 | 34 | 0.64 |
| Norwood | SA | 16 | 54 | 37 | 0.69 |
| North Hobart | TAS | 26 | N/A | N/A | — |
| Parap | NT | 1 | N/A | N/A | — |
| Braddon | ACT | 24 | 129 | 34 | 0.26 |

("N/A" is the ABCB Standard's own label, not a lookup failure — see caveats below.)

## Key finding

The **cooling-to-heating ratio swings from 0.09 (Warrnambool) to 2.44 (West End)**
— a >25× range — across suburbs this project currently treats identically with
one national `COOLING_FRACTION = HEATING_FRACTION = 0.70`. Even within Melbourne
alone, NatHERS assigns Carlton, Tullamarine/Epping, and Clayton/Frankston to
**three different climate zones** (21, 60, 62) with materially different
heating/cooling balances (0.85 vs 0.28 vs 0.28) — so even the "one fraction for
all Victorian suburbs" simplification undersells the variation NatHERS itself
already encodes at the postcode level.

This doesn't prove `COOLING_FRACTION`/`HEATING_FRACTION` are numerically wrong
(see caveats — it's not the same quantity), but it's a real, sourced argument
that a single uniform 0.70/0.70 pair across 8 states/territories and climates
from tropical Darwin to alpine-adjacent Canberra is very unlikely to be right,
and that whichever direction it's wrong in will differ by suburb, not apply
uniformly.

## Critical caveats — why this is not a drop-in replacement

1. **Different quantity.** NatHERS's published load limits are *whole-building*
   compliance thresholds (walls, glazing, floor, orientation, infiltration, and
   roof combined) for a *specific* reference-quality 7-star dwelling. Stage 3's
   `COOLING_FRACTION`/`HEATING_FRACTION` are specifically "what fraction of the
   *roof's own* conductive heat gain/loss becomes HVAC demand" — a narrower,
   roof-isolated quantity NatHERS doesn't publish directly. The ratio above is a
   reasonable proxy for *relative* heating-vs-cooling climate character, not a
   direct substitute value.
2. **NSW, NT, and Tasmania are excluded from this ABCB Standard entirely** — the
   Standard states outright that these load limits don't apply there (NSW uses
   its own BASIX compliance pathway instead; NT and Tas have no NatHERS load-limit
   requirement under the NCC pathway at all). That's Parramatta, Parap, and North
   Hobart — 3 of our 14 suburbs — with no data from this specific source,
   regardless of how the lookup is done.
3. **Reference-house dependent.** These are compliance-*minimum* loads for a
   standardised reference dwelling, not measured loads for the actual buildings
   in Stage 1/2's dataset (which span many real construction ages and types).
4. **Only the 7-star table is populated nationally** — the Standard's 6.5-star
   and 6-star tables are almost entirely "N/A" outside Queensland, so there's no
   way to cross-check against an older/lower star-band reference from this same
   source.

## Recommended next steps

- **Don't directly overwrite `COOLING_FRACTION`/`HEATING_FRACTION`** with values
  derived from this table — the unit mismatch (whole-building vs roof-only) makes
  that a false precision.
- **Do treat this as supporting evidence** that these two constants should vary
  by suburb/climate zone rather than being one national pair — worth citing
  alongside roadmap item 1 in any FYP write-up about model limitations.
- **The only way to get a fraction in the *right* units** (matching Stage 3's own
  roof-only definition) is either (a) Stuart's actual project NatHERS run for a
  reference house — still doesn't exist in this repo, would need to be
  requested — or (b) running a NatHERS-accredited simulation (e.g. via a free
  tool) for one representative house per relevant zone, then comparing its
  internal roof-only heating/cooling split against Stage 3's own raw thermal
  output for the same house/weather. Neither was attempted here — this findings
  doc only covers what's derivable from already-published NatHERS reference data
  without running new simulations.
- **Cheap sensitivity check that doesn't require new data**: re-run the existing
  Stage 3 comparison artifacts with `HEATING_FRACTION` scaled by each suburb's
  NatHERS zone ratio (e.g. Warrnambool's heating fraction pushed up, West End's
  pushed down) to see how much the "coating is a net loss" / "insulation always
  wins" findings move. If they're robust to this range, that's reassuring even
  without a fully validated fraction; if they flip, that's important to know
  before the FYP report leans on them.

## Sources

- [NatHERS Climate Zone Postcodes](https://www.nathers.gov.au/climate-zone-postcodes) — postcode → climate zone lookup page (links to the Sept 2025 PDF actually used)
- [NatHERS Heating and Cooling Load Limits Standard 2022 (PDF)](https://www.abcb.gov.au/sites/default/files/resources/2022/nathers-heating-cooling-load-limits-2022.pdf) — ABCB Standard, Tables 1–6
- [NatHERS Heating and Cooling Load Limits | ABCB](https://www.abcb.gov.au/resource/standard/nathers-heating-and-cooling-load-limits) — overview page
- [How NatHERS star ratings are calculated](https://www.nathers.gov.au/owners-and-builders/how-nathers-star-ratings-are-calculated)
- [NatHERS Climate Zones and Weather Files](https://www.nathers.gov.au/nathers-accredited-software/nathers-climate-zones-and-weather-files)
- [Australian Housing Data — Climate Zones dashboard, CSIRO](https://ahd.csiro.au/dashboards/energy-rating/climate-zones/) — general zone context, not suburb-specific lookup
