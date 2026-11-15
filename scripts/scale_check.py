"""Checks of the scale benchmark (make scale-check): each scale factor's results
against its sources, their place in the quality history and the dashboard's
Scale tab.

    benchmark   per scale factor: every step finished; the legacy tables at
                the factor times the base volumes (plus the change batch); the
                PBM feed and its ground truth scaled with the members; Splink's
                inputs equal to the members and cardholders; the candidate
                pairs adding up, each total at most the possible pairs, the
                pairs kept at most the candidates; the CDC current state equal
                to Postgres after the change batch; no dbt test failing but
                the identity quality gates, whose outcome is recorded;
                precision and recall for both linkage methods
    history     every scale snapshot in bronze._quality_runs and in
                gold.quality_history, with all its metrics
    dashboard   the Scale tab (Streamlit AppTest, in the dashboard container):
                no error, the benchmark offered, one row per scale factor with
                the snapshot's values

Run after make scale, make dq-history and make dashboard-up. Writes
data/scale/check.json and exits with status 1 if a check fails.
"""

import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingestion"))
sys.path.insert(0, str(ROOT / "scripts"))

import lakehouse  # noqa: E402
import quality_check as qc  # noqa: E402
import scale_benchmark as sb  # noqa: E402

OUT = ROOT / "data" / "scale"
REPORT = OUT / "check.json"
STEPS = ["seed", "pbm", "cdc_initial_load", "pbm_load", "fhir_terminology_load", "dbt_build", "blocking_analysis",
         "change_batch", "cdc_incremental_sync", "dbt_incremental", "dbt_rebuild", "cdc_reconcile"]
# Rows the generator writes per unit of each base volume (scaled by the factor).
LEGACY_EXACT = {"prv": "providers", "elig_span": "members"}


def check_benchmark(checks, report, snap):
    scenario, factor = report["scenario"], report["factor"]
    checks.group = f"benchmark {scenario}"
    v = qc.values(snap["metrics"])
    volumes, rows, events = report["volumes"], report["legacy_rows"], report["change_batch_events"]

    # A dbt step exits 1 when only the identity quality gates fail: finished.
    checks.check("every step finished", STEPS,
                 [s["step"] for s in report["steps"]
                  if s["exit_code"] == 0 or (s.get("failed") and sb.only_identity_gates(s["failed"]))])
    checks.check("volumes are the factor times the base ones",
                 {k: b * factor for k, b in sb.BASE_VOLUMES.items()}, volumes)
    checks.check("providers and eligibility spans as generated",
                 {t: volumes[k] for t, k in LEGACY_EXACT.items()}, {t: rows[t] for t in LEGACY_EXACT})
    # Claims and prior authorizations: as generated, plus the ones the change batch
    # added (at most one per event), minus the voided claims it purged.
    for table, key in (("clm_hdr", "claims"), ("pa_req", "prior-auths")):
        checks.check(f"{table} within the change batch of the generated volume", True,
                     volumes[key] - events <= rows[table] <= volumes[key] + events)
    checks.check("legacy members in silver", float(volumes["members"]), v.get(("volume", "legacy_members", "")))
    pbm = v.get(("volume", "pbm_members", ""))
    share = pbm / volumes["members"] if pbm else None
    checks.check("PBM cardholders scaled with the members (0.6 to 0.75 of them)", True,
                 share is not None and 0.6 <= share <= 0.75)
    checks.check("Splink inputs: the members and the cardholders",
                 [float(volumes["members"]), pbm],
                 [v.get(("scale", "splink_records", "legacy")), v.get(("scale", "splink_records", "pbm"))])

    pairs = report["candidate_pairs"]
    n_legacy, n_pbm = pairs["records"]["legacy"], pairs["records"]["pbm"]
    checks.check("possible pairs: legacy x PBM, and PBM pairs",
                 [n_legacy * n_pbm, n_pbm * (n_pbm - 1) // 2],
                 [pairs["link"][-1]["possible"], pairs["dedupe"][-1]["possible"]])
    for kind in ("link", "dedupe"):
        rules = pairs[kind]
        running = [sum(r["marginal"] for r in rules[: i + 1]) for i in range(len(rules))]
        checks.check(f"{kind}: the pairs each rule adds sum to the running total",
                     [r["cumulative"] for r in rules], running)
        candidates, kept = v.get(("scale", "candidate_pairs", kind)), v.get(("scale", "scored_pairs", kind))
        checks.check(f"{kind}: pairs kept <= candidates <= possible pairs", True,
                     None not in (candidates, kept) and kept <= candidates <= rules[-1]["possible"])

    checks.check("CDC current state equal to Postgres after the change batch", 0.0,
                 v.get(("cdc", "reconcile_mismatched_rows", "")))
    checks.check("the change batch captured by CDC", True, (v.get(("cdc", "cdc_changes", "")) or 0) >= events * 0.9)
    # Of the first build. The identity quality gates may fail at another volume
    # (recorded as a result); nothing else may.
    gates = report["quality_gates_failed"]
    build = next(s for s in report["steps"] if s["step"] == "dbt_build")
    checks.check("no dbt test failing but the identity quality gates", len(build.get("failed", [])),
                 v.get(("tests", "dbt_tests", "fail"), 0) + v.get(("tests", "dbt_tests", "error"), 0))
    checks.check("no dbt failure in any step but the identity quality gates", [],
                 [f for s in report["steps"] for f in s.get("failed", [])
                  if not sb.only_identity_gates([f])])
    checks.check("the gates' outcome recorded", {g: float(g in gates) for g in sorted(sb.IDENTITY_GATES)},
                 {g: v.get(("scale", "quality_gate_failed", g)) for g in sorted(sb.IDENTITY_GATES)})
    found = {m: [v.get(("linkage", m, d)) is not None for d in ("deterministic", "splink")]
             for m in ("match_precision", "match_recall")}
    checks.check("precision and recall for both linkage methods",
                 {m: [True, True] for m in found}, found)
    checks.check("no reviewer decision (splink_reviewed left out)", [],
                 [k for k in v if k[2] == "splink_reviewed"])


def check_history(checks, con, snaps):
    checks.group = "history"
    keys = [s["run_key"] for s in snaps]
    runs = dict(con.execute("select run_key, scenario from bronze._quality_runs").fetchall())
    checks.check("every scale snapshot collected, with its scenario",
                 {s["run_key"]: s["scenario"] for s in snaps}, {k: runs.get(k) for k in keys})
    in_gold = dict(con.execute("select run_key, count(*) from gold.quality_history group by run_key").fetchall())
    checks.check("every metric in gold.quality_history",
                 {s["run_key"]: len(s["metrics"]) for s in snaps}, {k: in_gold.get(k) for k in keys})


APP_TEST = """
import json, sys
from streamlit.testing.v1 import AppTest
at = AppTest.from_file("app.py", default_timeout=120).run()
box = next(b for b in at.selectbox if b.key == "benchmark")
out = {"exceptions": [e.message for e in at.exception], "benchmarks": list(box.options)}
at = box.set_value(sys.argv[1]).run()
table = next(d.value for d in at.dataframe if "scale" in d.value.columns)
out["after"] = [e.message for e in at.exception]
out["rows"] = json.loads(table.to_json(orient="records"))
print("APPTEST" + json.dumps(out))
"""


def check_dashboard(checks, snaps):
    checks.group = "dashboard"
    benchmark = f"{snaps[0]['source']} · {snaps[0]['branch']}"
    out = subprocess.run(["docker", "exec", qc.CONTAINER, "python", "-c", APP_TEST, benchmark],
                         capture_output=True, text=True, timeout=300).stdout
    line = next((x for x in out.splitlines() if x.startswith("APPTEST")), None)
    if not checks.check("the app runs headless", True, line is not None):
        return
    result = json.loads(line.removeprefix("APPTEST"))
    checks.check("no error on the page", [], result["exceptions"] + result["after"])
    checks.check("this benchmark offered", True, benchmark in result["benchmarks"])
    rows = {r["scale"]: r for r in result["rows"]}
    # The latest run of each scale factor of this benchmark: the ones just run.
    expected, shown = {}, {}
    for s in snaps:
        v = qc.values(s["metrics"])
        expected[s["scenario"]] = [s["run_key"], v.get(("volume", "legacy_members", "")),
                                   v.get(("scale", "disk_mb", "lakehouse_parquet")),
                                   v.get(("scale", "candidate_pairs", "link")),
                                   v.get(("linkage", "match_recall", "splink"))]
        r = rows.get(s["scenario"], {})
        shown[s["scenario"]] = [r.get("run"), r.get("legacy members"), r.get("Parquet (MB)"),
                                r.get("linkage candidate pairs"), r.get("Splink recall")]
    checks.check("one row per scale factor, with the snapshot's values", expected, shown)


def main():
    reports = sorted((json.loads(f.read_text()) for f in OUT.glob("*/benchmark.json")), key=lambda r: r["factor"])
    if not reports:
        raise SystemExit("No benchmark in data/scale/: run make scale first.")
    checks = qc.Checks()
    snaps = []
    for report in reports:
        snap = json.loads((OUT / f"quality_snapshot_{report['scenario']}.json").read_text())
        snaps.append(snap)
        check_benchmark(checks, report, snap)
    con = lakehouse.connect(read_only=True)
    try:
        check_history(checks, con, snaps)
    finally:
        con.close()
    check_dashboard(checks, snaps)

    by_group = defaultdict(lambda: [0, 0])
    for r in checks.results:
        by_group[r["group"]][0] += r["passed"]
        by_group[r["group"]][1] += 1
    report = {"scenarios": [r["scenario"] for r in reports],
              "passed": sum(r["passed"] for r in checks.results), "total": len(checks.results),
              "groups": {g: {"passed": p, "total": t} for g, (p, t) in by_group.items()},
              "failed": checks.failed, "checks": checks.results}
    REPORT.write_text(json.dumps(report, indent=2, default=str))
    for g, (p, t) in by_group.items():
        print(f"{g:16} {p:>4}/{t}")
    print(f"{report['passed']}/{report['total']} checks passed. Written to {REPORT.relative_to(ROOT)}")
    for r in checks.failed[:30]:
        print(f"  FAILED {r['group']} {r['check']}: expected {str(r['expected'])[:200]}, got {str(r['actual'])[:200]}")
    if checks.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
