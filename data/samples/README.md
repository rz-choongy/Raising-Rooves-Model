# Sample data

This folder is the only part of `data/` tracked in git. It exists so a fresh
clone can run Stages 2 and 3 immediately — no API keys, no tile downloads.

## Files

| File | What it is |
| --- | --- |
| `stage1_carlton.parquet` | Real Stage 1 output for Carlton: 6,177 buildings with footprint area, roof material/colour, absorptance estimates. |
| `heat_ingress_carlton_2007.csv` | Hourly BARRA2 weather at the Carlton centroid for 2007 (8,760 rows: direct/diffuse irradiance, temp, RH, wind). Stage 3's offline fallback when a live BARRA2 fetch isn't possible. Regenerate with `python -m tools.fetch_heat_ingress_weather --lat -37.7998 --lon 144.9667 --year 2007 --out data/samples/heat_ingress_carlton_2007.csv`. |

## How to use it

Follow the **Quickstart (no API keys needed)** section at the top of the
repo-root `README.md` — that is the canonical copy of the commands (copy this
parquet into `data/output/`, then run Stage 2, Stage 3, and the visualiser).

To regenerate this fixture from scratch you need a `GOOGLE_MAPS_API_KEY`:
`python -m stage1_segmentation.run_stage1 --suburb Carlton`, then copy the
resulting `data/output/stage1_carlton.parquet` here.

Do not add large files to this folder — keep samples under ~1 MB so the
repo stays fast to clone.
