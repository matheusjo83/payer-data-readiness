"""Run the official SQL on FHIR v2 test suite against the DuckDB compiler.

The suite lives in https://github.com/FHIR/sql-on-fhir.js (tests/*.json). It is
downloaded once, pinned to SUITE_COMMIT, into data/sof_tests/. Results are
compared the way the reference runner does: rows in any order, each row with
exactly the view's columns; a test that expects an error passes if compiling or
running the view fails.

Usage (from the repository root):
    python fhir/conformance.py              # summary per file, writes data/sof_conformance.json
    python fhir/conformance.py -v           # also list failing tests

Exits with status 1 if any shareable test fails (experimental tests are reported
but do not fail the run).
"""

import argparse
import io
import json
import sys
import tarfile
import urllib.request
from collections import Counter
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from sof_duckdb.compiler import compile_view  # noqa: E402

SUITE_REPO = "FHIR/sql-on-fhir.js"
SUITE_COMMIT = "0821b673346bb859658dfc65f6b4808c23e028db"
SUITE_DIR = Path("data/sof_tests")
REPORT = Path("data/sof_conformance.json")


def download_suite() -> None:
    if any(SUITE_DIR.glob("*.json")):
        return
    url = f"https://codeload.github.com/{SUITE_REPO}/tar.gz/{SUITE_COMMIT}"
    print(f"Downloading test suite {SUITE_REPO}@{SUITE_COMMIT[:7]} ...")
    SUITE_DIR.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, tarfile.open(fileobj=io.BytesIO(resp.read())) as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) == 3 and parts[1] == "tests" and parts[2].endswith(".json"):
                (SUITE_DIR / parts[2]).write_bytes(tar.extractfile(member).read())


def normalize(value):
    """JSON value -> comparable Python value (integral floats become ints, as in JavaScript)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def canonical(rows) -> Counter:
    return Counter(json.dumps({k: normalize(v) for k, v in r.items()}, sort_keys=True) for r in rows)


def run_view(con, view, resources):
    con.execute("CREATE OR REPLACE TABLE resources (resource JSON)")
    con.executemany("INSERT INTO resources VALUES (?)", [[json.dumps(r)] for r in resources])
    sql = compile_view(view, "resources", typed=False)
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return [{n: (json.loads(v) if v is not None else None) for n, v in zip(names, row)}
            for row in cur.fetchall()]


def run_test(con, test, resources) -> tuple[bool, str]:
    try:
        rows = run_view(con, test["view"], resources)
    except Exception as exc:
        return ("expectError" in test), f"error: {str(exc).splitlines()[0][:160]}"
    if "expectError" in test:
        return False, "expected an error"
    if "expectColumns" in test:
        cols = list(rows[0].keys()) if rows else []
        if rows and cols != test["expectColumns"]:
            return False, f"columns {cols} != {test['expectColumns']}"
    if "expect" in test and canonical(rows) != canonical(test["expect"]):
        return False, f"got {rows[:3]}{'...' if len(rows) > 3 else ''}"
    return True, ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    download_suite()

    con = duckdb.connect()
    totals = Counter()
    report = {"suite": f"{SUITE_REPO}@{SUITE_COMMIT}", "files": {}, "failures": []}
    for path in sorted(SUITE_DIR.glob("*.json")):
        suite = json.loads(path.read_text())
        file_stats = Counter()
        for test in suite["tests"]:
            tag = "experimental" if "experimental" in test.get("tags", []) else "shareable"
            passed, detail = run_test(con, test, suite.get("resources", []))
            file_stats[(tag, passed)] += 1
            totals[(tag, passed)] += 1
            if not passed:
                report["failures"].append({"file": path.name, "test": test["title"], "tag": tag, "detail": detail})
        report["files"][path.name] = {
            "passed": file_stats[("shareable", True)] + file_stats[("experimental", True)],
            "total": sum(file_stats.values()),
        }
        print(f"{path.name:26} {report['files'][path.name]['passed']:3}/{report['files'][path.name]['total']:<3}")

    for tag in ("shareable", "experimental"):
        passed, total = totals[(tag, True)], totals[(tag, True)] + totals[(tag, False)]
        report[tag] = {"passed": passed, "total": total}
        print(f"{tag:12} {passed}/{total} ({100 * passed / total:.1f}%)")
    REPORT.write_text(json.dumps(report, indent=2))
    if args.verbose:
        for f in report["failures"]:
            print(f"FAIL [{f['tag']}] {f['file']} :: {f['test']} -> {f['detail']}")
    print(f"Report written to {REPORT}")
    if totals[("shareable", False)]:
        sys.exit(f"{totals[('shareable', False)]} shareable test(s) failed")


if __name__ == "__main__":
    main()
