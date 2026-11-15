"""Check the data quality history (make dq-check), after dq-snapshot, dq-history
and dashboard-up. Every expected value is computed again from its source, not
taken from scripts/quality_history.py:

  snapshot   data/quality_snapshot.json has the run's fields and every area, and
             its metrics equal their sources: the API check and validator
             reports, the dbt results, the CDC reconciliation, the gold marts
             (linkage, data quality issues), the CDC changes in bronze, and the
             row counts;
  history    bronze._quality_runs has every snapshot collected and every
             finished CI run of main that GitHub lists (when a token is at
             hand); a run rebuilt from its reports has no lakehouse metric, and
             its values equal its reports artifact, downloaded again here; this
             run is there with all its metrics;
  gold       gold.quality_history has every metric once, and the previous value
             of a metric is the one the previous run of its series had;
  dashboard  the container answers its health check, and the app, run headless
             in it (Streamlit AppTest), shows no error, and the headline numbers
             of the latest run of main and of this run's series equal gold.

Writes data/quality_check.json.
"""

import io
import json
import subprocess
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingestion"))
sys.path.insert(0, str(ROOT / "scripts"))

import lakehouse  # noqa: E402
import quality_history as qh  # noqa: E402  (only for the GitHub client and paths)

DATA = ROOT / "data"
REPORT = DATA / "quality_check.json"
CONTAINER = "payer-quality-dashboard"
DASHBOARD = "http://localhost:8501"
AREAS = {"load", "cdc", "fhir", "linkage", "data_quality", "tests", "performance", "volume"}
LAKEHOUSE_AREAS = {"load", "linkage", "data_quality"}
LEGACY_TABLES = ["pln", "mbr_mstr", "elig_span", "prv", "clm_hdr", "clm_ln", "pa_req", "pa_doc", "mbr_cnsnt"]


class Checks:
    def __init__(self):
        self.results, self.failed = [], []
        self.group = ""

    def check(self, name, expected, actual, passed=None):
        ok = expected == actual if passed is None else bool(passed)
        r = {"group": self.group, "check": name, "expected": expected, "actual": actual, "passed": ok}
        self.results.append(r)
        if not ok:
            self.failed.append(r)
        return ok


def load(path):
    f = DATA / path
    return json.loads(f.read_text()) if f.is_file() else None


def values(metrics):
    return {(m["area"], m["metric"], m["dimension"]): m["value"] for m in metrics}


def close(a, b):
    return a is not None and b is not None and abs(a - b) < 1e-6


def check_snapshot(checks, con, snap):
    checks.group = "snapshot"
    v = values(snap["metrics"])
    checks.check("run fields", [], [k for k in ("run_key", "run_at", "commit", "branch", "scenario", "source")
                                    if not snap.get(k)])
    checks.check("every area", sorted(AREAS), sorted({m["area"] for m in snap["metrics"]}))
    checks.check("one value per area, metric and dimension", len(snap["metrics"]), len(v))

    for path, suite in qh.API_CHECKS.items():
        report = load(Path(path).name)
        if report:
            checks.check(f"API checks passed, {suite}", float(report["passed"]),
                         v.get(("fhir", "api_checks_passed", suite)))
    summary = load("fhir_validation_carin-bb/summary.json")
    if summary:
        errors = sum(r["errors"] for r in summary["by_resource"].values())
        checks.check("validation errors, CARIN Blue Button", float(errors), v.get(("fhir", "validation_errors", "carin-bb")))
    results = load("dbt_run_results.json")
    if not checks.check("dbt results kept (data/dbt_run_results.json)", True, results is not None):
        results = {"results": []}
    passing = sum(1 for r in results["results"] if r["unique_id"].startswith("test.") and r["status"] == "pass")
    checks.check("dbt tests passing", float(passing), v.get(("tests", "dbt_tests", "pass")))
    reconcile = load("cdc_reconcile.json")
    if reconcile:
        mismatched = sum(t["only_in_source"] + t["only_in_lakehouse"] for t in reconcile)
        checks.check("rows differing from Postgres", float(mismatched), v.get(("cdc", "reconcile_mismatched_rows", "")))

    for method, precision, recall in con.execute(
            "select method, precision, recall from gold.identity_match_quality where scope = 'all'").fetchall():
        checks.check(f"linkage precision and recall, {method}", [precision, recall],
                     [v.get(("linkage", "match_precision", method)), v.get(("linkage", "match_recall", method))])
    for name, count in con.execute("select check_name, issue_count from gold.dq_issue_summary").fetchall():
        checks.check(f"data quality issues, {name}", float(count), v.get(("data_quality", "issue_count", name)))

    # The CDC changes in bronze now: the snapshot must have read marts rebuilt
    # after the last sync, not the ones the full dbt build left.
    changes = sum(con.execute(f"select count(*) from bronze.legacy_{t}_changes where _op <> 'S'").fetchone()[0]
                  for t in LEGACY_TABLES)
    checks.check("CDC changes equal bronze now", float(changes), v.get(("cdc", "cdc_changes", "")))
    checks.check("CDC latency recorded", True, v.get(("cdc", "cdc_p95_latency_seconds", "")) is not None)
    members = con.execute("select count(*) from silver.stg_legacy__members").fetchone()[0]
    checks.check("volume: legacy members", float(members), v.get(("volume", "legacy_members", "")))
    loads = con.execute("select count(*), count(*) filter (where status = 'success') from bronze._load_log").fetchone()
    checks.check("load steps", [float(loads[0]), round(100.0 * loads[1] / loads[0], 1)],
                 [v.get(("load", "load_runs", "")), v.get(("load", "load_success_rate_pct", ""))])


def check_history(checks, con, snap):
    checks.group = "history"
    runs = {r[0]: r for r in con.execute(
        "select run_key, source, metrics_from, ci_run_id, commit from bronze._quality_runs").fetchall()}
    files = {json.loads(f.read_text())["run_key"]
             for f in list(qh.CI_DIR.glob("*.json")) + list(qh.LOCAL_DIR.glob("*.json"))} | {snap["run_key"]}
    checks.check("every snapshot collected", sorted(files), sorted(runs))

    count = dict(con.execute("select run_key, count(*) from bronze._quality_metrics group by run_key").fetchall())
    checks.check("this run, from its snapshot, with all its metrics", ["snapshot", len(snap["metrics"])],
                 [runs.get(snap["run_key"], [None] * 3)[2], count.get(snap["run_key"])])

    repo, tok = qh.repository(), qh.token()
    if not (repo and tok):
        checks.check("GitHub token (CI runs of main not checked)", True, False)
        return
    gh = qh.GitHub(repo, tok)
    listed = gh.json(f"actions/workflows/{qh.WORKFLOW}/runs?branch=main&event=push&status=completed&per_page=100")
    expected = sorted(f"ci-{r['id']}" for r in listed["workflow_runs"])
    checks.check("every finished CI run of main", [], [k for k in expected if k not in runs])

    rebuilt = [k for k, r in runs.items() if r[2] == "reports"]
    checks.check("runs from before the snapshot rebuilt from their reports", True, len(rebuilt) > 0)
    areas = defaultdict(set)
    for key, area in con.execute("select distinct run_key, area from bronze._quality_metrics").fetchall():
        areas[key].add(area)
    checks.check("rebuilt runs have no lakehouse metric", [],
                 [k for k in rebuilt if areas[k] & LAKEHOUSE_AREAS])

    # The oldest and the latest rebuilt runs against their reports artifacts,
    # downloaded again (the oldest validated base FHIR, with warnings).
    if not rebuilt:
        return
    by_id = sorted(rebuilt, key=lambda k: runs[k][3])
    for key in dict.fromkeys([by_id[0], by_id[-1]]):
        check_rebuilt(checks, con, gh, key, runs[key][3])


def check_rebuilt(checks, con, gh, key, run_id):
    artifacts = gh.json(f"actions/runs/{run_id}/artifacts")["artifacts"]
    z = zipfile.ZipFile(io.BytesIO(gh.download(next(a["id"] for a in artifacts if a["name"] == "reports"))))
    names = set(z.namelist())
    stored = {(m, d): val for m, d, val in con.execute(
        "select metric, dimension, value from bronze._quality_metrics where run_key = ?", [key]).fetchall()}
    expected, actual = {}, {}
    for path, suite in qh.API_CHECKS.items():
        if path in names:
            expected[f"checks {suite}"] = float(json.loads(z.read(path))["passed"])
            actual[f"checks {suite}"] = stored.get(("api_checks_passed", suite))
    for path, ig in qh.VALIDATIONS.items():
        if path in names:
            rows = json.loads(z.read(path))["by_resource"].values()
            for k in ("validated", "errors", "warnings"):
                expected[f"{k} {ig}"] = float(sum(r[k] for r in rows))
                actual[f"{k} {ig}"] = stored.get((f"validation_{k}", ig))
    results = next((json.loads(z.read(p)) for p in ("data/dbt_run_results.json", "dbt/target/run_results.json")
                    if p in names), None)
    if results and not results.get("args", {}).get("select"):
        expected["dbt tests passing"] = float(sum(1 for r in results["results"]
                                                  if r["unique_id"].startswith("test.") and r["status"] == "pass"))
        actual["dbt tests passing"] = stored.get(("dbt_tests", "pass"))
    checks.check(f"{key} equal to its reports artifact", expected, actual, passed=expected and expected == actual)


def check_gold(checks, con):
    checks.group = "gold"
    bronze = con.execute("select count(*) from bronze._quality_metrics").fetchone()[0]
    gold = con.execute("select count(*) from gold.quality_history").fetchone()[0]
    checks.check("every metric in gold.quality_history", bronze, gold)
    runs = con.execute("select count(*) from bronze._quality_runs").fetchone()[0]
    checks.check("every run in gold.quality_runs", runs, con.execute("select count(*) from gold.quality_runs").fetchone()[0])

    # The two latest runs of main, ordered here from bronze.
    main = [k for (k,) in con.execute(
        "select run_key from bronze._quality_runs where source = 'ci' and branch = 'main' and scenario = 'base' "
        "order by run_at desc limit 2").fetchall()]
    if len(main) < 2:
        checks.check("two runs of main (previous value not checked)", True, False)
        return
    pairs = con.execute("""
        select m.metric, m.dimension, m.value, p.value
        from bronze._quality_metrics m
        left join bronze._quality_metrics p
          on p.run_key = ? and p.area = m.area and p.metric = m.metric and p.dimension = m.dimension
        where m.run_key = ? order by 1, 2""", [main[1], main[0]]).fetchall()
    in_gold = {(r[0], r[1]): (r[2], r[3], r[4]) for r in con.execute(
        "select metric, dimension, previous_run_key, previous_value, change from gold.quality_history "
        "where run_key = ?", [main[0]]).fetchall()}
    wrong = [(m, d) for m, d, val, prev in pairs
             if in_gold.get((m, d), (None,))[0] != main[1]
             or in_gold[(m, d)][1] != prev
             or (prev is not None and val is not None and not close(in_gold[(m, d)][2], val - prev))]
    checks.check(f"previous values of {main[0]} are {main[1]}'s", [], wrong[:10])


APP_TEST = """
import json, sys
from streamlit.testing.v1 import AppTest
at = AppTest.from_file("app.py", default_timeout=90).run()
out = {"exceptions": [e.message for e in at.exception], "series": list(at.selectbox[0].options),
       "selected": at.selectbox[0].value, "metrics": {m.label: m.value for m in at.metric}}
if len(sys.argv) > 1:
    at.selectbox[0].set_value(sys.argv[1]).run()
    out["other"] = {"exceptions": [e.message for e in at.exception], "metrics": {m.label: m.value for m in at.metric}}
print("APPTEST" + json.dumps(out))
"""


def app_test(series=None):
    cmd = ["docker", "exec", CONTAINER, "python", "-c", APP_TEST] + ([series] if series else [])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    line = next((l for l in out.splitlines() if l.startswith("APPTEST")), None)
    return json.loads(line.removeprefix("APPTEST")) if line else None


def expected_headline(con, run_key):
    """The headline numbers the dashboard should show for a run, from gold."""
    def one(sql):
        r = con.execute(sql, [run_key]).fetchone()
        return None if r is None or r[0] is None else float(r[0])
    failed = one("select sum(value) filter (where metric = 'api_checks_total') - "
                 "sum(value) filter (where metric = 'api_checks_passed') from gold.quality_history "
                 "where run_key = ? and area = 'fhir'")
    errors = one("select sum(value) from gold.quality_history where run_key = ? and metric = 'validation_errors'")
    recall = one("select value from gold.quality_history where run_key = ? and metric = 'match_recall' "
                 "and dimension = 'splink_reviewed'")
    return {"API checks failed": "—" if failed is None else f"{failed:.0f}",
            "HL7 validation errors": "—" if errors is None else f"{errors:.0f}",
            "Linkage recall (Splink, reviewed)": "—" if recall is None else f"{recall:.4f}"}


def check_dashboard(checks, con, snap):
    checks.group = "dashboard"
    try:
        with urllib.request.urlopen(f"{DASHBOARD}/_stcore/health", timeout=5) as r:
            health = r.read().decode()
    except OSError as e:
        health = str(e)
    checks.check("health check", "ok", health)

    series = con.execute("select source || ' · ' || coalesce(branch, '?') || ' · ' || scenario "
                         "from gold.quality_runs where run_key = ?", [snap["run_key"]]).fetchone()[0]
    result = app_test(series)
    if not checks.check("the app runs headless", True, result is not None):
        return
    checks.check("no error on the page (main)", [], result["exceptions"])
    checks.check("no error on the page (this run's series)", [], result["other"]["exceptions"])
    every = [s for (s,) in con.execute("select distinct source || ' · ' || coalesce(branch, '?') || ' · ' || scenario "
                                       "from gold.quality_runs order by 1").fetchall()]
    checks.check("every series offered", every, sorted(result["series"]))

    latest_main = con.execute("select run_key from gold.quality_runs where source = 'ci' and branch = 'main' "
                              "and scenario = 'base' order by run_at desc limit 1").fetchone()
    if latest_main:
        checks.check("main selected first", "ci · main · base", result["selected"])
        expected = expected_headline(con, latest_main[0])
        checks.check(f"headline numbers of {latest_main[0]}", expected,
                     {k: result["metrics"].get(k) for k in expected})
    latest_here = con.execute("select run_key from gold.quality_runs where source || ' · ' || coalesce(branch, '?') "
                              "|| ' · ' || scenario = ? order by run_at desc limit 1", [series]).fetchone()[0]
    expected = expected_headline(con, latest_here)
    checks.check(f"headline numbers of {latest_here}", expected,
                 {k: result["other"]["metrics"].get(k) for k in expected})


def main():
    checks = Checks()
    snap = json.loads(qh.SNAPSHOT.read_text())
    con = lakehouse.connect(read_only=True)
    try:
        check_snapshot(checks, con, snap)
        check_history(checks, con, snap)
        check_gold(checks, con)
        check_dashboard(checks, con, snap)
    finally:
        con.close()

    by_group = defaultdict(lambda: [0, 0])
    for r in checks.results:
        by_group[r["group"]][0] += r["passed"]
        by_group[r["group"]][1] += 1
    report = {"run_key": snap["run_key"], "passed": sum(r["passed"] for r in checks.results),
              "total": len(checks.results),
              "groups": {g: {"passed": p, "total": t} for g, (p, t) in by_group.items()},
              "failed": checks.failed, "checks": checks.results}
    REPORT.write_text(json.dumps(report, indent=2, default=str))
    for g, (p, t) in by_group.items():
        print(f"{g:12} {p:>4}/{t}")
    print(f"{report['passed']}/{report['total']} checks passed. Written to {REPORT.relative_to(ROOT)}")
    for r in checks.failed[:30]:
        print(f"  FAILED {r['group']} {r['check']}: expected {str(r['expected'])[:200]}, got {str(r['actual'])[:200]}")
    if checks.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
