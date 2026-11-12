"""Validate a sample of the legacy-derived FHIR resources with the official HL7 validator.

Takes a reproducible sample of the gold FHIR models and runs the HL7 FHIR
validator on it. The sample is the first N resources ordered by md5 of a seed
and the resource ID, so it does not depend on the physical row order, which
changes each time dbt rebuilds a table (a reservoir sample did). By default,
terminology is not checked (-tx n/a): the validator checks structure,
cardinality, data types, invariants and value sets it can expand locally, but
not whether codes such as CPT, ICD-10-CM or NDC exist in their code systems.
With --tx <url> (make fhir-validate-carin uses https://tx.fhir.org), a
terminology server checks the codes too. Codes of code systems the server does
not have (NUBC revenue codes, the NCPDP stub, the Source of Payment Typology)
are counted as unverified. The lakehouse checks every code, not a sample, in
gold.terminology_code_check. Warnings the data cannot remove (see
KNOWN_WARNINGS: CARIN's claim identifier type, tx.fhir.org's outdated NDC, and
three PDex/ATR binding conflicts) are counted apart as known warnings, with the
reason in the summary. One error in HRex itself, which the IG's own examples
also fail (KNOWN_ERRORS), is counted apart the same way.

Two modes:

  default          base FHIR R4 (4.0.1): one file per resource, with meta.profile
                   removed from the exported copy so that only the base
                   specification applies (the CARIN package is loaded only for
                   the definitions of its code systems). Writes
                   data/fhir_validation/.
  --ig carin-bb    the CARIN Blue Button IG (STU 2.1.0, the version CMS
                   recommends for the Patient Access API), against the profile
                   each resource declares. Each ExplanationOfBenefit profile, and
                   the PBM claims, are sampled on their own. Every sampled resource is written as a
                   collection Bundle together with the resources it references
                   (Patient, Coverage, Organization), so the validator resolves
                   the references and checks the referenced resources against
                   the target profiles too. Writes data/fhir_validation_carin-bb/.
  --ig pdex        the Da Vinci PDex IG (STU 2.1.0), against the Prior
                   Authorization profile: one sample per decision (approved,
                   denied, pending), in Bundles as above, so the Coverage is
                   checked against Da Vinci HRex and the references against
                   US Core. Writes data/fhir_validation_pdex/.
  --ig provider-access
                   the data of the Provider Access and Payer-to-Payer APIs: the
                   claims without financial data against the CARIN Blue Button
                   2.1.0 Basis profiles (one sample per profile, and the PBM
                   claims), the attribution lists against the PDex Provider Group
                   profile and the opt-outs against the PDex Provider Access
                   Consent profile (in force and ended), in Bundles as above.
                   Writes data/fhir_validation_provider-access/.
  --ig payer-to-payer
                   the $bulk-member-match request and response that make
                   fhir-provider-api-check saved (data/fhir_provider_api/messages/),
                   against the PDex multi-member-match Parameters profiles, so the
                   HRex demographics, coverages and consents of the request and
                   the match groups of the response are checked too. Writes
                   data/fhir_validation_payer-to-payer/.

Usage (from the repository root, Java 17+ required):
    python fhir/validate.py                 # base FHIR R4
    python fhir/validate.py --sample 50
    python fhir/validate.py --ig carin-bb   # CARIN Blue Button 2.1.0
    python fhir/validate.py --ig carin-bb --tx https://tx.fhir.org   # with terminology
    python fhir/validate.py --ig pdex --tx https://tx.fhir.org        # PDex prior authorizations

Exits with status 1 if any sample has an error, if a sample is empty, or (with
--ig) if a sampled resource references a resource that is not exported.
Warnings do not fail the run.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
import lakehouse  # noqa: E402

VALIDATOR_VERSION = "6.10.4"
VALIDATOR_URL = ("https://github.com/hapifhir/org.hl7.fhir.core/releases/download/"
                 f"{VALIDATOR_VERSION}/validator_cli.jar")
VALIDATOR_JAR = Path(f"tools/validator_cli-{VALIDATOR_VERSION}.jar")
OUT_DIR = Path("data/fhir_validation")
MODELS = {
    "Patient": "gold.fhir_patient",
    "Coverage": "gold.fhir_coverage",
    "ExplanationOfBenefit": "gold.fhir_explanation_of_benefit",
    "Organization": "gold.fhir_organization",
    "PriorAuthorization": "gold.fhir_prior_authorization",   # ExplanationOfBenefit, use = preauthorization
    # Provider Access and Payer-to-Payer APIs.
    "Basis": "gold.fhir_explanation_of_benefit_basis",       # ExplanationOfBenefit without financial data
    "ProviderGroup": "gold.fhir_provider_group",              # Group (attribution list)
    "ProviderConsent": "gold.fhir_provider_consent",          # Consent (opt-out)
}
# Base-mode samples (kept as they were before Organization existed).
BASE_SAMPLES = {t: None for t in ("Patient", "Coverage", "ExplanationOfBenefit")}

CARIN_PACKAGE = "hl7.fhir.us.carin-bb#2.1.0"
CARIN = "http://hl7.org/fhir/us/carin-bb/StructureDefinition/"


def declares(profile: str) -> str:
    return f"resource->>'$.meta.profile[0]' = '{CARIN}{profile}|2.1.0'"


# label -> SQL condition on the gold table that selects the sample's population
# (None: every resource of the type). PBM claims (IDs starting with PBM) are a
# sample of their own, so the legacy pharmacy sample stays the same.
CARIN_SAMPLES = {
    "Patient": None,
    "Coverage": None,
    "Organization": None,
    "ExplanationOfBenefit.professional": declares("C4BB-ExplanationOfBenefit-Professional-NonClinician"),
    "ExplanationOfBenefit.inpatient": declares("C4BB-ExplanationOfBenefit-Inpatient-Institutional"),
    "ExplanationOfBenefit.outpatient": declares("C4BB-ExplanationOfBenefit-Outpatient-Institutional"),
    "ExplanationOfBenefit.pharmacy": declares("C4BB-ExplanationOfBenefit-Pharmacy") + " AND id NOT LIKE 'PBM%'",
    "ExplanationOfBenefit.pbm": "id LIKE 'PBM%'",
}
PDEX_PACKAGE = "hl7.fhir.us.davinci-pdex#2.1.0"
REVIEW_ACTION = ("json_extract_string(resource, "
                 "'$.item[0].adjudication[0].extension[0].extension[0].valueCodeableConcept.coding[0].code')")
# One sample per decision (A1 approved, A3 denied, A4 pending: X12 306 review action codes).
PDEX_SAMPLES = {
    "PriorAuthorization.approved": f"{REVIEW_ACTION} = 'A1'",
    "PriorAuthorization.denied": f"{REVIEW_ACTION} = 'A3'",
    "PriorAuthorization.pending": f"{REVIEW_ACTION} = 'A4'",
}

def declares_basis(profile: str) -> str:
    return declares(f"{profile}-Basis")


# The data of the Provider Access (and Payer-to-Payer) API: the claims without
# financial data against the CARIN Basis profiles, one sample per profile and the
# PBM claims apart, and the attribution lists and opt-outs against PDex. Both
# packages are loaded.
PROVIDER_ACCESS_SAMPLES = {
    "Basis.professional": declares_basis("C4BB-ExplanationOfBenefit-Professional-NonClinician"),
    "Basis.inpatient": declares_basis("C4BB-ExplanationOfBenefit-Inpatient-Institutional"),
    "Basis.outpatient": declares_basis("C4BB-ExplanationOfBenefit-Outpatient-Institutional"),
    "Basis.pharmacy": declares_basis("C4BB-ExplanationOfBenefit-Pharmacy") + " AND id NOT LIKE 'PBM%'",
    "Basis.pbm": "id LIKE 'PBM%'",
    "ProviderGroup": None,
    "ProviderConsent.active": "end_date IS NULL",
    "ProviderConsent.ended": "end_date IS NOT NULL",
}
# The Payer-to-Payer messages are not in gold: make fhir-provider-api-check writes
# the $bulk-member-match request it sends (as a new payer would) and the response
# of the gateway. Label -> file in that directory.
MESSAGES_DIR = Path("data/fhir_provider_api/messages")
PAYER_TO_PAYER_FILES = {
    "MatchRequest": "bulk-member-match-in.json",
    "MatchResponse": "bulk-member-match-out.json",
}
IGS = {"carin-bb": ([CARIN_PACKAGE], CARIN_SAMPLES), "pdex": ([PDEX_PACKAGE], PDEX_SAMPLES),
       "provider-access": ([CARIN_PACKAGE, PDEX_PACKAGE], PROVIDER_ACCESS_SAMPLES),
       "payer-to-payer": ([PDEX_PACKAGE], PAYER_TO_PAYER_FILES)}
BUNDLE_BASE = "https://payer-data-readiness.example/fhir/"
SAMPLE_SEED = 42
# Code systems whose content is not distributed (the terminology package has
# only a stub, and tx.fhir.org does not have it either: NUBC is licensed, and
# the Source of Payment Typology of Coverage.type is published by NAHDO with
# content not-present).
# Without a terminology server (-tx n/a) the validator reports their codes as
# errors although nothing is wrong with them; with tx.fhir.org the same codes
# get a warning ("could not be found, so the code cannot be validated"). Both
# are counted as terminology_unverified instead.
UNDISTRIBUTED_CODE_SYSTEMS = {
    "http://terminology.hl7.org/CodeSystem/NCPDPDispensedAsWrittenOrProductSelectionCode",
    "https://www.nubc.org/CodeSystem/RevenueCodes",
    "https://nahdo.org/sopt",
    # X12 306, the review action code of the PDex prior authorizations: X12 code
    # lists are licensed and no package or terminology server here has them.
    "https://codesystem.x12.org/005010/306",
}
# Value sets in the same situation (the validator reports them as not found).
UNDISTRIBUTED_VALUE_SET_PREFIXES = ("https://valueset.x12.org/",)
# Warnings that the data cannot remove, each with the reason. They are counted
# apart (known_warnings), listed in the summary, and never cover an error.
KNOWN_WARNINGS = {
    # CARIN requires these identifier types: uc (unique claim ID) on an EOB, MB
    # (member number) on a Patient's member ID. The base FHIR IdentifierType value
    # set (extensible) has no code for either, so the validator warns where the
    # base binding applies, as it does on CARIN's own examples.
    "ig_required_identifier_type": (
        lambda text, where: "value set 'IdentifierType'" in text
                            and ("C4BBIdentifierType#uc" in text or "v2-0203#MB)" in text),
        "CARIN requires the identifier types uc (claim) and MB (member), and the HRex member match uses MB too; the "
        "base IdentifierType value set has no code for a claim or member number"),
    # tx.fhir.org serves the NDC as of 2021-11-01 and marks every code in it inactive,
    # including products still marketed; gold.terminology_code_check checks the
    # NDCs against the FDA's current NDC Directory instead.
    "tx_ndc_snapshot": (
        lambda text, where: "has a status of inactive" in text and "productOrService" in where
                            and re.search(r"concept '\d{11}'", text) is not None,
        "tx.fhir.org's NDC is a 2021-11-01 snapshot that marks every code inactive; the lakehouse "
        "checks NDCs against the current FDA NDC Directory"),
    # Da Vinci PDex (Provider Access Consent) and HRex (Consent) fix the role of the
    # disclosing actor to provenance-participant-type#performer; the base Consent
    # binds the role (extensible) to SecurityRoleType, which has no such code.
    "pdex_consent_actor_role": (
        lambda text, where: "value set 'SecurityRoleType'" in text and "provenance-participant-type#performer" in text,
        "PDex and HRex fix the Consent source actor role to provenance-participant-type#performer; the base "
        "SecurityRoleType value set has no such code"),
    # Da Vinci PDex fixes the characteristic code of its Provider Group to
    # PdexMemberAttributionCS#pdexprovidergroup; the ATR Group it derives from binds
    # it (extensible) to the DaVinci Group Characteristic value set, which lacks it.
    "pdex_group_characteristic": (
        lambda text, where: "value set 'DaVinci Group Characteristic'" in text and "characteristic" in where,
        "PDex fixes the Provider Group characteristic to pdexprovidergroup; the ATR value set it derives from "
        "has no such code"),
    # The ATR coverage reference extension allows a US Core 7.0.0 Coverage or an ATR
    # Coverage; the Coverage conforms to both, and the validator notes that it
    # cannot tell which one was meant.
    "atr_coverage_reference_profiles": (
        lambda text, where: "Found multiple matching profiles for Coverage/" in text
                            and "atr-coverage" in text and "us-core-coverage" in text,
        "the Coverage conforms to both profiles the ATR coverage reference allows (US Core 7.0.0 and ATR), "
        "and the validator notes the ambiguity"),
}


# Errors in the IGs themselves, which no conforming resource can avoid: the IG's
# own examples fail the same way. Matched as narrowly as the warnings above,
# counted apart (known_errors) with the reason, and they do not fail the run;
# any other error does.
KNOWN_ERRORS = {
    # HRex 1.1.0 binds Consent.policy.uri (required) to a value set whose only two
    # codes are HTML page anchors (...hrex-consent.html#regular and #sensitive);
    # the validator looks URLs under hl7.org/fhir up as definitions and finds none.
    # The PDex 2.1.0 and HRex examples fail with the same error.
    "hrex_consent_policy_uri": (
        lambda text, where: where.endswith(".policy[0].uri") and "No definition could be found for URL value" in text
                            and re.search(r"'http://hl7\.org/fhir/us/davinci-hrex/StructureDefinition-hrex-consent"
                                          r"\.html#(regular|sensitive)'", text) is not None,
        "HRex 1.1.0 requires Consent.policy.uri to be one of two HTML page anchors (#regular, #sensitive), which the "
        "validator cannot resolve as definitions; the IG's own examples fail the same way"),
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

    The label is a resource type, or ExplanationOfBenefit.<sample> for a sample
    restricted by a condition (a declared profile, or the PBM claims). With bundles, each file is a Bundle
    holding the resource and what it references; without, meta.profile is removed.
    """
    input_dir = OUT_DIR / "input"
    shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True)
    con = lakehouse.connect(read_only=True)
    counts, unresolved = {}, {}
    try:
        for label, condition in samples.items():
            source = MODELS[label.partition(".")[0]]
            if condition:
                source = f"(SELECT * FROM {source} WHERE {condition})"
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


def copy_messages(files: dict) -> tuple[dict, dict]:
    """The Payer-to-Payer messages, as they are (they declare their PDex profiles)."""
    input_dir = OUT_DIR / "input"
    shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True)
    missing = [name for name in files.values() if not (MESSAGES_DIR / name).exists()]
    if missing:
        raise SystemExit(f"Missing {missing} in {MESSAGES_DIR}: run make fhir-provider-api-check first")
    for label, name in files.items():
        shutil.copy(MESSAGES_DIR / name, input_dir / f"{label}-{name}")
    return {label: 1 for label in files}, {}


def run_validator(extra_args: list[str] = (), tx: str = "n/a") -> dict:
    # -clear-tx-cache: the validator caches terminology answers between runs, so
    # without it an offline run would reuse codes checked by an earlier online one,
    # and an online run answers from what the server said last time.
    output = OUT_DIR / "results.json"
    cmd = ["java", "-jar", str(VALIDATOR_JAR), str(OUT_DIR / "input"),
           "-version", "4.0.1", "-tx", tx, "-clear-tx-cache", *extra_args, "-output", str(output)]
    print("Running:", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (OUT_DIR / "validator.log").write_text(proc.stdout + proc.stderr)
    if not output.exists():
        raise SystemExit(f"Validator produced no output (exit {proc.returncode}); see {OUT_DIR / 'validator.log'}")
    return json.loads(output.read_text())


def summarize(results: dict, counts: dict, unresolved: dict, ig: str | None = None, top: int = 15,
              tx: str = "n/a") -> dict:
    # One OperationOutcome per file (a Bundle when several files were validated).
    if results.get("resourceType") == "Bundle":
        outcomes = [e["resource"] for e in results.get("entry", [])]
    else:
        outcomes = [results]
    per_type = {t: Counter() for t in counts}
    messages = Counter()
    known_counts = Counter()
    for oo in outcomes:
        source = next((ext.get("valueString", "") for ext in oo.get("extension", [])
                       if ext.get("url", "").endswith("operationoutcome-file")), "")
        rtype = Path(source).name.split("-", 1)[0]
        files_with_errors = False
        for issue in oo.get("issue", []):
            severity = issue.get("severity")
            text = issue.get("details", {}).get("text") or issue.get("diagnostics", "")
            undistributed = (any(f"CodeSystem '{cs}'" in text and "could not be found" in text
                                 for cs in UNDISTRIBUTED_CODE_SYSTEMS)
                             or any(f"ValueSet '{vs}" in text and "not found" in text
                                    for vs in UNDISTRIBUTED_VALUE_SET_PREFIXES))
            if severity in ("error", "warning") and undistributed:
                per_type.setdefault(rtype, Counter())["terminology_unverified"] += 1
                continue
            where_raw = (issue.get("expression") or issue.get("location") or [""])[0]
            known_error = next((k for k, (match, _) in KNOWN_ERRORS.items()
                                if severity == "error" and match(text, re.sub(r"/\*[^*]*\*/", "", where_raw))), None)
            if known_error:
                per_type.setdefault(rtype, Counter())["known_errors"] += 1
                known_counts[known_error] += 1
                continue
            known = next((k for k, (match, _) in KNOWN_WARNINGS.items()
                          if severity == "warning" and match(text, where_raw)), None)
            if known:
                per_type.setdefault(rtype, Counter())["known_warnings"] += 1
                known_counts[known] += 1
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
        "terminology_checked": tx != "n/a",
        "terminology_server": None if tx == "n/a" else tx,
        "by_resource": {t: {"validated": counts.get(t, 0),
                            "resources_with_errors": per_type[t]["resources_with_errors"],
                            "errors": per_type[t]["error"],
                            "warnings": per_type[t]["warning"],
                            "terminology_unverified": per_type[t]["terminology_unverified"],
                            "known_warnings": per_type[t]["known_warnings"],
                            "known_errors": per_type[t]["known_errors"],
                            "unresolved_references": len(unresolved.get(t, []))} for t in counts},
        "unresolved_references": {t: refs[:20] for t, refs in unresolved.items() if refs},
        "undistributed_code_systems": sorted(UNDISTRIBUTED_CODE_SYSTEMS),
        "known_warnings": {k: {"count": known_counts[k], "reason": reason}
                           for k, (_, reason) in KNOWN_WARNINGS.items()},
        "known_errors": {k: {"count": known_counts[k], "reason": reason}
                         for k, (_, reason) in KNOWN_ERRORS.items()},
        "top_messages": [{"resource": r, "severity": s, "message": m, "count": n}
                         for (r, s, m), n in messages.most_common(top)],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200, help="resources per sample")
    ap.add_argument("--ig", choices=sorted(IGS),
                    help=f"validate against an IG ({CARIN_PACKAGE}, {PDEX_PACKAGE}, or both for provider-access)")
    ap.add_argument("--top", type=int, default=15, help="most frequent messages to report")
    ap.add_argument("--tx", default="n/a", help="terminology server (default n/a: codes not checked)")
    args = ap.parse_args()
    global OUT_DIR
    if args.ig:
        OUT_DIR = Path(f"data/fhir_validation_{args.ig}")
    download_validator()
    packages, samples = IGS[args.ig] if args.ig else ([CARIN_PACKAGE], BASE_SAMPLES)
    if args.ig == "payer-to-payer":
        counts, unresolved = copy_messages(samples)
    else:
        counts, unresolved = export_sample(args.sample, samples, bundles=bool(args.ig))
    # The base mode loads the CARIN package too, but only as definitions (its code
    # systems): the exported copies carry no meta.profile, so no profile applies.
    extra = [arg for package in packages for arg in ("-ig", package)]
    summary = summarize(run_validator(extra, args.tx), counts, unresolved, ", ".join(packages) if args.ig else None,
                        args.top, args.tx)

    width = max(map(len, summary["by_resource"])) + 1
    print(f"\n{'Resource':{width}} {'Validated':>9} {'With errors':>11} {'Errors':>7} {'Warnings':>8} "
          f"{'Tx unverified':>13} {'Known':>6} {'Known err':>9}")
    for t, s in summary["by_resource"].items():
        print(f"{t:{width}} {s['validated']:>9} {s['resources_with_errors']:>11} {s['errors']:>7} {s['warnings']:>8} "
              f"{s['terminology_unverified']:>13} {s['known_warnings']:>6} {s['known_errors']:>9}")
    for k, v in summary["known_warnings"].items():
        print(f"  known warning {k}: {v['count']} ({v['reason']})")
    for k, v in summary["known_errors"].items():
        print(f"  known IG error {k}: {v['count']} ({v['reason']})")
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
