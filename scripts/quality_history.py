"""Data quality history: one snapshot of the quality metrics per pipeline run,
and the history of those snapshots in the lakehouse.

    snapshot   Read this run's metrics and write data/quality_snapshot.json, with a
               copy under data/quality_history/local/. The metrics come from the
               reports the checks write in data/ (SQL on FHIR conformance, HL7
               validation, API checks, CDC reconciliation, dbt test results) and
               from the quality marts in gold (load reliability, CDC latency,
               linkage quality, data quality issues, volumes).
    collect    Gather every snapshot and write them to bronze._quality_runs and
               bronze._quality_metrics (replaced each time), which
               gold.quality_runs and gold.quality_history read. The snapshots:
               - the CI runs of main, from GitHub: their quality-snapshot
                 artifact, or, for runs from before it, the metrics their reports
                 artifact holds (the reports only; the lakehouse of those runs is
                 gone, so their load, CDC latency and linkage metrics are missing);
               - the runs of the scale benchmark workflow (scale.yml): one
                 snapshot per scale factor, in their scale-results-<factor>x
                 artifacts (one job per factor; older runs: one scale-results);
               - the local copies under data/quality_history/local/;
               - this run's data/quality_snapshot.json and the scale snapshots
                 in data/scale/ (a CI run is still going when it collects, so
                 GitHub has no artifact of it yet).
               Downloaded snapshots are kept under data/quality_history/ci/ and
               not downloaded again. Without a GitHub token, only those, the
               local copies and this run's are collected.

A snapshot is one run: when, which commit, which scenario (DQ_SCENARIO, `base`
by default: the generators' default volumes; the scale benchmark's are 1x, 10x,
30x), and a list of metrics, each one (area, metric, dimension, value). The areas:

    load          bronze load steps (gold.load_reliability)
    cdc           CDC latency, the replication slot, reconciliation with Postgres
    fhir          SQL on FHIR conformance, HL7 validation, API checks, view parity,
                  terminology
    linkage       identity linkage and deduplication quality against ground truth
    data_quality  the legacy issues the lakehouse detects (gold.dq_issue_summary)
    tests         dbt build results
    performance   durations: dbt build, bulk exports, the CI run
    volume        rows in the lakehouse, resources published
    scale         the scale benchmark: time, memory and disk per step, Splink
                  candidate pairs per blocking rule (scripts/scale_benchmark.py)

The GitHub token comes from GITHUB_TOKEN or GH_TOKEN, or `gh auth token`; the
repository from GITHUB_REPOSITORY or the origin remote.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingestion"))

DATA = ROOT / "data"
SNAPSHOT = DATA / "quality_snapshot.json"
HISTORY = DATA / "quality_history"
LOCAL_DIR = HISTORY / "local"
CI_DIR = HISTORY / "ci"
SCALE_DIR = DATA / "scale"
WORKFLOW = "ci.yml"
SCALE_WORKFLOW = "scale.yml"
SNAPSHOT_ARTIFACT = "quality-snapshot"
REPORTS_ARTIFACT = "reports"
SCALE_ARTIFACT = "scale-results"
API = "https://api.github.com"

# HL7 validator summaries, by the directory the run writes them to. The first
# runs had one validation only, of base FHIR R4, in data/fhir_validation/.
VALIDATIONS = {
    "data/fhir_validation/summary.json": "base",
    "data/fhir_validation_carin-bb/summary.json": "carin-bb",
    "data/fhir_validation_pdex/summary.json": "pdex",
    "data/fhir_validation_provider-access/summary.json": "provider-access",
    "data/fhir_validation_payer-to-payer/summary.json": "payer-to-payer",
    "data/fhir_validation_prior-auth/summary.json": "prior-auth",
}
API_CHECKS = {
    "data/fhir_api.json": "patient-access",
    "data/fhir_provider_api.json": "provider-access",
    "data/fhir_security.json": "security",
    "data/fhir_prior_auth.json": "prior-auth",
}
PUBLISHED = {
    "data/fhir_publish.json": "patient-access",
    "data/fhir_publish_provider.json": "provider-access",
}
# Row counts that describe the run's volume.
VOLUME = {
    "legacy_members": "silver.stg_legacy__members",
    "legacy_claims": "gold.fct_claims",
    "legacy_claim_lines": "silver.stg_legacy__claim_lines",
    "legacy_prior_auths": "silver.stg_legacy__prior_auths",
    "synthea_patients": "silver.stg_fhir__patients",
    "pbm_members": "silver.stg_pbm__members",
    "pbm_claims": "gold.fct_pbm_claims",
    "fhir_patients": "gold.fhir_patient",
    "fhir_explanations_of_benefit": "gold.fhir_explanation_of_benefit",
}


def metric(area, name, value, dimension=""):
    return {"area": area, "metric": name, "dimension": dimension,
            "value": None if value is None else float(value)}


# --- Metrics from the reports -------------------------------------------------

def from_reports(read) -> list[dict]:
    """The metrics the CI reports hold. `read(path)` returns the parsed JSON of a
    report (path relative to the repository, as in the reports artifact) or None."""
    out = []

    sof = read("data/sof_conformance.json")
    if sof:
        for tag in ("shareable", "experimental"):
            if tag in sof:
                out.append(metric("fhir", "sof_tests_passed", sof[tag]["passed"], tag))
                out.append(metric("fhir", "sof_tests_total", sof[tag]["total"], tag))

    for path, name in VALIDATIONS.items():
        summary = read(path)
        if not summary:
            continue
        rows = summary.get("by_resource", {}).values()
        for key in ("validated", "errors", "warnings", "resources_with_errors", "terminology_unverified"):
            if any(key in r for r in rows):
                out.append(metric("fhir", f"validation_{key}", sum(r.get(key, 0) for r in rows), name))

    for path, name in API_CHECKS.items():
        report = read(path)
        if report and "passed" in report:
            out.append(metric("fhir", "api_checks_passed", report["passed"], name))
            out.append(metric("fhir", "api_checks_total", report["total"], name))

    api = read("data/fhir_api.json")
    if api and "bulk_export" in api:
        out.append(metric("performance", "bulk_export_seconds", api["bulk_export"]["seconds"], "patient-access"))
    provider = read("data/fhir_provider_api.json")
    if provider and "payer_to_payer" in provider:
        out.append(metric("performance", "bulk_export_seconds",
                          provider["payer_to_payer"]["export_seconds"], "payer-to-payer"))

    for path, api_name in PUBLISHED.items():
        report = read(path)
        if not report:
            continue
        counts = {}
        for load in report.get("loads", []):
            counts[load["resources"]] = counts.get(load["resources"], 0) + load["count"]
        for resource, count in sorted(counts.items()):
            out.append(metric("volume", "fhir_resources_published", count, f"{api_name}:{resource}"))

    reconcile = read("data/cdc_reconcile.json")
    if reconcile:
        out.append(metric("cdc", "reconcile_source_rows", sum(t["source_rows"] for t in reconcile)))
        out.append(metric("cdc", "reconcile_mismatched_rows",
                          sum(t["only_in_source"] + t["only_in_lakehouse"] for t in reconcile)))
        for t in reconcile:
            out.append(metric("cdc", "reconcile_mismatched_rows",
                              t["only_in_source"] + t["only_in_lakehouse"], t["table"]))

    drill = read("data/slot_drill.json")
    if drill:
        out.append(metric("cdc", "slot_drill_passed", drill.get("result") == "passed"))
        if drill.get("alert"):
            out.append(metric("cdc", "slot_drill_alert_retained_mb", drill["alert"]["retained_mb"]))

    # The full build's results. Before they were kept apart, the artifact had
    # only dbt/target/run_results.json: the full build's until cdc-check came,
    # then cdc-check's selection, which is left out.
    results = read("data/dbt_run_results.json")
    if not results:
        results = read("dbt/target/run_results.json")
        if results and results.get("args", {}).get("select"):
            results = None
    if results:
        counts = {}
        for r in results["results"]:
            kind = r["unique_id"].split(".")[0]
            counts[(kind, r["status"])] = counts.get((kind, r["status"]), 0) + 1
        for status in ("pass", "warn", "fail", "error"):
            out.append(metric("tests", "dbt_tests", counts.get(("test", status), 0), status))
        out.append(metric("tests", "dbt_models", sum(v for (k, _), v in counts.items() if k == "model")))
        out.append(metric("performance", "dbt_build_seconds", results.get("elapsed_time")))
    return out


def read_dir(root: Path):
    def read(path):
        f = root / path
        return json.loads(f.read_text()) if f.is_file() else None
    return read


def read_zip(data: bytes):
    """Reads members of an artifact by name, in memory: nothing is extracted."""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())

    def read(path):
        return json.loads(z.read(path)) if path in names else None
    return read


# --- Metrics from the lakehouse -----------------------------------------------

def rows(con, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def from_lakehouse(con) -> list[dict]:
    out = []

    loads = rows(con, "select * from gold.load_reliability order by source")
    for r in loads:
        out.append(metric("load", "load_runs", r["runs"], r["source"]))
        out.append(metric("load", "load_failed_runs", r["runs"] - r["successful_runs"], r["source"]))
        out.append(metric("load", "load_success_rate_pct", r["success_rate_pct"], r["source"]))
        out.append(metric("load", "load_avg_seconds", r["avg_seconds"], r["source"]))
    if loads:
        runs = sum(r["runs"] for r in loads)
        ok = sum(r["successful_runs"] for r in loads)
        out.append(metric("load", "load_runs", runs))
        out.append(metric("load", "load_failed_runs", runs - ok))
        out.append(metric("load", "load_success_rate_pct", round(100.0 * ok / runs, 1)))
        out.append(metric("load", "load_rows", sum(r["total_rows"] or 0 for r in loads)))

    latency = rows(con, "select * from gold.cdc_latency order by source_table")
    for r in latency:
        out.append(metric("cdc", "cdc_changes", r["changes"], r["source_table"]))
        for q in ("p50", "p95", "max"):
            out.append(metric("cdc", f"cdc_{q}_latency_seconds", r[f"{q}_latency_seconds"], r["source_table"]))
    if latency:
        out.append(metric("cdc", "cdc_changes", sum(r["changes"] for r in latency)))
        # Quantiles do not add up across tables: the slowest table's.
        out.append(metric("cdc", "cdc_p95_latency_seconds", max(r["p95_latency_seconds"] for r in latency)))
        out.append(metric("cdc", "cdc_max_latency_seconds", max(r["max_latency_seconds"] for r in latency)))
    for r in rows(con, "select * from gold.cdc_slot_lag"):
        out.append(metric("cdc", "slot_losses", r["slot_losses"], r["slot_name"]))
        out.append(metric("cdc", "slot_max_pending_mb", r["max_pending_mb"], r["slot_name"]))

    for r in rows(con, "select * from gold.identity_match_quality where scope = 'all' order by method"):
        out.append(metric("linkage", "match_precision", r["precision"], r["method"]))
        out.append(metric("linkage", "match_recall", r["recall"], r["method"]))
        out.append(metric("linkage", "match_linked_wrong", r["linked_wrong"], r["method"]))
        out.append(metric("linkage", "match_in_review", r["in_review"], r["method"]))
    for r in rows(con, "select * from gold.identity_dedupe_quality order by method"):
        out.append(metric("linkage", "dedupe_precision", r["precision"], r["method"]))
        out.append(metric("linkage", "dedupe_recall", r["recall"], r["method"]))
        out.append(metric("linkage", "dedupe_in_review", r["in_review"], r["method"]))

    for r in rows(con, "select * from gold.dq_issue_summary order by check_name"):
        out.append(metric("data_quality", "issue_count", r["issue_count"], r["check_name"]))
        rate = 100.0 * r["issue_count"] / r["population"] if r["population"] else None
        out.append(metric("data_quality", "issue_rate_pct", None if rate is None else round(rate, 2), r["check_name"]))

    parity = rows(con, "select * from gold.fhir_view_parity order by comparison")
    for r in parity:
        out.append(metric("fhir", "view_parity_mismatched_rows", r["only_in_view"] + r["only_in_reference"], r["comparison"]))
    if parity:
        out.append(metric("fhir", "view_parity_mismatched_rows",
                          sum(r["only_in_view"] + r["only_in_reference"] for r in parity)))
    for r in rows(con, "select status, sum(occurrences) as n from gold.terminology_code_check group by status order by status"):
        out.append(metric("fhir", "terminology_codes", r["n"], r["status"]))

    for name, table in VOLUME.items():
        out.append(metric("volume", name, con.execute(f"select count(*) from {table}").fetchone()[0]))
    return out


# --- Snapshot ----------------------------------------------------------------

def git(*args) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def make_record(metrics: list[dict], scenario: str | None = None, key_suffix: str = "") -> dict:
    """A snapshot of this run: its metrics and where they come from (the CI run,
    or this machine's branch and commit)."""
    now = datetime.now(timezone.utc)
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    run_id = os.environ.get("GITHUB_RUN_ID") if in_ci else None
    return {
        "run_key": (f"ci-{run_id}" if run_id else f"local-{now:%Y%m%dT%H%M%SZ}") + key_suffix,
        "run_at": now.isoformat(timespec="seconds"),
        # In a pull request, the checkout is the merge of the branch into main.
        "commit": os.environ.get("DQ_COMMIT") or git("rev-parse", "HEAD"),
        "branch": (os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")) if in_ci
                  else git("rev-parse", "--abbrev-ref", "HEAD"),
        "scenario": scenario or os.environ.get("DQ_SCENARIO", "base"),
        "source": "ci" if in_ci else "local",
        "ci_run_id": int(run_id) if run_id else None,
        "ci_event": os.environ.get("GITHUB_EVENT_NAME") if in_ci else None,
        "conclusion": None,
        "duration_seconds": None,
        "metrics_from": "snapshot",
        "metrics": metrics,
    }


def write_snapshot(record: dict, path: Path) -> None:
    """Writes the snapshot, and outside CI a copy under data/quality_history/local/."""
    text = json.dumps(record, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if record["source"] == "local":
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        (LOCAL_DIR / f"{record['run_key']}.json").write_text(text)
    areas = sorted({m["area"] for m in record["metrics"]})
    print(f"{record['run_key']}: {len(record['metrics'])} metrics ({', '.join(areas)}) -> {path.relative_to(ROOT)}")


def snapshot() -> dict:
    import lakehouse
    con = lakehouse.connect(read_only=True)
    record = make_record(from_reports(read_dir(ROOT)) + from_lakehouse(con))
    write_snapshot(record, SNAPSHOT)
    return record


# --- GitHub -------------------------------------------------------------------

def token() -> str | None:
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def repository() -> str | None:
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    url = git("remote", "get-url", "origin") or ""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHub:
    def __init__(self, repo: str, tok: str):
        self.repo, self.tok = repo, tok
        self.opener = urllib.request.build_opener(NoRedirect)

    def _get(self, url: str) -> urllib.request.addinfourl:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.tok}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "quality-history"})
        try:
            return self.opener.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e
            raise

    def json(self, path: str) -> dict:
        with self._get(f"{API}/repos/{self.repo}/{path}") as r:
            return json.load(r)

    def download(self, artifact_id: int) -> bytes:
        """The artifact's zip. GitHub answers with a redirect to a signed URL,
        which is fetched without the token."""
        r = self._get(f"{API}/repos/{self.repo}/actions/artifacts/{artifact_id}/zip")
        location = r.headers.get("Location")
        r.close()
        if not location:
            raise RuntimeError(f"artifact {artifact_id}: no download location")
        with urllib.request.urlopen(location, timeout=120) as blob:
            return blob.read()

    def runs(self, workflow: str, query: str = "") -> list[dict]:
        """The finished runs of a workflow; none if GitHub does not know it yet."""
        runs, page = [], 1
        while True:
            try:
                batch = self.json(f"actions/workflows/{workflow}/runs?status=completed{query}"
                                  f"&per_page=100&page={page}")["workflow_runs"]
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return runs
                raise
            runs += batch
            if len(batch) < 100:
                return runs
            page += 1

    def main_runs(self) -> list[dict]:
        return self.runs(WORKFLOW, "&branch=main&event=push")

    def scale_runs(self) -> list[dict]:
        """Every run of the scale benchmark, on any branch: they are few, and the
        branch is part of each series."""
        return self.runs(SCALE_WORKFLOW)


def artifacts(gh: GitHub, run: dict) -> dict:
    return {a["name"]: a for a in gh.json(f"actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
            if not a["expired"]}


def run_meta(run: dict) -> dict:
    started = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
    return {
        "run_key": f"ci-{run['id']}",
        "commit": run["head_sha"],
        "branch": run["head_branch"],
        "source": "ci",
        "ci_run_id": run["id"],
        "ci_event": run["event"],
        "conclusion": run["conclusion"],
        "duration_seconds": (ended - started).total_seconds(),
    }


def ci_record(gh: GitHub, run: dict) -> dict | None:
    """The snapshot of a finished CI run of main: its quality-snapshot artifact,
    or one made from its reports artifact."""
    found = artifacts(gh, run)
    meta = run_meta(run)
    started = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
    if SNAPSHOT_ARTIFACT in found:
        read = read_zip(gh.download(found[SNAPSHOT_ARTIFACT]["id"]))
        record = read("data/quality_snapshot.json") or read("quality_snapshot.json")
        if record:
            record.update(meta)
            return record
    if REPORTS_ARTIFACT not in found:
        return None
    metrics = from_reports(read_zip(gh.download(found[REPORTS_ARTIFACT]["id"])))
    return {**meta, "run_at": started.isoformat(timespec="seconds"), "scenario": "base",
            "metrics_from": "reports", "metrics": metrics}


def scale_records(gh: GitHub, run: dict) -> list[dict]:
    """The snapshots of a run of the scale benchmark, one per scale factor (the
    scale factors it finished). Each factor's job uploads its own artifact,
    scale-results-<factor>x; runs from before the matrix have one, scale-results."""
    found = artifacts(gh, run)
    names = sorted(n for n in found if n == SCALE_ARTIFACT or n.startswith(f"{SCALE_ARTIFACT}-"))
    records = []
    for artifact in names:
        z = zipfile.ZipFile(io.BytesIO(gh.download(found[artifact]["id"])))
        for name in sorted(z.namelist()):
            if re.fullmatch(r"(?:.*/)?quality_snapshot_[^/]+\.json", name):
                record = json.loads(z.read(name))
                meta = run_meta(run)
                meta["run_key"] = f"ci-{run['id']}-{record['scenario']}"
                record.update(meta)
                records.append(record)
    return records


def snapshot_files() -> list[dict]:
    """Every snapshot on disk: the downloaded CI runs (a scale run's file holds a
    list, one per scale factor), the local copies, this run's and the scale
    benchmark's."""
    records = []
    files = sorted(CI_DIR.glob("*.json")) + sorted(LOCAL_DIR.glob("*.json")) + sorted(SCALE_DIR.glob("quality_snapshot_*.json"))
    if SNAPSHOT.is_file():
        files.append(SNAPSHOT)
    for f in files:
        content = json.loads(f.read_text())
        records += content if isinstance(content, list) else [content]
    return records


# --- Collect -------------------------------------------------------------------

RUN_COLUMNS = ["run_key", "run_at", "commit", "branch", "scenario", "source", "ci_run_id",
               "ci_event", "conclusion", "duration_seconds", "metrics_from"]


def collect(offline: bool = False) -> list[dict]:
    import common
    records = {}
    CI_DIR.mkdir(parents=True, exist_ok=True)

    repo, tok = repository(), None if offline else token()
    if repo and tok:
        gh = GitHub(repo, tok)
        runs = gh.main_runs()
        new = 0
        for run in runs:
            cached = CI_DIR / f"{run['id']}.json"
            if cached.is_file():
                continue
            record = ci_record(gh, run)
            if record:
                cached.write_text(json.dumps(record, indent=2) + "\n")
                new += 1
        print(f"GitHub {repo}: {len(runs)} finished CI runs of main, {new} downloaded now")
        scale_runs, new = gh.scale_runs(), 0
        for run in scale_runs:
            cached = CI_DIR / f"scale-{run['id']}.json"
            if cached.is_file():
                continue
            found = scale_records(gh, run)
            if found:
                cached.write_text(json.dumps(found, indent=2) + "\n")
                new += 1
        print(f"GitHub {repo}: {len(scale_runs)} finished scale benchmark runs, {new} downloaded now")
    else:
        print("No GitHub token or repository: collecting the snapshots already downloaded")

    # Later files win: this run's snapshots over copies of them.
    for r in snapshot_files():
        records[r["run_key"]] = r

    con = common.connect()
    con.execute("BEGIN")
    con.execute("DELETE FROM bronze._quality_metrics")
    con.execute("DELETE FROM bronze._quality_runs")
    for r in records.values():
        con.execute(f"INSERT INTO bronze._quality_runs VALUES ({', '.join('?' * len(RUN_COLUMNS))})",
                    [r.get(c) for c in RUN_COLUMNS])
        if r["metrics"]:
            con.executemany("INSERT INTO bronze._quality_metrics VALUES (?, ?, ?, ?, ?)",
                            [[r["run_key"], m["area"], m["metric"], m["dimension"], m["value"]] for m in r["metrics"]])
    con.execute("COMMIT")
    by_source = {}
    for r in records.values():
        key = f"{r['source']} ({r['metrics_from']})"
        by_source[key] = by_source.get(key, 0) + 1
    print(f"{len(records)} runs in bronze._quality_runs: "
          + ", ".join(f"{n} {k}" for k, n in sorted(by_source.items())))
    return list(records.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot")
    c = sub.add_parser("collect")
    c.add_argument("--offline", action="store_true", help="do not ask GitHub")
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot()
    else:
        collect(args.offline)


if __name__ == "__main__":
    main()
