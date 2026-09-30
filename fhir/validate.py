"""Validate a sample of the legacy-derived FHIR resources with the official HL7 validator.

Takes a reproducible sample of the gold FHIR models and runs the HL7 FHIR
validator on it. The sample is the first N resources ordered by md5 of a seed
and the resource ID, so it does not depend on the physical row order, which
changes each time dbt rebuilds a table (a reservoir sample did). Terminology is not checked (-tx n/a): the
validator checks structure, cardinality, data types, invariants and value sets
it can expand locally, but not whether codes such as CPT, ICD-10-CM or NDC exist
in their code systems.

Two modes:

  default          base FHIR R4 (4.0.1): one file per resource, with meta.profile
                   removed from the exported copy so that only the base
                   specification applies. Writes data/fhir_validation/.
  --ig carin-bb    the CARIN Blue Button IG (STU 2.1.0, the version CMS
                   recommends for the Patient Access API), against the profile
                   each resource declares. Each ExplanationOfBenefit profile is
                   sampled on its own. Every sampled resource is written as a
                   collection Bundle together with the resources it references
                   (Patient, Coverage, Organization), so the validator resolves
                   the references and checks the referenced resources against
                   the target profiles too. Writes data/fhir_validation_carin-bb/.

Usage (from the repository root, Java 17+ required):
    python fhir/validate.py                 # base FHIR R4
    python fhir/validate.py --sample 50
    python fhir/validate.py --ig carin-bb   # CARIN Blue Button 2.1.0

Exits with status 1 if any sample has an error, if a sample is empty, or (with
--ig) if a sampled resource references a resource that is not exported.
Warnings do not fail the run.
"""

import argparse
import json
import re
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
    "Organization": "gold.fhir_organization",
}
# Base-mode samples (kept as they were before Organization existed).
BASE_SAMPLES = {t: None for t in ("Patient", "Coverage", "ExplanationOfBenefit")}

CARIN_PACKAGE = "hl7.fhir.us.carin-bb#2.1.0"
CARIN = "http://hl7.org/fhir/us/carin-bb/StructureDefinition/"
# label -> profile the sampled resources must declare (None: every resource of the type).
CARIN_SAMPLES = {
    "Patient": None,
    "Coverage": None,
    "Organization": None,
    "ExplanationOfBenefit.professional": CARIN + "C4BB-ExplanationOfBenefit-Professional-NonClinician|2.1.0",
    "ExplanationOfBenefit.inpatient": CARIN + "C4BB-ExplanationOfBenefit-Inpatient-Institutional|2.1.0",
    "ExplanationOfBenefit.outpatient": CARIN + "C4BB-ExplanationOfBenefit-Outpatient-Institutional|2.1.0",
    "ExplanationOfBenefit.pharmacy": CARIN + "C4BB-ExplanationOfBenefit-Pharmacy|2.1.0",
}
BUNDLE_BASE = "https://payer-data-readiness.example/fhir/"
SAMPLE_SEED = 42
# Code systems whose content is not distributed (the terminology package has
# only a stub). Without a terminology server (-tx n/a) the validator reports
# their codes as errors although nothing is wrong with them; with tx.fhir.org
# the same codes get a warning ("could not be found, so the code cannot be
# validated"). These errors are counted as terminology_unverified instead.
UNDISTRIBUTED_CODE_SYSTEMS = {
    "http://terminology.hl7.org/CodeSystem/NCPDPDispensedAsWrittenOrProductSelectionCode",
}


def download_validator() -> None:
    if VALIDATOR_JAR.exists():
        return
    VALIDATOR_JAR.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading HL7 FHIR validator {VALIDATOR_VERSION} ...")
    with urllib.request.urlopen(VALIDATOR_URL) as resp, VALIDATOR_JAR.open("wb") as f:
        shutil.copyfileobj(resp, f)


def references(node) -> list[str]:
    """Relative references ('Type/id') anywhere in a resource."""
    if isinstance(node, dict):
        found = [node["reference"]] if isinstance(node.get("reference"), str) else []
        return found + [r for v in node.values() for r in references(v)]
    if isinstance(node, list):
        return [r for v in node for r in references(v)]
    return []


def bundle_with_references(con, focal: dict) -> tuple[dict, list[str]]:
    """A collection Bundle (the focal resource first, then everything it references,
    transitively) and the references that point to no exported resource. The
    validator does not report those as errors, so they are counted separately."""
    entries, seen, queue, unresolved = [], set(), [focal], []
    while queue:
        doc = queue.pop(0)
        key = f"{doc['resourceType']}/{doc['id']}"
        if key in seen:
            continue
        seen.add(key)
        entries.append({"fullUrl": BUNDLE_BASE + key, "resource": doc})
        for ref in references(doc):
            rtype, _, rid = ref.partition("/")
            if ref in seen:
                continue
            row = (con.execute(f"SELECT resource FROM {MODELS[rtype]} WHERE id = ?", [rid]).fetchone()
                   if rtype in MODELS else None)
            if row:
                queue.append(json.loads(row[0]))
            else:
                unresolved.append(f"{key} -> {ref}")
    bundle = {"resourceType": "Bundle", "id": re.sub(r"[^A-Za-z0-9.-]", "-", focal["id"]),
              "type": "collection", "entry": entries}
    return bundle, unresolved


def export_sample(sample: int, samples: dict, bundles: bool) -> tuple[dict, dict]:
    """Write each sample as one file per resource, named <label>-<id>.json.

    The label is a resource type, or ExplanationOfBenefit.<setting> for a sample
    restricted to one declared profile. With bundles, each file is a Bundle
    holding the resource and what it references; without, meta.profile is removed.
    """
    input_dir = OUT_DIR / "input"
    shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True)
    con = duckdb.connect(LAKEHOUSE, read_only=True)
    counts, unresolved = {}, {}
    try:
        for label, profile in samples.items():
            source = MODELS[label.partition(".")[0]]
            if profile:
                source = f"(SELECT * FROM {source} WHERE resource->>'$.meta.profile[0]' = '{profile}')"
            rows = con.execute(
                f"SELECT id, resource FROM {source} ORDER BY md5('{SAMPLE_SEED}:' || id) LIMIT {sample}"
            ).fetchall()
            for rid, resource in rows:
                doc = json.loads(resource)
                if bundles:
                    doc, missing = bundle_with_references(con, doc)
                    unresolved.setdefault(label, []).extend(missing)
                else:
                    doc.get("meta", {}).pop("profile", None)
                (input_dir / f"{label}-{rid}.json").write_text(json.dumps(doc))
            counts[label] = len(rows)
    finally:
        con.close()
    return counts, unresolved


def run_validator(extra_args: list[str] = ()) -> dict:
    # -clear-tx-cache: without a terminology server the validator still reuses codes
    # cached by earlier runs that had one, so results would depend on the machine.
    output = OUT_DIR / "results.json"
    cmd = ["java", "-jar", str(VALIDATOR_JAR), str(OUT_DIR / "input"),
           "-version", "4.0.1", "-tx", "n/a", "-clear-tx-cache", *extra_args, "-output", str(output)]
    print("Running:", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (OUT_DIR / "validator.log").write_text(proc.stdout + proc.stderr)
    if not output.exists():
        raise SystemExit(f"Validator produced no output (exit {proc.returncode}); see {OUT_DIR / 'validator.log'}")
    return json.loads(output.read_text())


def summarize(results: dict, counts: dict, unresolved: dict, ig: str | None = None, top: int = 15) -> dict:
    # One OperationOutcome per file (a Bundle when several files were validated).
    outcomes = [e["resource"] for e in results.get("entry", [])] if results.get("resourceType") == "Bundle" else [results]
    per_type = {t: Counter() for t in counts}
    messages = Counter()
    for oo in outcomes:
        source = next((ext.get("valueString", "") for ext in oo.get("extension", [])
                       if ext.get("url", "").endswith("operationoutcome-file")), "")
        rtype = Path(source).name.split("-", 1)[0]
        files_with_errors = False
        for issue in oo.get("issue", []):
            severity = issue.get("severity")
            text = issue.get("details", {}).get("text") or issue.get("diagnostics", "")
            if severity == "error" and any(f"CodeSystem '{cs}'" in text and "could not be found" in text
                                           for cs in UNDISTRIBUTED_CODE_SYSTEMS):
                per_type.setdefault(rtype, Counter())["terminology_unverified"] += 1
                continue
            if severity in ("fatal", "error"):
                files_with_errors = True
            if severity in ("fatal", "error", "warning"):
                per_type.setdefault(rtype, Counter())[severity if severity != "fatal" else "error"] += 1
                where = re.sub(r"\[\d+\]", "[n]", re.sub(r"/\*[^*]*\*/", "", (issue.get("expression") or [""])[0]))
                messages[(rtype, severity, f"{where}: {text}"[:260] if where else text[:200])] += 1
        per_type.setdefault(rtype, Counter())["resources_with_errors"] += files_with_errors

    summary = {
        "validator": VALIDATOR_VERSION,
        "fhir_version": "4.0.1",
        "implementation_guide": ig,
        "terminology_checked": False,
        "by_resource": {t: {"validated": counts.get(t, 0),
                            "resources_with_errors": per_type[t]["resources_with_errors"],
                            "errors": per_type[t]["error"],
                            "warnings": per_type[t]["warning"],
                            "terminology_unverified": per_type[t]["terminology_unverified"],
                            "unresolved_references": len(unresolved.get(t, []))} for t in counts},
        "unresolved_references": {t: refs[:20] for t, refs in unresolved.items() if refs},
        "undistributed_code_systems": sorted(UNDISTRIBUTED_CODE_SYSTEMS),
        "top_messages": [{"resource": r, "severity": s, "message": m, "count": n}
                         for (r, s, m), n in messages.most_common(top)],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200, help="resources per sample")
    ap.add_argument("--ig", choices=["carin-bb"], help=f"validate against an IG ({CARIN_PACKAGE})")
    ap.add_argument("--top", type=int, default=15, help="most frequent messages to report")
    args = ap.parse_args()
    global OUT_DIR
    if args.ig:
        OUT_DIR = Path(f"data/fhir_validation_{args.ig}")
    download_validator()
    counts, unresolved = export_sample(args.sample, CARIN_SAMPLES if args.ig else BASE_SAMPLES, bundles=bool(args.ig))
    extra = ["-ig", CARIN_PACKAGE] if args.ig else []
    summary = summarize(run_validator(extra), counts, unresolved, CARIN_PACKAGE if args.ig else None, args.top)

    width = max(map(len, summary["by_resource"])) + 1
    print(f"\n{'Resource':{width}} {'Validated':>9} {'With errors':>11} {'Errors':>7} {'Warnings':>8} {'Tx unverified':>13}")
    for t, s in summary["by_resource"].items():
        print(f"{t:{width}} {s['validated']:>9} {s['resources_with_errors']:>11} {s['errors']:>7} {s['warnings']:>8} {s['terminology_unverified']:>13}")
    for m in summary["top_messages"]:
        print(f"  [{m['severity']}] {m['resource']} x{m['count']}: {m['message']}")
    print(f"\nSummary written to {OUT_DIR / 'summary.json'}")

    empty = [t for t, s in summary["by_resource"].items() if s["validated"] == 0]
    failing = [t for t, s in summary["by_resource"].items() if s["resources_with_errors"]]
    dangling = [t for t, s in summary["by_resource"].items() if s["unresolved_references"]]
    if empty or failing or dangling:
        raise SystemExit(f"Validation failed: no resources for {empty or '-'}; errors in {failing or '-'}; "
                         f"unresolved references in {dangling or '-'}")


if __name__ == "__main__":
    main()
