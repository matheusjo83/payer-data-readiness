"""Data quality history: the quality metrics of each pipeline run over time.

One series at a time (source, branch and scenario: the CI runs of main by
default). The headline numbers are the latest run's, with the change from the
run before it; the tabs follow each area run by run.
"""

import altair as alt
import pandas as pd
import streamlit as st

import data

st.set_page_config(page_title="Data quality history", page_icon=":bar_chart:", layout="wide")

# Reference palette (categorical slots in fixed order, one step per mode).
LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"]
theme = getattr(getattr(st, "context", None), "theme", None)
dark = getattr(theme, "type", None) == "dark"
PALETTE = DARK if dark else LIGHT
INK = "#c3c2b7" if dark else "#52514e"
SURFACE = "#1a1a19" if dark else "#fcfcfb"
GRID = "#3a3a38" if dark else "#e7e6e2"
DEFAULT_SERIES = "ci · main · base"


@st.cache_data(ttl=60)
def load():
    return data.load()


runs, history = load()
all_history = history

st.title("Data quality history")
st.caption("Load reliability, CDC latency, FHIR conformance and linkage quality, run by run. "
           "Runs rebuilt from their CI reports have no lakehouse metrics (load, CDC latency, linkage).")

if runs.empty:
    st.info("No runs yet: run `make dq-snapshot dq-history`.")
    st.stop()

series_options = sorted(runs["series"].unique(), key=lambda s: (s != DEFAULT_SERIES, s))
series = st.selectbox("Series (source · branch · scenario)", series_options, key="series")
runs = runs[runs["series"] == series].sort_values("run_number")
history = history[history["series"] == series]
labels = {r.run_key: f"#{r.run_number} {r.short_commit or '?'}" for r in runs.itertuples()}
order = [labels[k] for k in runs["run_key"]]
history = history.assign(run=history["run_key"].map(labels))
latest = runs.iloc[-1]
previous = runs.iloc[-2] if len(runs) > 1 else None


def value(run_key, area, metric, dimension="", agg="sum"):
    rows = history[(history["run_key"] == run_key) & (history["area"] == area) & (history["metric"] == metric)]
    if dimension is not None:
        rows = rows[rows["dimension"] == dimension]
    if rows.empty or rows["value"].isna().all():
        return None
    return float(rows["value"].agg(agg))


def failed_checks(run_key):
    passed = value(run_key, "fhir", "api_checks_passed", None)
    total = value(run_key, "fhir", "api_checks_total", None)
    return None if passed is None else total - passed


def failing_tests(run_key):
    fail = value(run_key, "tests", "dbt_tests", "fail")
    error = value(run_key, "tests", "dbt_tests", "error")
    return None if fail is None else fail + (error or 0)


KPIS = [
    ("API checks failed", lambda k: failed_checks(k), "{:.0f}", "inverse"),
    ("HL7 validation errors", lambda k: value(k, "fhir", "validation_errors", None), "{:.0f}", "inverse"),
    ("dbt tests failing", failing_tests, "{:.0f}", "inverse"),
    ("Load success rate", lambda k: value(k, "load", "load_success_rate_pct"), "{:.1f}%", "normal"),
    ("CDC p95 latency", lambda k: value(k, "cdc", "cdc_p95_latency_seconds"), "{:.2f} s", "inverse"),
    ("Linkage recall (Splink, reviewed)", lambda k: value(k, "linkage", "match_recall", "splink_reviewed"),
     "{:.4f}", "normal"),
]

st.subheader(f"Latest run: {labels[latest.run_key]}")
st.caption(f"{pd.Timestamp(latest.run_at):%Y-%m-%d %H:%M} · {latest.metrics_from} · {int(latest.metrics)} metrics"
           + (f" · GitHub run {int(latest.ci_run_id)}" if pd.notna(latest.ci_run_id) else ""))
for col, (label, fn, fmt, delta_color) in zip(st.columns(len(KPIS)), KPIS):
    now = fn(latest.run_key)
    before = fn(previous.run_key) if previous is not None else None
    delta = None if now is None or before is None else now - before
    col.metric(label, "—" if now is None else fmt.format(now),
               None if not delta else fmt.format(delta).replace("%", " pts"),
               delta_color=delta_color,
               help=None if now is not None else "Not in this run's metrics.")


def series_chart(metric, area, title, y_title, dimensions=None, total=False, fmt=",.4~f", frame=None):
    """One line per dimension (or the total, dimension ''), run by run."""
    frame = history if frame is None else frame
    rows = frame[(frame["area"] == area) & (frame["metric"] == metric)]
    rows = rows[rows["dimension"] == ""] if total else rows[rows["dimension"] != ""]
    if dimensions:
        rows = rows[rows["dimension"].isin(dimensions)]
    rows = rows.dropna(subset=["value"])
    st.markdown(f"**{title}**")
    if rows.empty:
        st.caption("No values in this series: only runs that took their own snapshot have it, "
                   "not those rebuilt from their CI reports.")
        return
    domain = dimensions or sorted(rows["dimension"].unique())
    color = (alt.Color("dimension:N", title=None, scale=alt.Scale(domain=domain, range=PALETTE[:len(domain)]),
                       legend=alt.Legend(orient="top", labelColor=INK))
             if not total and len(domain) > 1 else alt.value(PALETTE[0]))
    base = alt.Chart(rows).encode(
        x=alt.X("run:O", sort=order, title=None, axis=alt.Axis(labelAngle=-40, labelColor=INK, domainColor=GRID,
                                                              tickColor=GRID)),
        y=alt.Y("value:Q", title=y_title, axis=alt.Axis(labelColor=INK, titleColor=INK, gridColor=GRID,
                                                        domain=False, ticks=False)),
        color=color,
        tooltip=[alt.Tooltip("run:N", title="Run"), alt.Tooltip("run_at:T", title="When", format="%Y-%m-%d %H:%M"),
                 alt.Tooltip("dimension:N", title="Of"), alt.Tooltip("value:Q", title=y_title, format=fmt),
                 alt.Tooltip("change:Q", title="Change", format=fmt)],
    )
    chart = base.mark_line(strokeWidth=2) + base.mark_point(size=70, filled=True, opacity=1, stroke=SURFACE, strokeWidth=2)
    st.altair_chart(chart.properties(height=260).configure_view(stroke=None), use_container_width=True)


load_tab, cdc_tab, fhir_tab, linkage_tab, dq_tab, perf_tab, scale_tab, runs_tab = st.tabs(
    ["Load reliability", "CDC latency", "FHIR conformance", "Linkage quality", "Data quality and tests",
     "Performance", "Scale", "Runs"])

with load_tab:
    c1, c2 = st.columns(2)
    with c1:
        series_chart("load_success_rate_pct", "load", "Load success rate, all sources", "%", total=True, fmt=".1f")
    with c2:
        series_chart("load_failed_runs", "load", "Failed load steps, all sources", "steps", total=True, fmt=".0f")
    series_chart("load_rows", "load", "Rows loaded into bronze", "rows", total=True, fmt=",.0f")

with cdc_tab:
    c1, c2 = st.columns(2)
    with c1:
        series_chart("cdc_p95_latency_seconds", "cdc", "CDC p95 latency, slowest table", "seconds", total=True,
                     fmt=".2f")
    with c2:
        series_chart("cdc_max_latency_seconds", "cdc", "CDC maximum latency", "seconds", total=True, fmt=".2f")
    c1, c2 = st.columns(2)
    with c1:
        series_chart("reconcile_mismatched_rows", "cdc", "Rows differing from Postgres after CDC", "rows",
                     total=True, fmt=",.0f")
    with c2:
        series_chart("cdc_changes", "cdc", "Changes captured", "changes", total=True, fmt=",.0f")

with fhir_tab:
    series_chart("api_checks_passed", "fhir", "API checks passed, per suite", "checks",
                 dimensions=["patient-access", "provider-access", "security", "prior-auth"], fmt=".0f")
    c1, c2 = st.columns(2)
    with c1:
        series_chart("validation_errors", "fhir", "HL7 validation errors, per implementation guide", "errors",
                     fmt=".0f")
    with c2:
        series_chart("validation_warnings", "fhir", "HL7 validation warnings, per implementation guide",
                     "warnings", fmt=",.0f")
    c1, c2 = st.columns(2)
    with c1:
        series_chart("sof_tests_passed", "fhir", "SQL on FHIR tests passed", "tests",
                     dimensions=["shareable", "experimental"], fmt=".0f")
    with c2:
        series_chart("view_parity_mismatched_rows", "fhir", "ViewDefinition rows differing from their table",
                     "rows", total=True, fmt=",.0f")

with linkage_tab:
    methods = ["deterministic", "splink", "splink_reviewed"]
    c1, c2 = st.columns(2)
    with c1:
        series_chart("match_precision", "linkage", "PBM to legacy linkage: precision", "precision",
                     dimensions=methods)
    with c2:
        series_chart("match_recall", "linkage", "PBM to legacy linkage: recall", "recall", dimensions=methods)
    c1, c2 = st.columns(2)
    with c1:
        series_chart("dedupe_precision", "linkage", "PBM deduplication: precision", "precision",
                     dimensions=["splink", "splink_reviewed"])
    with c2:
        series_chart("dedupe_recall", "linkage", "PBM deduplication: recall", "recall",
                     dimensions=["splink", "splink_reviewed"])

with dq_tab:
    series_chart("issue_rate_pct", "data_quality", "Legacy issues detected, share of the population", "%",
                 fmt=".2f")
    series_chart("dbt_tests", "tests", "dbt tests by result", "tests", dimensions=["pass", "warn", "fail", "error"],
                 fmt=".0f")

with perf_tab:
    durations = runs.dropna(subset=["duration_seconds"]).assign(
        run=lambda d: d["run_key"].map(labels), area="performance", metric="ci_run_minutes", dimension="",
        value=lambda d: d["duration_seconds"] / 60,
        change=lambda d: d["value"].diff())
    series_chart("ci_run_minutes", "performance", "CI run duration", "minutes", total=True, fmt=".1f",
                 frame=durations)
    c1, c2 = st.columns(2)
    with c1:
        series_chart("dbt_build_seconds", "performance", "dbt build", "seconds", total=True, fmt=".0f")
    with c2:
        series_chart("bulk_export_seconds", "performance", "Bulk export", "seconds",
                     dimensions=["patient-access", "payer-to-payer"], fmt=".1f")

# --- Scale benchmark: the latest run of each scale factor, side by side -----------

def scale_runs_of(frame, benchmark):
    """The latest run of each scale factor of a benchmark (source · branch)."""
    rows = frame[(frame["area"] == "scale") & (frame["benchmark"] == benchmark)]
    latest_runs = (rows[["run_key", "scenario", "run_at"]].drop_duplicates()
                   .sort_values("run_at").groupby("scenario").tail(1))
    factors = rows[rows["metric"] == "scale_factor"].set_index("run_key")["value"]
    latest_runs = latest_runs.assign(factor=latest_runs["run_key"].map(factors)).sort_values("factor")
    return latest_runs


def scale_chart(frame, metric, area, title, y_title, dimensions, fmt=",.0f", log=False, total_label=None):
    """One line per dimension, across the scale factors."""
    rows = frame[(frame["area"] == area) & (frame["metric"] == metric)]
    if total_label:
        rows = rows[rows["dimension"].isin(dimensions + [""])].assign(
            dimension=lambda d: d["dimension"].replace("", total_label))
        dimensions = dimensions + [total_label]
    else:
        rows = rows[rows["dimension"].isin(dimensions)]
    rows = rows.dropna(subset=["value"])
    if log:
        rows = rows[rows["value"] > 0]
    st.markdown(f"**{title}**")
    if rows.empty:
        st.caption("No values in this benchmark.")
        return
    domain = [d for d in dimensions if d in set(rows["dimension"])]
    base = alt.Chart(rows).encode(
        x=alt.X("scenario:O", sort=scale_order, title="scale", axis=alt.Axis(labelAngle=0, labelColor=INK,
                                                                           titleColor=INK, domainColor=GRID,
                                                                           tickColor=GRID)),
        y=alt.Y("value:Q", title=y_title, scale=alt.Scale(type="log") if log else alt.Undefined,
                axis=alt.Axis(labelColor=INK, titleColor=INK, gridColor=GRID, domain=False, ticks=False)),
        color=alt.Color("dimension:N", title=None, sort=domain,
                        scale=alt.Scale(domain=domain, range=PALETTE[:len(domain)]),
                        legend=alt.Legend(orient="top", labelColor=INK, labelLimit=260)),
        tooltip=[alt.Tooltip("scenario:N", title="Scale"), alt.Tooltip("dimension:N", title="Of"),
                 alt.Tooltip("value:Q", title=y_title, format=fmt),
                 alt.Tooltip("run_at:T", title="When", format="%Y-%m-%d %H:%M")],
    )
    chart = base.mark_line(strokeWidth=2) + base.mark_point(size=70, filled=True, opacity=1, stroke=SURFACE,
                                                             strokeWidth=2)
    st.altair_chart(chart.properties(height=260).configure_view(stroke=None), use_container_width=True)


with scale_tab:
    scaled = all_history.assign(benchmark=all_history["source"] + " · " + all_history["branch"].fillna("?"))
    benchmarks = sorted(scaled.loc[scaled["area"] == "scale", "benchmark"].unique(),
                        key=lambda b: (b != "ci · main", b))
    if not benchmarks:
        st.info("No scale benchmark yet: run `make scale` (or the Scale benchmark workflow), then `make dq-history`.")
    else:
        benchmark = st.selectbox("Benchmark (source · branch)", benchmarks, key="benchmark")
        chosen = scale_runs_of(scaled, benchmark)
        scale_order = list(chosen["scenario"])
        frame = scaled[scaled["run_key"].isin(chosen["run_key"])]
        st.caption("The latest run of each scale factor. Synthea and the FHIR APIs stay at the base volume; "
                   "the linkage is the model's own decisions (no reviewer decisions at any scale).")

        def at(run_key, area, metric, dimension=""):
            r = frame[(frame["run_key"] == run_key) & (frame["area"] == area) & (frame["metric"] == metric)
                      & (frame["dimension"] == dimension)]["value"]
            return None if r.empty else float(r.iloc[0])

        def step_max(run_key, metric):
            r = frame[(frame["run_key"] == run_key) & (frame["area"] == "scale") & (frame["metric"] == metric)]
            return None if r.empty else float(r["value"].max())

        summary = pd.DataFrame([{
            "scale": r.scenario,
            "legacy members": at(r.run_key, "volume", "legacy_members"),
            "legacy claims": at(r.run_key, "volume", "legacy_claims"),
            "PBM cardholders": at(r.run_key, "volume", "pbm_members"),
            "pipeline minutes": (at(r.run_key, "scale", "total_seconds") or 0) / 60,
            "peak process memory (MB)": step_max(r.run_key, "step_peak_rss_mb"),
            "Parquet (MB)": at(r.run_key, "scale", "disk_mb", "lakehouse_parquet"),
            "linkage candidate pairs": at(r.run_key, "scale", "candidate_pairs", "link"),
            "Splink precision": at(r.run_key, "linkage", "match_precision", "splink"),
            "Splink recall": at(r.run_key, "linkage", "match_recall", "splink"),
            "rows differing after CDC": at(r.run_key, "cdc", "reconcile_mismatched_rows"),
            "run": r.run_key,
        } for r in chosen.itertuples()])
        st.dataframe(summary, hide_index=True, use_container_width=True, column_config={
            "pipeline minutes": st.column_config.NumberColumn(format="%.1f"),
            "Splink precision": st.column_config.NumberColumn(format="%.4f"),
            "Splink recall": st.column_config.NumberColumn(format="%.4f")})

        c1, c2 = st.columns(2)
        with c1:
            scale_chart(frame, "step_seconds", "scale", "Initial load: time per step", "seconds",
                        ["seed", "pbm", "cdc_initial_load", "pbm_load", "dbt_build"], fmt=",.1f")
        with c2:
            scale_chart(frame, "step_seconds", "scale", "Incremental refresh: time per step", "seconds",
                        ["change_batch", "cdc_incremental_sync", "dbt_incremental", "dbt_rebuild"], fmt=",.1f")
        c1, c2 = st.columns(2)
        with c1:
            scale_chart(frame, "step_peak_rss_mb", "scale", "Peak memory of the pipeline's processes", "MB",
                        ["cdc_initial_load", "pbm_load", "dbt_build", "dbt_incremental", "blocking_analysis",
                         "cdc_reconcile"])
        with c2:
            scale_chart(frame, "disk_mb", "scale", "Size on disk", "MB",
                        ["lakehouse_parquet", "lakehouse_catalog", "legacy_db", "legacy_wal", "pbm_feed"],
                        fmt=",.1f")
        link_rules = ["link:birth_date", "link:first_name_canonical+last_name", "link:last_name+zip_code",
                      "link:first_name_canonical+zip_code"]
        c1, c2 = st.columns(2)
        with c1:
            scale_chart(frame, "candidate_pairs", "scale", "Splink linkage: candidate pairs each blocking rule adds",
                        "pairs (log)", link_rules, log=True)
        with c2:
            # Pairs the blocking rules produce, and those the model kept (probability 0.01 or more).
            pairs = frame[(frame["area"] == "scale") & frame["metric"].isin(["candidate_pairs", "scored_pairs"])
                          & frame["dimension"].isin(["link", "dedupe"])]
            pairs = pairs.assign(metric="pairs", dimension=pairs["dimension"] + " " + pairs["metric"].map(
                {"candidate_pairs": "candidates", "scored_pairs": "kept"}))
            scale_chart(pairs, "pairs", "scale", "Splink: candidate pairs and pairs kept (probability ≥ 0.01)",
                        "pairs (log)", ["link candidates", "link kept", "dedupe candidates", "dedupe kept"],
                        log=True)
        c1, c2 = st.columns(2)
        with c1:
            scale_chart(frame, "match_precision", "linkage", "PBM to legacy linkage: precision", "precision",
                        ["deterministic", "splink"], fmt=".4f")
        with c2:
            scale_chart(frame, "match_recall", "linkage", "PBM to legacy linkage: recall", "recall",
                        ["deterministic", "splink"], fmt=".4f")

with runs_tab:
    table = runs.assign(run=runs["run_key"].map(labels))[
        ["run", "run_at", "commit", "branch", "metrics_from", "conclusion", "duration_seconds", "metrics", "areas",
         "legacy_members", "legacy_claims", "fhir_resources_published", "run_key"]]
    st.dataframe(table.iloc[::-1], hide_index=True, use_container_width=True)
