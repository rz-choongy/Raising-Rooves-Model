"""
Streamlit dashboard for the Raising Rooves pipeline.

A single-page control panel over the existing CLI pipeline: pick a suburb,
see what input data (tiles/footprints/weather) and outputs already exist for
it, run any stage as a subprocess (same as the `python -m ...` commands in
README.md), and browse the resulting map/report/charts without leaving the
browser.

This does not replace the CLI entry points — it drives them. Every button
here runs the exact same `python -m stageN...` command documented in
README.md.

Usage:
    pip install streamlit
    streamlit run tools/dashboard.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from config.settings import BARRA_DIR, OUTPUT_DIR, PROJECT_ROOT, TILES_DIR
from config.suburbs import SUBURBS
from tools.download_tiles import TILE_FILE_IDS

st.set_page_config(page_title="Raising Rooves", layout="wide")


# ── Data-readiness helpers ───────────────────────────────────────────────────


def _stage_files(suburb_key: str) -> dict[str, Path]:
    return {
        "tiles": TILES_DIR / suburb_key,
        "footprints_index": PROJECT_ROOT / "data/raw/footprints/buildings_index.gpkg",
        "stage1": OUTPUT_DIR / f"stage1_{suburb_key}.parquet",
        "stage2": OUTPUT_DIR / f"stage2_{suburb_key}.parquet",
        "stage3": OUTPUT_DIR / f"stage3_{suburb_key}.parquet",
        "map": OUTPUT_DIR / f"stage3_{suburb_key}_map.html",
        "map_fallback": OUTPUT_DIR / f"stage2_{suburb_key}_map.html",
        "report": OUTPUT_DIR / f"stage3_{suburb_key}_report.html",
        "report_fallback": OUTPUT_DIR / f"stage2_{suburb_key}_report.html",
        "summary_png": OUTPUT_DIR / f"stage3_{suburb_key}_summary.png",
        "summary_png_fallback": OUTPUT_DIR / f"stage2_{suburb_key}_summary.png",
    }


def _readiness_row(suburb_key: str) -> dict[str, str]:
    f = _stage_files(suburb_key)
    has_tiles = f["tiles"].is_dir() and any(f["tiles"].glob("*.png"))
    return {
        "suburb": SUBURBS[suburb_key].name,
        "tile_source": "Drive zip available" if suburb_key in TILE_FILE_IDS else "needs API key",
        "tiles_on_disk": "yes" if has_tiles else "no",
        "stage1": "yes" if f["stage1"].exists() else "no",
        "stage2": "yes" if f["stage2"].exists() else "no",
        "stage3": "yes" if f["stage3"].exists() else "no",
    }


def _best_output(suburb_key: str) -> tuple[pd.DataFrame | None, int]:
    f = _stage_files(suburb_key)
    if f["stage3"].exists():
        return pd.read_parquet(f["stage3"]), 3
    if f["stage2"].exists():
        return pd.read_parquet(f["stage2"]), 2
    return None, 0


# ── Pipeline runner ───────────────────────────────────────────────────────────


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", *cmd],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    return proc.returncode, output


# ── Layout ────────────────────────────────────────────────────────────────────

st.title("Raising Rooves — Model Dashboard")
st.caption(
    "Control panel over the existing pipeline. Every action here runs the "
    "same `python -m ...` command documented in README.md."
)

suburb_keys = sorted(SUBURBS.keys(), key=lambda k: SUBURBS[k].name)
suburb_key = st.sidebar.selectbox(
    "Suburb", suburb_keys, format_func=lambda k: SUBURBS[k].name
)
suburb = SUBURBS[suburb_key]
st.sidebar.write(f"Zone type: **{suburb.zone_type}**")
st.sidebar.write(f"SA2 code: `{suburb.sa2_code or 'unknown'}`")
st.sidebar.write(f"Centroid: `{suburb.centroid}`")

status = _readiness_row(suburb_key)
st.sidebar.divider()
st.sidebar.subheader("Data readiness")
for k, v in status.items():
    if k == "suburb":
        continue
    st.sidebar.write(f"{k.replace('_', ' ')}: **{v}**")

tab_run, tab_results, tab_data = st.tabs(["Run pipeline", "Results", "Data status (all suburbs)"])

# --- Run pipeline tab ---------------------------------------------------------

with tab_run:
    st.subheader(f"Run stages for {suburb.name}")
    debug = st.checkbox("Debug logging (--debug)", value=False)
    dbg_flag = ["--debug"] if debug else []

    col1, col2, col3, col4 = st.columns(4)

    if col1.button("Run Stage 1 (roof segmentation)"):
        with st.spinner("Running Stage 1..."):
            code, out = _run(
                ["stage1_segmentation.run_stage1", "--suburb", suburb.name, *dbg_flag]
            )
        st.code(out or "(no output)")
        st.success("Stage 1 finished") if code == 0 else st.error(f"Stage 1 failed (exit {code})")

    if col2.button("Run Stage 2 (irradiance)"):
        with st.spinner("Running Stage 2..."):
            code, out = _run(
                ["stage2_irradiance.run_stage2", "--suburb", suburb.name, *dbg_flag]
            )
        st.code(out or "(no output)")
        st.success("Stage 2 finished") if code == 0 else st.error(f"Stage 2 failed (exit {code})")

    if col3.button("Run Stage 3 (thermal, ~5-10 min)"):
        with st.spinner("Running Stage 3 — this can take 5-10 minutes..."):
            code, out = _run(
                ["stage3_thermal.run_stage3", "--suburb", suburb.name, *dbg_flag]
            )
        st.code(out or "(no output)")
        st.success("Stage 3 finished") if code == 0 else st.error(f"Stage 3 failed (exit {code})")

    if col4.button("Build visualisation"):
        with st.spinner("Building map/report/charts..."):
            code, out = _run(
                ["tools.visualise_results", "--suburb", suburb.name, *dbg_flag]
            )
        st.code(out or "(no output)")
        st.success("Visualisation finished") if code == 0 else st.error(
            f"Visualisation failed (exit {code})"
        )

    st.divider()
    st.caption(
        "Stage 1 needs satellite tiles (Google Drive zip for Carlton/Clayton, "
        "or your own GOOGLE_MAPS_API_KEY for any other suburb — see the "
        "'Data status' tab). Stage 3 needs Stage 2 output plus hourly BARRA2 "
        "weather, auto-resolved per README.md."
    )

# --- Results tab ---------------------------------------------------------------

with tab_results:
    df, stage_used = _best_output(suburb_key)
    if df is None:
        st.info(f"No Stage 2/3 output yet for {suburb.name}. Run the pipeline first.")
    else:
        st.subheader(f"{suburb.name} — Stage {stage_used} output ({len(df)} buildings)")

        if stage_used == 3 and "electricity_saved_kwh_yr" in df.columns:
            area_col = "roof_surface_area_m2" if "roof_surface_area_m2" in df.columns else None
            per_building_kwh = df["net_electricity_saved_kwh_yr"].mean()
            per_m2_kwh = (
                (df["net_electricity_saved_kwh_yr"] / df[area_col]).replace(
                    [float("inf"), float("-inf")], pd.NA
                ).dropna().mean()
                if area_col
                else float("nan")
            )
            c1, c2, c3 = st.columns(3)
            c1.metric("Mean net electricity saved / building / yr", f"{per_building_kwh:,.1f} kWh")
            c2.metric("Mean net electricity saved / m² roof / yr", f"{per_m2_kwh:,.2f} kWh/m²")
            c3.metric("Buildings modelled", f"{len(df):,}")
            st.caption(
                "Per-building and per-m² figures are the reporting headline per "
                "CLAUDE.md — suburb totals below are supporting context only."
            )
        elif stage_used == 2 and "energy_saved_kwh_yr" in df.columns:
            c1, c2 = st.columns(2)
            c1.metric("Mean absorbed-solar reduction / building / yr", f"{df['energy_saved_kwh_yr'].mean():,.1f} kWh")
            c2.metric("Buildings", f"{len(df):,}")
            st.caption(
                "Stage 2 only — this is absorbed solar reduction, not electricity "
                "savings. Run Stage 3 for electricity/CO2 figures."
            )

        f = _stage_files(suburb_key)
        report = f["report"] if f["report"].exists() else f["report_fallback"]
        map_file = f["map"] if f["map"].exists() else f["map_fallback"]
        summary_png = f["summary_png"] if f["summary_png"].exists() else f["summary_png_fallback"]

        st.divider()
        if map_file.exists():
            st.subheader("Interactive map")
            st.components.v1.html(map_file.read_text(), height=600, scrolling=True)
        else:
            st.caption("No map yet — click 'Build visualisation' in the Run tab.")

        if summary_png.exists():
            st.subheader("Summary charts")
            st.image(str(summary_png))

        if report.exists():
            st.caption(f"Full HTML report: `{report.relative_to(PROJECT_ROOT)}`")

        with st.expander("Raw table"):
            st.dataframe(df)

# --- Data status tab -----------------------------------------------------------

with tab_data:
    st.subheader("Which suburbs can we run right now?")
    st.write(
        f"{len(SUBURBS)} suburbs are configured in `config/suburbs.py`. "
        "Running Stage 1 for any of them needs satellite tiles first."
    )
    st.markdown(
        "- **Pre-fetched tile zips exist for only 2 suburbs** on the team "
        f"Google Drive: {', '.join(SUBURBS[k].name for k in TILE_FILE_IDS)}. "
        "Download the zip manually (Drive is account-shared, not link-shared) "
        "and extract into `data/raw/tiles/{suburb}/`.\n"
        "- **Every other suburb needs a `GOOGLE_MAPS_API_KEY`** in `.env` so "
        "Stage 1 can download its own tiles fresh.\n"
        "- The footprint spatial index (`data/raw/footprints/buildings_index.gpkg`) "
        "is optional but recommended for all suburbs — build it once with "
        "`python -m tools.build_footprint_index` after fetching "
        "`melbourne_overture.geojsonl` (see README.md 'Data Needed')."
    )

    rows = [_readiness_row(k) for k in suburb_keys]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    footprint_index = PROJECT_ROOT / "data/raw/footprints/buildings_index.gpkg"
    barra_hourly_samples = list(BARRA_DIR.glob("**/*"))
    st.divider()
    st.write(
        f"Footprint spatial index present: **{'yes' if footprint_index.exists() else 'no'}**"
    )
    st.write(f"Cached BARRA2 files on disk: **{len(barra_hourly_samples)}**")
