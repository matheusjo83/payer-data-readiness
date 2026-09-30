"""Validate a sample of the legacy-derived FHIR resources with the official HL7 validator.

Takes a reproducible sample (reservoir, fixed seed) of each gold FHIR model,
writes one JSON file per resource and runs the HL7 FHIR validator against the
base FHIR R4 specification (4.0.1). Terminology is not checked (-tx n/a): the
validator checks structure, cardinality, data types and invariants, but not
whether codes such as CPT or ICD-10-CM exist in their code systems.

Usage (from the repository root, Java 17+ required):
    python fhir/validate.py                 # writes data/fhir_validation/summary.json
    python fhir/validate.py --sample 50

Exits with status 1 if any sampled resource has an error, or if a resource type
has nothing to validate. Warnings do not fail the run.
"""

import argparse
import json
import shutil
import subprocess
import urllib.request
from collections import Counter
from pathlib import Path

import duckdb

VALIDATOR_VERSION = "6.10.4"
VALIDATOR_URL = ("https://github.com/hapifhir/org.hl7.fhir.core/releases/download/"
                 f"{VALIDATOR_VERSION}/validator_cli.jar")
VALIDATOR_JAR = Path(f"tools/validator_cli-{VALIDATOR_VERSION}.jar")
LAKEHOUSE = "data/lakehouse.duckdb"
OUT_DIR = Path("data/fhir_validation")
MODELS = {
    "Patient": "gold.fhir_patient",
    "Coverage": "gold.fhir_coverage",
    "ExplanationOfBenefit": "gold.fhir_explanation_of_benefit",
}


def download_validator() -> None:
    if VALIDATOR_JAR.exists():
        return
    VALIDATOR_JAR.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading HL7 FHIR validator {VALIDATOR_VERSION} ...")
    with urllib.request.urlopen(VALIDATOR_URL) as resp, VALIDATOR_JAR.open("wb") as f:
        shutil.copyfileobj(resp, f)


def export_sample(sample: int) -> dict:
    input_dir = OUT_DIR / "input"
    shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True)
    con = duckdb.connect(LAKEHOUSE, read_only=True)
    counts = {}
    try:
        for rtype, table in MODELS.items():
            rows = con.execute(
                f"SELECT id, resource FROM {table} USING SAMPLE reservoir({sample} ROWS) REPEATABLE (42)"
            ).fetchall()
            for rid, resource in rows:
                (input_dir / f"{rtype}-{rid}.json").write_text(resource)
            counts[rtype] = len(rows)
    finally:
        con.close()
    return counts


def run_validator() -> dict:
    output = OUT_DIR / "results.json"
    cmd = ["java", "-jar", str(VALIDATOR_JAR), str(OUT_DIR / "input"),
           "-version", "4.0.1", "-tx", "n/a", "-output", str(output)]
    print("Running:", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (OUT_DIR / "validator.log").write_text(proc.stdout + proc.stderr)
    if not output.exists():
        raise SystemExit(f"Validator produced no output (exit {proc.returncode}); see {OUT_DIR / 'validator.log'}")
    return json.loads(output.read_text())


def summarize(results: dict, counts: dict) -> dict:
    # One OperationOutcome per file (a Bundle when several files were validated).
    outcomes = [e["resource"] for e in results.get("entry", [])] if results.get("resourceType") == "Bundle" else [results]
    per_type = {t: Counter() for t in MODELS}
    messages = Counter()
    for oo in outcomes:
        source = next((ext.get("valueString", "") for ext in oo.get("extension", [])
                       if ext.get("url", "").endswith("operationoutcome-file")), "")
        rtype = Path(source).name.split("-", 1)[0]
        files_with_errors = False
        for issue in oo.get("issue", []):
            severity = issue.get("severity")
            if severity in ("fatal", "error"):
                files_with_errors = True
            if severity in ("fatal", "error", "warning"):
                per_type.setdefault(rtype, Counter())[severity if severity != "fatal" else "error"] += 1
                text = issue.get("details", {}).get("text") or issue.get("diagnostics", "")
                messages[(rtype, severity, text[:200])] += 1
        per_type.setdefault(rtype, Counter())["resources_with_errors"] += files_with_errors

    summary = {
        "validator": VALIDATOR_VERSION,
        "fhir_version": "4.0.1",
        "terminology_checked": False,
        "by_resource": {t: {"validated": counts.get(t, 0),
                            "resources_with_errors": per_type[t]["resources_with_errors"],
                            "errors": per_type[t]["error"],
                            "warnings": per_type[t]["warning"]} for t in MODELS},
        "top_messages": [{"resource": r, "severity": s, "message": m, "count": n}
                         for (r, s, m), n in messages.most_common(15)],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200, help="resources per type")
    args = ap.parse_args()
    download_validator()
    counts = export_sample(args.sample)
    summary = summarize(run_validator(), counts)

    print(f"\n{'Resource':22} {'Validated':>9} {'With errors':>11} {'Errors':>7} {'Warnings':>8}")
    for t, s in summary["by_resource"].items():
        print(f"{t:22} {s['validated']:>9} {s['resources_with_errors']:>11} {s['errors']:>7} {s['warnings']:>8}")
    for m in summary["top_messages"]:
        print(f"  [{m['severity']}] {m['resource']} x{m['count']}: {m['message']}")
    print(f"\nSummary written to {OUT_DIR / 'summary.json'}")

    empty = [t for t, s in summary["by_resource"].items() if s["validated"] == 0]
    failing = [t for t, s in summary["by_resource"].items() if s["resources_with_errors"]]
    if empty or failing:
        raise SystemExit(f"Validation failed: no resources for {empty or '-'}; errors in {failing or '-'}")


if __name__ == "__main__":
    main()
