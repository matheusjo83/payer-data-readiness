"""Check the FHIR server against the gold models (make fhir-api-check).

Every expected figure is computed from gold, independently of the server:

  counts       resources of each type on the server, and ExplanationOfBenefit by
               use (claim, preauthorization) and by type;
  searches     the ones CARIN Blue Button and Da Vinci PDex require of a server
               (patient, identifier, type, use, billable-period-start), on a
               reproducible sample of members, claims and PBM cardholder IDs;
  Bulk export  a system-level $export of Organization, Patient, Coverage and
               ExplanationOfBenefit: the exported IDs must be exactly the gold
               IDs, and every exported resource must equal its gold resource
               apart from meta (the server sets versionId and lastUpdated).

The server is reached through fhir-gateway-patient with the payer's
administration client (admin-patient-access, system/*.rs): these checks are
about the data the server holds; what a member's app may see is checked by
make fhir-security-check.

Writes data/fhir_api.json and exits with status 1 if any check fails.

Usage (from the repository root, after make fhir-publish):
    python fhir/api_check.py [--base http://localhost:8090/fhir]
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from smart_client import BackendClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
import lakehouse  # noqa: E402

REPORT = Path("data/fhir_api.json")
DEFAULT_BASE = os.environ.get("FHIR_BASE_URL", "http://localhost:8090/fhir")
SAMPLE = 20
SEED = 42
MEMBER_ID = "https://payer-data-readiness.example/member-id"
CARDHOLDER_ID = "https://pbm.example/cardholder-id"
# Gold model of each resource type the server holds; ExplanationOfBenefit comes
# from two models (claims and prior authorizations).
GOLD = {
    "Organization": ["gold.fhir_organization"],
    "Patient": ["gold.fhir_patient"],
    "Coverage": ["gold.fhir_coverage"],
    "ExplanationOfBenefit": ["gold.fhir_explanation_of_benefit", "gold.fhir_prior_authorization"],
}
ADMIN = BackendClient("admin-patient-access", "system/*.rs")
ALL_EOB = ("(SELECT id, resource FROM gold.fhir_explanation_of_benefit "
           "UNION ALL SELECT id, resource FROM gold.fhir_prior_authorization)")


def get(url: str, accept: str = "application/fhir+json", headers: dict | None = None):
    # no-cache: HAPI reuses the result of an identical search for a minute, so a
    # check run right after a change would otherwise see the old count.
    req = urllib.request.Request(url, headers={"Accept": accept, "Cache-Control": "no-cache", **ADMIN.headers(),
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.status, dict(resp.headers), resp.read()


def total(base: str, query: str) -> int:
    sep = "&" if "?" in query else "?"
    return json.loads(get(f"{base}/{query}{sep}_summary=count")[2])["total"]


def without_meta(resource: dict) -> dict:
    return {k: v for k, v in resource.items() if k != "meta"}


class Checks:
    def __init__(self):
        self.results = []

    def add(self, group: str, name: str, expected, actual) -> None:
        self.results.append({"group": group, "check": name, "expected": expected, "actual": actual,
                             "passed": expected == actual})

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.results if not r["passed"]]


def check_counts(base: str, con, checks: Checks) -> None:
    for rtype, models in GOLD.items():
        expected = sum(con.execute(f"SELECT count(*) FROM {m}").fetchone()[0] for m in models)
        checks.add("counts", rtype, expected, total(base, rtype))
    for use in ("claim", "preauthorization"):
        expected = con.execute(f"SELECT count(*) FROM {ALL_EOB} WHERE resource->>'$.use' = ?", [use]).fetchone()[0]
        query = f"ExplanationOfBenefit?use={use}"
        checks.add("counts", query, expected, total(base, query))
    type_code = "resource->>'$.type.coding[0].code'"
    for (code,) in con.execute(f"SELECT DISTINCT {type_code} FROM {ALL_EOB} ORDER BY 1").fetchall():
        expected = con.execute(f"SELECT count(*) FROM {ALL_EOB} WHERE {type_code} = ?", [code]).fetchone()[0]
        query = f"ExplanationOfBenefit?type={code}"
        checks.add("counts", query, expected, total(base, query))
    day = "2026-01-01"
    expected = con.execute(f"SELECT count(*) FROM {ALL_EOB} WHERE resource->>'$.billablePeriod.start' >= ?",
                           [day]).fetchone()[0]
    checks.add("counts", f"ExplanationOfBenefit?billable-period-start=ge{day}", expected,
               total(base, f"ExplanationOfBenefit?billable-period-start=ge{day}"))


def sample(con, sql: str) -> list:
    return con.execute(f"SELECT * FROM ({sql}) ORDER BY md5('{SEED}:' || k) LIMIT {SAMPLE}").fetchall()


def check_searches(base: str, con, checks: Checks) -> None:
    def ids(query: str) -> list[str]:
        bundle = json.loads(get(f"{base}/{query}{'&' if '?' in query else '?'}_count=500&_elements=id")[2])
        return sorted(e["resource"]["id"] for e in bundle.get("entry", []))

    q = urllib.parse.quote
    for (member,) in sample(con, "SELECT id AS k FROM gold.fhir_patient"):
        checks.add("Patient?identifier (member ID)", member, [member],
                   ids(f"Patient?identifier={q(MEMBER_ID + '|' + member)}"))
        expected = [r[0] for r in con.execute(
            f"SELECT id FROM {ALL_EOB} WHERE resource->>'$.patient.reference' = ? ORDER BY id",
            [f"Patient/{member}"]).fetchall()]
        checks.add("ExplanationOfBenefit?patient", member, expected,
                   ids(f"ExplanationOfBenefit?patient=Patient/{member}"))
        expected = [r[0] for r in con.execute(
            "SELECT id FROM gold.fhir_prior_authorization WHERE resource->>'$.patient.reference' = ? ORDER BY id",
            [f"Patient/{member}"]).fetchall()]
        checks.add("ExplanationOfBenefit?patient&use=preauthorization", member, expected,
                   ids(f"ExplanationOfBenefit?patient=Patient/{member}&use=preauthorization"))
        expected = [r[0] for r in con.execute(
            "SELECT id FROM gold.fhir_coverage WHERE resource->>'$.beneficiary.reference' = ? ORDER BY id",
            [f"Patient/{member}"]).fetchall()]
        checks.add("Coverage?patient", member, expected, ids(f"Coverage?patient=Patient/{member}"))
    for cardholder, member in sample(con, "SELECT cardholder_id AS k, member_id FROM gold.member_xref "
                                          "WHERE member_id IS NOT NULL"):
        checks.add("Patient?identifier (PBM cardholder ID)", cardholder, [member],
                   ids(f"Patient?identifier={q(CARDHOLDER_ID + '|' + cardholder)}"))
    for eob_id, system, value in sample(con, f"SELECT id AS k, resource->>'$.identifier[0].system' AS s, "
                                             f"resource->>'$.identifier[0].value' AS v FROM {ALL_EOB}"):
        checks.add("ExplanationOfBenefit?identifier", eob_id, [eob_id],
                   ids(f"ExplanationOfBenefit?identifier={q(system + '|' + value)}"))


def check_bulk_export(base: str, con, checks: Checks) -> dict:
    types = ",".join(GOLD)
    started = time.time()
    status, headers, _ = get(f"{base}/$export?_type={types}", headers={"Prefer": "respond-async"})
    poll = headers.get("Content-Location")
    if status != 202 or not poll:
        checks.add("bulk export", "kick-off", "202 with Content-Location", f"{status} {poll}")
        return {}
    while True:
        status, headers, body = get(poll, accept="application/json")
        if status == 200:
            manifest = json.loads(body)
            break
        if time.time() - started > 1800:
            checks.add("bulk export", "completed within 30 minutes", True, False)
            return {}
        time.sleep(2)
    exported = defaultdict(dict)
    for output in manifest.get("output", []):
        for line in get(output["url"], accept="application/fhir+ndjson")[2].decode().splitlines():
            if line.strip():
                resource = json.loads(line)
                exported[output["type"]][resource["id"]] = resource
    seconds = round(time.time() - started, 1)
    for rtype, models in GOLD.items():
        gold = {rid: json.loads(r) for m in models
                for rid, r in con.execute(f"SELECT id, resource FROM {m}").fetchall()}
        got = exported.get(rtype, {})
        checks.add("bulk export: same IDs as gold", rtype, len(gold), len(set(gold) & set(got)))
        checks.add("bulk export: IDs not in gold", rtype, 0, len(set(got) - set(gold)))
        differing = sorted(rid for rid in set(gold) & set(got) if without_meta(gold[rid]) != without_meta(got[rid]))
        checks.add("bulk export: resources equal to gold (apart from meta)", rtype, [], differing[:20])
    return {"seconds": seconds, "files": len(manifest.get("output", [])),
            "resources": {t: len(v) for t, v in exported.items()}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE, help="FHIR server base URL")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    metadata = json.loads(get(f"{base}/metadata")[2])
    checks = Checks()
    checks.add("server", "fhirVersion", "4.0.1", metadata.get("fhirVersion"))
    con = lakehouse.connect(read_only=True)
    try:
        check_counts(base, con, checks)
        check_searches(base, con, checks)
        bulk = check_bulk_export(base, con, checks)
    finally:
        con.close()

    groups = defaultdict(lambda: [0, 0])
    for r in checks.results:
        groups[r["group"]][0] += r["passed"]
        groups[r["group"]][1] += 1
    report = {"server": base, "software": metadata.get("software"), "bulk_export": bulk,
              "passed": sum(r["passed"] for r in checks.results), "total": len(checks.results),
              "groups": {g: {"passed": p, "total": t} for g, (p, t) in groups.items()},
              "failed": checks.failed, "checks": checks.results}
    REPORT.write_text(json.dumps(report, indent=2))
    for g, (p, t) in groups.items():
        print(f"{g:55} {p:>4}/{t}")
    if bulk:
        print(f"Bulk export: {bulk['resources']} in {bulk['files']} files, {bulk['seconds']} s")
    print(f"{report['passed']}/{report['total']} checks passed. Written to {REPORT}")
    for r in checks.failed[:20]:
        print(f"  FAILED {r['group']} {r['check']}: expected {str(r['expected'])[:120]}, got {str(r['actual'])[:120]}")
    if checks.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
