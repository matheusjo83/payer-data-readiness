"""Scale benchmark: the data pipeline at the base volume and at multiples of it.

For each scale factor, from an empty legacy database and lakehouse:

    seed                  the legacy generator at factor x its default volumes
                          (members, providers, claims, prior authorizations)
    pbm                   the PBM feed and its ground truth (they follow the members)
    cdc_initial_load      the first CDC sync: replication slot and snapshot into bronze
    pbm_load              the PBM feed into bronze
    fhir_terminology_load Synthea's FHIR data and the code systems into bronze
                          (Synthea stays at its base population: it measures
                          conformance, not volume)
    dbt_build             every silver and gold model and test, the Splink models included
    blocking_analysis     the candidate pairs each Splink blocking rule adds (exact counts)
    change_batch          simulated activity in Postgres: about 1% of the rows
                          (one event per 100 members, claims and prior authorizations)
    cdc_incremental_sync  the next CDC sync: only those changes
    dbt_incremental       the incremental current state of the legacy tables (as make cdc-check)
    dbt_rebuild           every model and test again, after the changes
    cdc_reconcile         the current state against Postgres, row by row

Each step is timed and its peak memory recorded (the largest resident set of
the step's processes, from the kernel's accounting of the process tree), while
docker stats samples the legacy database and the lakehouse catalog. After the
steps, the sizes on disk: the Parquet files, the catalog, the legacy database
and its WAL.

The identity quality gates (assert_identity_match_quality and
assert_identity_dedupe_quality: no wrong link or merge, recall at least 95%)
hold the linkage to what it achieves on the base data; at other volumes their
failure is a result, recorded, and the benchmark goes on. Any other dbt failure
stops it.

The reviewer decisions (seed identity_review_decisions) are off at every scale,
the base one included: they name cardholders and members of the base data, which
at another volume are other people. The linkage is measured on the model's own
decisions.

Writes, per scale factor (scenario `<factor>x`), under data/scale/:

    <scenario>/benchmark.json          steps, resources, sizes, candidate pairs
    <scenario>/dbt_run_results.json    the first dbt build's results
    <scenario>/logs/<step>.log         each step's output
    quality_snapshot_<scenario>.json   the quality snapshot of the run, with
                                       the benchmark's metrics in area `scale`

and data/scale/summary.json with every scenario benchmarked so far.

Memory: dbt runs with its own profile (data/scale/profiles/), the project's
one plus a DuckDB memory limit (by default 70% of the machine's memory, option
--memory-limit), 4 DuckDB threads (--duckdb-threads) and a spill directory,
data/scale/tmp/: DuckDB's default, 80%,
leaves too little for the containers and the rest of the dbt process at 30x
(on a 16 GB machine the build used 10 GB at 10x). A watchdog stops a step,
and the benchmark, when the memory still available falls under 8% of the
machine's (or 1 GB): the benchmark reports it instead of taking the machine
down. A local run
also keeps a copy of each snapshot in data/quality_history/local/, so make
dq-history adds it to the history.

The benchmark deletes the legacy database and the lakehouse (not the other
data under data/): run make reset and the pipeline to return to the base data.

Usage (from the repository root, after make synthea):
    python scripts/scale_benchmark.py 10
    python scripts/scale_benchmark.py 1 10 30
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingestion"))
sys.path.insert(0, str(ROOT / "scripts"))

import quality_history as qh  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "scale"
PY = sys.executable
DBT = str(Path(PY).parent / "dbt") if (Path(PY).parent / "dbt").exists() else (shutil.which("dbt") or "dbt")
# The generator's defaults (legacy_db/seed/generate_legacy_data.py), scaled.
BASE_VOLUMES = {"members": 5000, "providers": 300, "claims": 25000, "prior-auths": 3000}
CONTAINERS = {"legacy-db": "payer-legacy-db", "lakehouse-catalog": "payer-lakehouse-catalog"}
DBT_VARS = "{identity_apply_review_decisions: false}"
SPLINK_MODELS = ["identity_matches_splink", "identity_pbm_duplicates_splink"]
# The identity quality gates hold the linkage to what it achieves on the base
# data (no wrong link or merge, recall at least 95%). At other volumes they are
# what the benchmark measures, not a reason to stop: their failures are recorded
# (scale metric quality_gate_failed). Any other failure stops the benchmark.
IDENTITY_GATES = {"assert_identity_match_quality", "assert_identity_dedupe_quality"}
PROFILES = OUT / "profiles"
SPILL = OUT / "tmp"
TOTAL_MEMORY = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
# The watchdog stops a step below this much available memory.
MIN_AVAILABLE = max(TOTAL_MEMORY * 0.08, 1024 ** 3)


def write_profile(memory_fraction: float, duckdb_threads: int) -> None:
    """The project's dbt profile with a DuckDB memory limit, thread count and spill directory."""
    import yaml
    profile = yaml.safe_load((ROOT / "dbt" / "profiles.yml").read_text())
    for output in profile["payer_data_readiness"]["outputs"].values():
        # As connection config, set once: dbt-duckdb applies `settings` on every
        # cursor, and DuckDB refuses a new temp_directory once it has spilled.
        output.setdefault("config_options", {}).update({
            "memory_limit": f"{int(TOTAL_MEMORY * memory_fraction / 1024 ** 2)}MB",
            "temp_directory": str(SPILL),
            "preserve_insertion_order": False,
            # DuckDB's own threads (dbt's threads are how many models run at once).
            # Each thread writing Parquet holds a row group: with 20 of them, a
            # model over the 30x EOB JSON ran out of memory on its own.
            "threads": duckdb_threads,
        })
    PROFILES.mkdir(parents=True, exist_ok=True)
    (PROFILES / "profiles.yml").write_text(yaml.safe_dump(profile, sort_keys=False))


def available_memory() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return TOTAL_MEMORY


def scenario_name(factor: int) -> str:
    return f"{factor}x"


# --- Running and measuring a step ----------------------------------------------

class DockerSampler:
    """Samples memory and CPU of the containers with docker stats, by step."""

    def __init__(self):
        self.step = None
        self.samples = {}   # step -> container -> [(mem_mb, cpu_pct)]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @staticmethod
    def _mb(text: str) -> float:
        value = text.strip()
        for unit, factor in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("GB", 1000 / 1.048576),
                             ("MB", 1 / 1.048576), ("kB", 1 / 1048.576), ("B", 1 / 1048576)):
            if value.endswith(unit):
                return float(value[: -len(unit)]) * factor
        return float(value)

    def _run(self):
        names = {v: k for k, v in CONTAINERS.items()}
        while not self._stop.is_set():
            step = self.step
            try:
                out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names],
                                     capture_output=True, text=True, timeout=30).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            for line in out.splitlines():
                try:
                    s = json.loads(line)
                    mem = self._mb(s["MemUsage"].split("/")[0])
                    cpu = float(s["CPUPerc"].rstrip("%"))
                except (ValueError, KeyError):
                    continue
                if step and s.get("Name") in names:
                    self.samples.setdefault(step, {}).setdefault(names[s["Name"]], []).append((mem, cpu))
            self._stop.wait(1.0)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=60)

    def summary(self, step: str) -> dict:
        out = {}
        for container, samples in self.samples.get(step, {}).items():
            out[container] = {"peak_mem_mb": round(max(m for m, _ in samples), 1),
                              "mean_cpu_pct": round(sum(c for _, c in samples) / len(samples), 1),
                              "samples": len(samples)}
        return out


def dbt_failures() -> list[str]:
    """The nodes that failed in the last dbt command, from its run results."""
    results = json.loads((ROOT / "dbt" / "target" / "run_results.json").read_text())["results"]
    return sorted(r["unique_id"] for r in results if r["status"] in ("error", "fail", "runtime error"))


def only_identity_gates(failures: list[str]) -> bool:
    return all(f.startswith("test.") and f.split(".")[-1] in IDENTITY_GATES for f in failures)


def run_step(name: str, cmd: list[str], logs: Path, sampler: DockerSampler, cwd: Path = ROOT,
             dbt: bool = False) -> dict:
    """Runs one step; its time, exit status and peak memory. The peak comes from
    wait4: the kernel's maximum resident set of the process and of every
    descendant it waited for (dbt's Python models run inside dbt itself)."""
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{name}.log"
    sampler.step = name
    print(f"  {name}: {' '.join(cmd)}", flush=True)
    start = time.monotonic()
    killed = threading.Event()
    lowest = [available_memory()]
    with log.open("w") as f:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        done = threading.Event()

        def watchdog():
            while not done.wait(0.5):
                lowest[0] = min(lowest[0], free := available_memory())
                if free < MIN_AVAILABLE:
                    killed.set()
                    try:
                        os.killpg(proc.pid, 9)
                    except ProcessLookupError:
                        pass
                    return

        guard = threading.Thread(target=watchdog, daemon=True)
        guard.start()
        _, status, usage = os.wait4(proc.pid, 0)
        done.set()
        guard.join()
        proc.returncode = os.waitstatus_to_exitcode(status)
    seconds = time.monotonic() - start
    sampler.step = None
    if killed.is_set():
        raise SystemExit(f"Step {name} stopped after {seconds:,.0f} s: available memory fell under "
                         f"{MIN_AVAILABLE / 1024 ** 3:.1f} GB (peak RSS {usage.ru_maxrss / 1024:,.0f} MB). "
                         f"Lower --memory-limit or the scale factor; its log: {log.relative_to(ROOT)}")
    result = {"step": name, "seconds": round(seconds, 2), "exit_code": proc.returncode,
              "peak_rss_mb": round(usage.ru_maxrss / 1024, 1),   # ru_maxrss is in KiB on Linux
              "min_available_mb": round(lowest[0] / 1024 ** 2, 1)}
    print(f"    {seconds:,.1f} s, peak RSS {result['peak_rss_mb']:,.0f} MB, exit {proc.returncode}", flush=True)
    if dbt:
        result["failed"] = dbt_failures()
        if proc.returncode != 0 and result["failed"] and only_identity_gates(result["failed"]):
            print(f"    identity quality gates failed (recorded): {', '.join(result['failed'])}", flush=True)
            return result
    if proc.returncode != 0:
        print("".join(log.read_text().splitlines(keepends=True)[-60:]))
        raise SystemExit(f"Step {name} failed (exit {proc.returncode}); its log: {log.relative_to(ROOT)}")
    return result


# --- Environment ----------------------------------------------------------------

def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)


def fresh_environment() -> None:
    """An empty legacy database and lakehouse: their containers, volumes and files."""
    project = json.loads(subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=ROOT,
                                        capture_output=True, text=True, check=True).stdout)["name"]
    compose("rm", "--stop", "--force", "--volumes", *CONTAINERS)
    subprocess.run(["docker", "volume", "rm", "--force", f"{project}_legacy_data", f"{project}_lakehouse_catalog"],
                   check=True, capture_output=True)
    for path in ("lakehouse", "pbm"):
        shutil.rmtree(DATA / path, ignore_errors=True)
    compose("up", "-d", "--build", "--wait", *CONTAINERS)


def psql(container: str, user: str, db: str, sql: str) -> str:
    return subprocess.run(["docker", "exec", CONTAINERS[container], "psql", "-U", user, "-d", db, "-Atc", sql],
                          capture_output=True, text=True, check=True).stdout.strip()


def tree_bytes(path: Path, suffix: str | None = None) -> tuple[int, int]:
    files = [f for f in path.rglob("*") if f.is_file() and (suffix is None or f.suffix == suffix)]
    return sum(f.stat().st_size for f in files), len(files)


def sizes() -> dict:
    parquet, parquet_files = tree_bytes(DATA / "lakehouse", ".parquet")
    pbm, _ = tree_bytes(DATA / "pbm")
    mb = 1024 * 1024
    return {
        "lakehouse_parquet_mb": round(parquet / mb, 1),
        "lakehouse_parquet_files": parquet_files,
        "lakehouse_catalog_mb": round(int(psql("lakehouse-catalog", "lakehouse", "lakehouse",
                                               "select pg_database_size('lakehouse')")) / mb, 1),
        "legacy_db_mb": round(int(psql("legacy-db", "legacy", "payer_legacy",
                                       "select pg_database_size('payer_legacy')")) / mb, 1),
        "legacy_wal_mb": round(int(psql("legacy-db", "legacy", "payer_legacy",
                                        "select coalesce(sum(size), 0) from pg_ls_waldir()")) / mb, 1),
        "pbm_feed_mb": round(pbm / mb, 1),
    }


def legacy_rows() -> dict:
    tables = ["mbr_mstr", "elig_span", "prv", "clm_hdr", "clm_ln", "pa_req", "mbr_cnsnt"]
    out = psql("legacy-db", "legacy", "payer_legacy",
               " union all ".join(f"select '{t}', count(*) from {t}" for t in tables))
    return {t: int(n) for t, n in (line.split("|") for line in out.splitlines())}


# --- Splink candidate pairs -------------------------------------------------------

def load_module(name: str):
    import importlib.util
    path = ROOT / "dbt" / "models" / "marts" / "identity" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rule_name(rule) -> str:
    sql = rule.get_blocking_rule("duckdb").blocking_rule_sql
    cols = []
    for part in sql.split(" AND "):
        col = part.strip().strip("()").split("=")[0].strip()
        cols.append(col.removeprefix("l.").strip('"'))
    return "+".join(cols)


def blocking_analysis() -> dict:
    """Exact counts of the pairs each rule adds to the ones before it, for the
    linkage (legacy x PBM) and the PBM dedupe, from gold.identity_people. Run as
    its own step (python -c), so its time and memory are measured apart."""
    import duckdb
    import lakehouse
    from splink import DuckDBAPI
    from splink.blocking_analysis import count_comparisons_from_blocking_rules

    people = lakehouse.connect(read_only=True).execute(
        "select * from gold.identity_people order by source_dataset, unique_id").to_arrow_table()
    con = duckdb.connect()
    con.register("people", people)
    for name, source in (("legacy_people", "legacy"), ("pbm_people", "pbm")):
        con.execute(f"CREATE TABLE {name} AS SELECT * EXCLUDE (source_dataset) FROM people "
                    f"WHERE source_dataset = '{source}' ORDER BY unique_id")
    counts = {"records": {"legacy": con.execute("select count(*) from legacy_people").fetchone()[0],
                          "pbm": con.execute("select count(*) from pbm_people").fetchone()[0]}}
    for kind, module, tables, link_type in (
        ("link", "identity_matches_splink", ["legacy_people", "pbm_people"], "link_only"),
        ("dedupe", "identity_pbm_duplicates_splink", ["pbm_people"], "dedupe_only"),
    ):
        rules = load_module(module).BLOCKING_RULES
        db_api = DuckDBAPI(connection=con)   # one per analysis: a table is registered once in each
        frames = [db_api.register(t, dataset_display_name=t.split("_")[0]) for t in tables]
        records = count_comparisons_from_blocking_rules(
            frames if len(frames) > 1 else frames[0], blocking_rules=rules, link_type=link_type,
            record_sample_proportion=1.0)
        counts[kind] = [{"rule": rule_name(rule), "marginal": int(r["marginal_comparison_count"]),
                         "cumulative": int(r["cumulative_comparison_count"]),
                         "possible": int(r["total_possible_comparison_count"])}
                        for rule, r in zip(rules, records, strict=True)]
    return counts


# --- One scale factor ---------------------------------------------------------------

def benchmark(factor: int, events_per_row: float) -> dict:
    scenario = scenario_name(factor)
    out = OUT / scenario
    shutil.rmtree(out, ignore_errors=True)
    logs = out / "logs"
    volumes = {k: v * factor for k, v in BASE_VOLUMES.items()}
    events = round(events_per_row * (volumes["members"] + volumes["claims"] + volumes["prior-auths"]))
    print(f"\n== {scenario}: {volumes}, change batch of {events} events", flush=True)

    fresh_environment()
    shutil.rmtree(SPILL, ignore_errors=True)
    sampler = DockerSampler()
    sampler.start()
    steps = []
    dbt = [DBT, "build", "--profiles-dir", str(PROFILES), "--vars", DBT_VARS]
    started = time.time()
    try:
        steps.append(run_step("seed", [PY, "legacy_db/seed/generate_legacy_data.py",
                                       *[a for k, v in volumes.items() for a in (f"--{k}", str(v))]],
                              logs, sampler))
        steps.append(run_step("pbm", [PY, "scripts/generate_pbm_feed.py"], logs, sampler))
        steps.append(run_step("cdc_initial_load", [PY, "ingestion/cdc_legacy.py", "sync"], logs, sampler))
        steps.append(run_step("pbm_load", [PY, "ingestion/load_pbm.py"], logs, sampler))
        steps.append(run_step("fhir_terminology_load", ["sh", "-c", f"{PY} ingestion/load_fhir.py && "
                                                        f"{PY} ingestion/load_terminology.py"], logs, sampler))
        steps.append(run_step("dbt_build", dbt, logs, sampler, cwd=ROOT / "dbt", dbt=True))
        shutil.copy(ROOT / "dbt" / "target" / "run_results.json", out / "dbt_run_results.json")
        pairs_file = out / "candidate_pairs.json"
        steps.append(run_step("blocking_analysis", [
            PY, "-c", "import json, sys; sys.path.insert(0, 'scripts'); import scale_benchmark as s; "
                      f"open({str(pairs_file)!r}, 'w').write(json.dumps(s.blocking_analysis(), indent=2))"],
            logs, sampler))
        steps.append(run_step("change_batch", [PY, "legacy_db/seed/simulate_changes.py", "--events", str(events),
                                               "--duration", "0", "--seed", "42"], logs, sampler))
        steps.append(run_step("cdc_incremental_sync", [PY, "ingestion/cdc_legacy.py", "sync"], logs, sampler))
        steps.append(run_step("dbt_incremental", [
            DBT, "build", "--profiles-dir", str(PROFILES), "-s", "path:models/staging/legacy/current",
            "assert_cdc_current_state_matches_full"], logs, sampler, cwd=ROOT / "dbt", dbt=True))
        steps.append(run_step("dbt_rebuild", dbt, logs, sampler, cwd=ROOT / "dbt", dbt=True))
        steps.append(run_step("cdc_reconcile", [PY, "scripts/cdc_reconcile.py"], logs, sampler))
        shutil.move(DATA / "cdc_reconcile.json", out / "cdc_reconcile.json")
        # The quality marts the snapshot reads (as make dq-snapshot).
        subprocess.run([DBT, "build", "--profiles-dir", str(PROFILES), "--vars", DBT_VARS, "-s",
                        "load_reliability", "cdc_latency", "cdc_slot_lag"],
                       cwd=ROOT / "dbt", check=True, capture_output=True)
    finally:
        sampler.stop()
        shutil.rmtree(SPILL, ignore_errors=True)
    for s in steps:
        s["containers"] = sampler.summary(s["step"])

    results = json.loads((out / "dbt_run_results.json").read_text())["results"]
    model_seconds = {r["unique_id"].split(".")[-1]: round(r["execution_time"], 2) for r in results
                     if r["unique_id"].startswith("model.")}
    report = {
        "scenario": scenario,
        "factor": factor,
        "volumes": volumes,
        "change_batch_events": events,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "total_seconds": round(sum(s["seconds"] for s in steps), 1),
        "steps": steps,
        "legacy_rows": legacy_rows(),
        "sizes": sizes(),
        "candidate_pairs": json.loads(pairs_file.read_text()),
        "splink_model_seconds": {m: model_seconds.get(m) for m in SPLINK_MODELS},
        "quality_gates_failed": sorted({f.split(".")[-1] for s in steps for f in s.get("failed", [])}),
        "slowest_models": dict(sorted(model_seconds.items(), key=lambda kv: -kv[1])[:10]),
        "machine": {"cpus": os.cpu_count(), "memory_gb": round(TOTAL_MEMORY / 1024 ** 3, 1),
                    "duckdb_memory_limit": yaml_setting("memory_limit"),
                    "duckdb_threads": yaml_setting("threads")},
    }
    (out / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    snapshot(report, out)
    return report


def yaml_setting(name: str):
    import yaml
    profile = yaml.safe_load((PROFILES / "profiles.yml").read_text())
    return profile["payer_data_readiness"]["outputs"]["local"]["config_options"][name]


def scale_metrics(report: dict) -> list[dict]:
    m = qh.metric
    out = [m("scale", "scale_factor", report["factor"]),
           m("scale", "change_batch_events", report["change_batch_events"]),
           m("scale", "total_seconds", report["total_seconds"])]
    for s in report["steps"]:
        out.append(m("scale", "step_seconds", s["seconds"], s["step"]))
        out.append(m("scale", "step_peak_rss_mb", s["peak_rss_mb"], s["step"]))
        out.append(m("scale", "step_min_available_mb", s["min_available_mb"], s["step"]))
        for container, c in s["containers"].items():
            out.append(m("scale", "container_peak_mem_mb", c["peak_mem_mb"], f"{container}:{s['step']}"))
            out.append(m("scale", "container_mean_cpu_pct", c["mean_cpu_pct"], f"{container}:{s['step']}"))
    for container in CONTAINERS:
        peaks = [s["containers"][container]["peak_mem_mb"] for s in report["steps"] if container in s["containers"]]
        if peaks:
            out.append(m("scale", "container_peak_mem_mb", max(peaks), container))
    for key, value in report["sizes"].items():
        if key.endswith("_mb"):
            out.append(m("scale", "disk_mb", value, key.removesuffix("_mb")))
    out.append(m("scale", "parquet_files", report["sizes"]["lakehouse_parquet_files"]))
    for table, n in report["legacy_rows"].items():
        out.append(m("scale", "legacy_rows", n, table))
    pairs = report["candidate_pairs"]
    for side, n in pairs["records"].items():
        out.append(m("scale", "splink_records", n, side))
    for kind in ("link", "dedupe"):
        for r in pairs[kind]:
            out.append(m("scale", "candidate_pairs", r["marginal"], f"{kind}:{r['rule']}"))
        out.append(m("scale", "candidate_pairs", pairs[kind][-1]["cumulative"], kind))
        out.append(m("scale", "possible_pairs", pairs[kind][-1]["possible"], kind))
    for gate in sorted(IDENTITY_GATES):
        out.append(m("scale", "quality_gate_failed", gate in report["quality_gates_failed"], gate))
    for model, seconds in report["splink_model_seconds"].items():
        out.append(m("scale", "splink_model_seconds", seconds, model))
    return out


def snapshot(report: dict, out: Path) -> dict:
    """The quality snapshot of this scale run: the lakehouse's quality marts, this
    run's dbt results and CDC reconciliation (not the other reports in data/,
    which come from other runs), the scored Splink pairs and the benchmark."""
    import lakehouse
    files = {"data/dbt_run_results.json": out / "dbt_run_results.json",
             "data/cdc_reconcile.json": out / "cdc_reconcile.json"}

    def read(path):
        f = files.get(path)
        return json.loads(f.read_text()) if f and f.is_file() else None

    con = lakehouse.connect(read_only=True)
    # Without the review decisions, splink_reviewed is the model itself: left out.
    metrics = [x for x in qh.from_reports(read) + qh.from_lakehouse(con) if x["dimension"] != "splink_reviewed"]
    for kind, table in (("link", "gold.identity_matches_splink"), ("dedupe", "gold.identity_pbm_duplicates_splink")):
        metrics.append(qh.metric("scale", "scored_pairs", con.execute(f"select count(*) from {table}").fetchone()[0],
                                 kind))
    metrics += scale_metrics(report)
    record = qh.make_record(metrics, scenario=report["scenario"], key_suffix=f"-{report['scenario']}")
    qh.write_snapshot(record, OUT / f"quality_snapshot_{report['scenario']}.json")
    return record


def summary() -> None:
    reports = [json.loads(f.read_text()) for f in sorted(OUT.glob("*/benchmark.json"))]
    reports.sort(key=lambda r: r["factor"])
    (OUT / "summary.json").write_text(json.dumps(
        [{k: r[k] for k in ("scenario", "factor", "volumes", "total_seconds", "sizes", "started_at")}
         | {"steps": {s["step"]: {"seconds": s["seconds"], "peak_rss_mb": s["peak_rss_mb"]} for s in r["steps"]},
            "candidate_pairs": {k: r["candidate_pairs"][k][-1]["cumulative"] for k in ("link", "dedupe")}}
         for r in reports], indent=2) + "\n")
    if not reports:
        return
    names = [s["step"] for s in reports[0]["steps"]]
    print("\n| Step | " + " | ".join(r["scenario"] for r in reports) + " |")
    print("|---|" + "---:|" * len(reports))
    for name in names:
        cells = []
        for r in reports:
            s = next((x for x in r["steps"] if x["step"] == name), None)
            cells.append("—" if s is None else f"{s['seconds']:,.1f} s / {s['peak_rss_mb']:,.0f} MB")
        print(f"| {name} | " + " | ".join(cells) + " |")
    print("| total | " + " | ".join(f"{r['total_seconds']:,.0f} s" for r in reports) + " |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("factors", nargs="+", type=int, help="scale factors (1 = the base volumes)")
    ap.add_argument("--events-per-row", type=float, default=0.01,
                    help="change batch size per member, claim and prior authorization (default 0.01)")
    ap.add_argument("--memory-limit", type=float, default=0.70,
                    help="DuckDB memory limit in dbt, as a fraction of the machine's memory (default 0.70)")
    ap.add_argument("--duckdb-threads", type=int, default=4,
                    help="DuckDB threads in dbt (default 4, the GitHub runner's cores)")
    args = ap.parse_args()
    if not (DATA / "synthea" / "fhir").is_dir():
        raise SystemExit("No Synthea data in data/synthea/fhir/: run make synthea first.")
    OUT.mkdir(parents=True, exist_ok=True)
    write_profile(args.memory_limit, args.duckdb_threads)
    for factor in args.factors:
        benchmark(factor, args.events_per_row)
    summary()


if __name__ == "__main__":
    main()
