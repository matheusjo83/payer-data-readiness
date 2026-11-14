"""Check the Provider Access and Payer-to-Payer APIs against gold (make fhir-provider-api-check).

Calls the FHIR gateway (fhir-gateway, in front of fhir-server-provider) as a
provider and as a new payer would, and compares every answer with a figure
computed from the gold models, independently of the server:

  counts           resources of each type on the server, the attribution lists
                   (PDex Provider Groups) and the opt-outs;
  no financial     a sample of claims equal to their Basis resource in gold, and
  data             no amount on any claim;
  attribution      Group search by provider NPI, and each list's members equal
                   to gold.provider_attribution without the opted-out members;
  export           $davinci-data-export of a sample of attribution lists
  (snapshot)       (provider-snapshot): exactly the members' Patient, Coverage,
                   claims (Basis) and prior authorizations, each equal to gold
                   apart from meta, and the Organizations they reference;
  patient          the patient parameter: only the members named, a non-member
                   reported in an OperationOutcome and ignored;
  opt-out at       an opt-out added after the list was built (a Consent put on
  execution        the server) leaves the member out of the next export;
  download/delta   provider-download records the transmission on the Group
                   members; provider-delta then leaves out what they were sent;
  refusals         an exportType the Group does not allow, a GET, and HAPI's
                   own $export (which would bypass the opt-outs);
  $bulk-member-     a request built from gold: former members with their member
  match            ID, with a changed family name, with an unknown member ID; a wrong
                   birth date, another payer's coverage, strangers, ambiguous
                   demographics; consents for non-sensitive data only, expired,
                   or naming another payer. Each must land in the expected
                   group, and the match group must be stored;
  export           $davinci-data-export of the match group (payertopayer):
  (payer-to-payer) the matched members' data, claims within 5 years and prior
                   authorizations pending or decided in the last 12 months;
  consent at       each matched member's consent period kept on the match group
  export           (member.period); a consent that ended between the match and
                   the export leaves the member out, reported in an
                   OperationOutcome (the gateway checks the period against
                   today, so the check ends it by editing the stored period, as
                   the administration client); the new payer revoking the
                   consent by deleting its match group: the files of a finished
                   export and the status of a running one answer 410, and a new
                   export is refused;
  $member-match    one member: a match, no match (422) and an unusable consent
                   (422);
  export expiry    through fhir-gateway-expiry (the same API, keeping a finished
                   export 20 seconds and one running export per client): a
                   second export while one runs refused (429, Retry-After); the
                   Expires the manifest announces fixed when the job ends; the
                   files served until then; after it, the job's directory gone
                   (removed by the sweeper, before any request), its status and
                   files 404; after a restart, a finished job unknown and its
                   files gone; and no copy left on the FHIR server (no Binary:
                   the gateway deletes HAPI's export files once it has read
                   them).

Every call carries a SMART access token (Backend Services): each attribution
list is read and exported by its own provider's client (provider-<id>), the
Payer-to-Payer calls by the new payer's (payer-new-payer), and the counts, the
claim sample, the reset of the lists and the opt-out added for the check by the
payer's administration client (admin-provider-access). What a client may not
do is checked by make fhir-security-check.

The Payer-to-Payer request and response are written to
data/fhir_provider_api/messages/ (validated by make fhir-validate-provider).
Writes data/fhir_provider_api.json and exits with status 1 if any check fails.

Usage (from the repository root, after make fhir-publish):
    python fhir/provider_api_check.py [--base http://localhost:8092/fhir]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from api_check import Checks, sample, without_meta
from smart_client import BackendClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
import lakehouse  # noqa: E402

REPORT = Path("data/fhir_provider_api.json")
MESSAGES = Path("data/fhir_provider_api/messages")
DEFAULT_BASE = os.environ.get("FHIR_GATEWAY_URL", "http://localhost:8092/fhir")
EXPIRY_BASE = os.environ.get("FHIR_GATEWAY_EXPIRY_URL", "http://localhost:8095/fhir")
EXPIRY_CONTAINER = "payer-fhir-gateway-expiry"
PROVIDER_SCOPE = "system/Patient.rs system/Coverage.rs system/ExplanationOfBenefit.rs system/Group.rs system/Organization.rs"
PAYER_SCOPE = "system/Group.crds system/Patient.rs system/Coverage.r system/ExplanationOfBenefit.r system/Organization.r"
ADMIN = BackendClient("admin-provider-access", "system/*.cruds")
NEW_PAYER = BackendClient("payer-new-payer", PAYER_SCOPE)
_providers: dict[str, BackendClient] = {}


def provider_client(group_or_provider_id: str) -> BackendClient:
    """The backend client of a provider (its attribution list is attribution-<provider id>)."""
    pid = group_or_provider_id.removeprefix("attribution-")
    if pid not in _providers:
        _providers[pid] = BackendClient(f"provider-{pid}", PROVIDER_SCOPE)
    return _providers[pid]

PDEX = "http://hl7.org/fhir/us/davinci-pdex/"
HREX = "http://hl7.org/fhir/us/davinci-hrex/"
MEMBER_ID = "https://payer-data-readiness.example/member-id"
PAYER_ID = {"system": "https://payer-data-readiness.example/payer-id", "value": "payer"}
NEW_PAYER_ID = {"system": "https://new-payer.example/payer-id", "value": "new-payer"}
OTHER_PAYER_ID = {"system": "https://other-payer.example/payer-id", "value": "other-payer"}
SNAPSHOT, DOWNLOAD, DELTA = (f"hl7.fhir.us.davinci-pdex#provider-{t}" for t in ("snapshot", "download", "delta"))
P2P = "hl7.fhir.us.davinci-pdex#payertopayer"
LAST_TRANSMISSION = PDEX + "StructureDefinition/base-ext-last-transmission"
TYPES = ["Patient", "Coverage", "ExplanationOfBenefit", "Organization"]


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None,
         client: BackendClient | None = None):
    """(status, headers, parsed JSON or raw bytes); HTTP errors are returned."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/fhir+json", "Content-Type": "application/fhir+json", "Cache-Control": "no-cache",
        **(client.headers() if client else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            status, hdrs, raw = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, dict(e.headers), e.read()
    try:
        return status, hdrs, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, hdrs, raw


def ndjson(url: str, client: BackendClient) -> list[dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/fhir+ndjson", **client.headers()})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return [json.loads(line) for line in resp.read().decode().splitlines() if line.strip()]


def parameters(**values) -> dict:
    params = []
    for name, value in values.items():
        for v in value if isinstance(value, list) else [value]:
            if name == "patient":
                params.append({"name": name, "valueReference": {"reference": v}})
            else:
                params.append({"name": name, "valueString" if name.startswith("_") else "valueCanonical": v})
    return {"resourceType": "Parameters", "parameter": params}


def export(base: str, group_id: str, client: BackendClient, **values) -> tuple[dict, dict, list]:
    """Run $davinci-data-export; (manifest, {type: {id: resource}}, OperationOutcomes)."""
    status, headers, body = call("POST", f"{base}/Group/{group_id}/$davinci-data-export", parameters(**values),
                                 {"Prefer": "respond-async"}, client)
    if status != 202:
        raise SystemExit(f"Export of Group/{group_id} not accepted: HTTP {status} {body}")
    poll, started = headers["Content-Location"], time.time()
    while True:
        status, _, manifest = call("GET", poll, headers={"Accept": "application/json"}, client=client)
        if status == 200:
            break
        if status != 202 or time.time() - started > 900:
            raise SystemExit(f"Export of Group/{group_id} failed: HTTP {status} {manifest}")
        time.sleep(2)
    resources = defaultdict(dict)
    for output in manifest["output"]:
        for r in ndjson(output["url"], client):
            resources[r["resourceType"]][r["id"]] = r
    outcomes = [o for err in manifest.get("error", []) for o in ndjson(err["url"], client)]
    return manifest, resources, outcomes


class Gold:
    """Expected data, from the gold models."""

    def __init__(self, con):
        self.con = con

    def rows(self, sql: str, params=None) -> list:
        return self.con.execute(sql, params or []).fetchall()

    def resources(self, model: str, where: str, params=None) -> dict:
        return {rid: json.loads(r) for rid, r in self.rows(f"SELECT id, resource FROM {model} WHERE {where}", params)}

    def opted_out_on(self, day: date) -> set[str]:
        return {m for (m,) in self.rows(
            "SELECT member_id FROM gold.fhir_provider_consent WHERE effective_date <= ? "
            "AND (end_date IS NULL OR end_date > ?)", [day, day])}

    def member_data(self, members: set[str]) -> dict:
        """{type: {id: resource}} of the members (no lookback), with the Organizations they reference."""
        refs = [f"Patient/{m}" for m in sorted(members)]
        if not refs:
            return {}
        marks = ",".join("?" * len(refs))
        data = {
            "Patient": self.resources("gold.fhir_patient", f"id IN ({','.join('?' * len(members))})", sorted(members)),
            "Coverage": self.resources("gold.fhir_coverage", f"resource->>'$.beneficiary.reference' IN ({marks})",
                                       refs),
            "ExplanationOfBenefit": {
                **self.resources("gold.fhir_explanation_of_benefit_basis",
                                 f"resource->>'$.patient.reference' IN ({marks})", refs),
                **self.resources("gold.fhir_prior_authorization",
                                 f"resource->>'$.patient.reference' IN ({marks})", refs)},
        }
        orgs = {ref.split("/", 1)[1] for rs in data.values() for r in rs.values()
                for ref in references(r) if ref.startswith("Organization/")}
        data["Organization"] = self.resources("gold.fhir_organization", f"id IN ({','.join('?' * len(orgs))})",
                                              sorted(orgs)) if orgs else {}
        return data


def references(node) -> set[str]:
    if isinstance(node, dict):
        found = {node["reference"]} if isinstance(node.get("reference"), str) else set()
        return found.union(*(references(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(references(v) for v in node)) if node else set()
    return set()


def patient_of(resource: dict) -> str | None:
    if resource["resourceType"] == "Patient":
        return f"Patient/{resource['id']}"
    field = {"Coverage": "beneficiary", "ExplanationOfBenefit": "patient"}.get(resource["resourceType"])
    return resource.get(field, {}).get("reference") if field else None


def compare_export(checks: Checks, group: str, label: str, expected: dict, got: dict) -> None:
    for rtype in TYPES:
        exp, act = expected.get(rtype, {}), got.get(rtype, {})
        checks.add(group, f"{label}: {rtype} IDs", sorted(exp), sorted(act))
        differing = sorted(i for i in set(exp) & set(act) if without_meta(exp[i]) != without_meta(act[i]))
        checks.add(group, f"{label}: {rtype} equal to gold apart from meta", [], differing[:20])
    claims = [r for r in got.get("ExplanationOfBenefit", {}).values() if r.get("use") == "claim"]
    checks.add(group, f"{label}: no amount on any claim", 0, sum('"amount"' in json.dumps(r) for r in claims))


def group_members(base: str, group_id: str, client: BackendClient) -> dict:
    status, _, group = call("GET", f"{base}/Group/{group_id}", client=client)
    return {m["entity"]["reference"]: m for m in group.get("member", [])} if status == 200 else {}


def check_counts(base: str, gold: Gold, checks: Checks) -> None:
    def total(query: str) -> int:
        return call("GET", f"{base}/{query}{'&' if '?' in query else '?'}_summary=count", client=ADMIN)[2]["total"]

    expected = {
        "Patient": "gold.fhir_patient", "Coverage": "gold.fhir_coverage", "Organization": "gold.fhir_organization",
        "Consent": "gold.fhir_provider_consent",
        "ExplanationOfBenefit?use=claim": "gold.fhir_explanation_of_benefit_basis",
        "ExplanationOfBenefit?use=preauthorization": "gold.fhir_prior_authorization",
        "Group?code=pdexprovidergroup": "gold.fhir_provider_group",
    }
    for query, model in expected.items():
        checks.add("counts", query, gold.rows(f"SELECT count(*) FROM {model}")[0][0], total(query))


def check_no_financial_data(base: str, gold: Gold, checks: Checks) -> None:
    for (eob_id,) in sample(gold.con, "SELECT id AS k FROM gold.fhir_explanation_of_benefit_basis"):
        expected = gold.resources("gold.fhir_explanation_of_benefit_basis", "id = ?", [eob_id])[eob_id]
        status, _, got = call("GET", f"{base}/ExplanationOfBenefit/{eob_id}", client=ADMIN)
        checks.add("no financial data: claim equal to its Basis resource", eob_id, True,
                   status == 200 and without_meta(got) == without_meta(expected))


def check_attribution(base: str, gold: Gold, checks: Checks, groups: list[str]) -> None:
    for provider_id, npi in sample(gold.con, "SELECT provider_id AS k, npi FROM gold.dim_provider WHERE npi_valid "
                                             "AND provider_id IN (SELECT provider_id FROM gold.fhir_provider_group)"):
        npi_token = urllib.parse.quote("http://hl7.org/fhir/sid/us-npi|" + npi)
        status, _, bundle = call("GET", f"{base}/Group?identifier={npi_token}&_elements=id",
                                 client=provider_client(provider_id))
        checks.add("attribution: Group?identifier (provider NPI)", provider_id, [f"attribution-{provider_id}"],
                   sorted(e["resource"]["id"] for e in bundle.get("entry", [])))
    for gid in groups:
        provider = gid.removeprefix("attribution-")
        expected = sorted(f"Patient/{m}" for (m,) in gold.rows(
            "SELECT member_id FROM gold.provider_attribution WHERE provider_id = ? AND NOT is_opted_out", [provider]))
        checks.add("attribution: list members equal to gold", gid, expected,
                   sorted(group_members(base, gid, provider_client(gid))))


def check_provider_exports(base: str, gold: Gold, checks: Checks, groups: list[str]) -> dict:
    timings = {}
    # A previous run may have recorded transmissions on these lists: start from gold.
    for gid in groups:
        resource = gold.resources("gold.fhir_provider_group", "id = ?", [gid])[gid]
        call("PUT", f"{base}/Group/{gid}", resource, client=ADMIN)

    for gid in groups:
        started = time.time()
        manifest, got, _ = export(base, gid, provider_client(gid), exportType=SNAPSHOT)
        timings[gid] = round(time.time() - started, 1)
        day = datetime.fromisoformat(manifest["transactionTime"]).date()
        members = {r.removeprefix("Patient/") for r in group_members(base, gid, provider_client(gid))} \
            - gold.opted_out_on(day)
        compare_export(checks, "export: provider-snapshot", gid, gold.member_data(members), got)

    gid = groups[0]
    provider = provider_client(gid)
    members = sorted(r.removeprefix("Patient/") for r in group_members(base, gid, provider))
    a, b = members[0], members[1]

    # patient parameter, with one reference that is not a member.
    _, got, outcomes = export(base, gid, provider, exportType=SNAPSHOT, patient=[f"Patient/{a}", f"Patient/{b}",
                                                                       "Patient/not-a-member"])
    compare_export(checks, "export: patient parameter", gid, gold.member_data({a, b}), got)
    checks.add("export: patient parameter", "non-member reported", True,
               any("Patient/not-a-member" in json.dumps(o) for o in outcomes))

    # An opt-out recorded after the list was built.
    gid2 = groups[1]
    members2 = sorted(r.removeprefix("Patient/") for r in group_members(base, gid2, provider_client(gid2)))
    x = members2[0]
    consent = gold.resources("gold.fhir_provider_consent", "true LIMIT 1")
    consent = next(iter(consent.values()))
    consent.update({"id": "check-opt-out", "patient": {"reference": f"Patient/{x}"},
                    "performer": [{"reference": f"Patient/{x}"}]})
    consent["provision"]["period"] = {"start": date.today().isoformat()}
    status, _, _ = call("PUT", f"{base}/Consent/check-opt-out", consent, client=ADMIN)
    try:
        manifest, got, outcomes = export(base, gid2, provider_client(gid2), exportType=SNAPSHOT)
    finally:
        call("DELETE", f"{base}/Consent/check-opt-out", client=ADMIN)
    day = datetime.fromisoformat(manifest["transactionTime"]).date()
    expected_members = set(members2) - {x} - gold.opted_out_on(day)
    checks.add("export: opt-out at execution", f"{gid2} without Patient/{x}",
               sorted(f"Patient/{m}" for m in expected_members),
               sorted(f"Patient/{p}" for p in got.get("Patient", {})))
    checks.add("export: opt-out at execution", "member's data left out", 0,
               sum(1 for rs in got.values() for r in rs.values() if patient_of(r) == f"Patient/{x}"))
    checks.add("export: opt-out at execution", "reported", True, any(f"Patient/{x}" in json.dumps(o) for o in outcomes))

    # provider-download records the transmission; provider-delta leaves it out.
    manifest, _, _ = export(base, gid, provider, exportType=DOWNLOAD, patient=[f"Patient/{a}"])
    after = group_members(base, gid, provider)
    sent = [e.get("valueDateTime") for e in after[f"Patient/{a}"].get("extension", []) if e["url"] == LAST_TRANSMISSION]
    checks.add("export: provider-download", "lastTransmitted recorded", [manifest["transactionTime"]], sent)
    checks.add("export: provider-download", "other members not marked", 0,
               sum(1 for ref, m in after.items() if ref != f"Patient/{a}"
                   and any(e["url"] == LAST_TRANSMISSION for e in m.get("extension", []))))
    manifest, got, _ = export(base, gid, provider, exportType=DELTA)
    day = datetime.fromisoformat(manifest["transactionTime"]).date()
    expected_members = set(members) - {a} - gold.opted_out_on(day)
    compare_export(checks, "export: provider-delta", gid, gold.member_data(expected_members), got)

    # Refusals.
    status, _, _ = call("POST", f"{base}/Group/{gid}/$davinci-data-export", parameters(exportType=P2P),
                        {"Prefer": "respond-async"}, provider)
    checks.add("refusals", "payertopayer on an attribution list", 400, status)
    status, _, _ = call("GET", f"{base}/Group/{gid}/$davinci-data-export", client=provider)
    checks.add("refusals", "GET $davinci-data-export", 405, status)
    status, _, _ = call("POST", f"{base}/Group/{gid}/$davinci-data-export", parameters(exportType=SNAPSHOT),
                        client=provider)
    checks.add("refusals", "without Prefer: respond-async", 400, status)
    status, _, _ = call("GET", f"{base}/Group/{gid}/$export", headers={"Prefer": "respond-async"}, client=provider)
    checks.add("refusals", "HAPI Group $export", 400, status)
    status, _, _ = call("GET", f"{base}/$export", headers={"Prefer": "respond-async"}, client=provider)
    checks.add("refusals", "HAPI system $export", 400, status)
    return timings


# --- Payer-to-Payer ----------------------------------------------------------------

def narrative(text: str) -> dict:
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml"><p>{text}</p></div>'}


def hrex_consent(n: int, policy: str = "regular", period: dict | None = None, source: dict = PAYER_ID) -> dict:
    return {
        "resourceType": "Consent",
        "meta": {"profile": [HREX + "StructureDefinition/hrex-consent"]},
        "text": narrative(f"Member {n} permits the old payer to disclose {policy} data to the new payer"),
        "status": "active",
        "scope": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/consentscope",
                              "code": "patient-privacy"}]},
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IDSCL"}]}],
        "patient": {"reference": f"Patient/{n}"},
        "performer": [{"reference": f"Patient/{n}"}],
        "sourceReference": {"reference": f"https://new-payer.example/fhir/DocumentReference/consent-{n}"},
        "policy": [{"uri": f"{HREX}StructureDefinition-hrex-consent.html#{policy}"}],
        "provision": {
            "type": "permit",
            "period": period or {"start": (date.today() - timedelta(days=10)).isoformat(),
                                 "end": (date.today() + timedelta(days=80)).isoformat()},
            "actor": [
                {"role": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                      "code": "performer"}]},
                 "reference": {"identifier": source, "display": "Old payer"}},
                {"role": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ParticipationType",
                                      "code": "IRCP"}]},
                 "reference": {"identifier": NEW_PAYER_ID, "display": "New payer"}}],
            "action": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/consentaction",
                                    "code": "disclose"}]}]},
    }


def member_bundle(n: int, first: str, last: str, birth: str, gender: str, member_id: str,
                  payor: dict = PAYER_ID, consent: dict | None = None) -> dict:
    self_relationship = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/subscriber-relationship",
                                     "code": "self"}]}
    patient = {"resourceType": "Patient", "id": str(n),
               "meta": {"profile": [HREX + "StructureDefinition/hrex-patient-demographics"]},
               "text": narrative(f"{first} {last}, born {birth}, {gender}"),
               "identifier": [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                                                    "code": "MB"}]},
                               "system": "https://new-payer.example/member-id", "value": f"NP{n:06d}"}],
               "name": [{"family": last, "given": [first]}], "gender": gender, "birthDate": birth}
    coverage = {"resourceType": "Coverage", "id": f"old-{n}",
                "meta": {"profile": [HREX + "StructureDefinition/hrex-coverage"]},
                "text": narrative(f"Coverage of member {n} with the old payer, member ID {member_id}"),
                "identifier": [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                                                     "code": "MB"}]},
                                "system": MEMBER_ID, "value": member_id}],
                "status": "active", "subscriberId": member_id, "beneficiary": {"reference": f"Patient/{n}"},
                "relationship": self_relationship,
                "payor": [{"identifier": payor, "display": "Old payer"}]}
    new_coverage = {"resourceType": "Coverage", "id": f"new-{n}",
                    "meta": {"profile": [HREX + "StructureDefinition/hrex-coverage"]},
                    "text": narrative(f"Coverage of member {n} with the new payer"),
                    "status": "active", "subscriberId": f"NP{n:06d}", "beneficiary": {"reference": f"Patient/{n}"},
                    "relationship": self_relationship,
                    "payor": [{"identifier": NEW_PAYER_ID, "display": "New payer"}]}
    return {"name": "MemberBundle", "part": [
        {"name": "MemberPatient", "resource": patient},
        {"name": "CoverageToMatch", "resource": coverage},
        {"name": "CoverageToLink", "resource": new_coverage},
        {"name": "Consent", "resource": consent or hrex_consent(n)}]}


def build_request(gold: Gold) -> tuple[dict, dict]:
    """A $bulk-member-match request and the expected outcome of each MemberBundle:
    {n: ('match', member_id) | ('nomatch', None) | ('consentconstraint', None)}."""
    # Former members whose demographics no other member shares.
    former = gold.rows(
        "WITH m AS (SELECT *, count(*) OVER (PARTITION BY first_name, last_name, birth_date, gender) AS same "
        "FROM gold.dim_member) "
        "SELECT member_id, first_name, last_name, strftime(birth_date, '%Y-%m-%d'), gender FROM m "
        "WHERE same = 1 AND NOT is_currently_covered AND birth_date IS NOT NULL AND gender IN ('male', 'female') "
        "AND first_name IS NOT NULL AND last_name IS NOT NULL "
        "ORDER BY md5('42:' || member_id) LIMIT 22")
    twins = gold.rows(
        "SELECT first_name, last_name, strftime(birth_date, '%Y-%m-%d'), gender FROM gold.dim_member "
        "WHERE birth_date IS NOT NULL AND gender IN ('male', 'female') GROUP BY ALL HAVING count(*) > 1 "
        "ORDER BY 1, 2, 3 LIMIT 1")
    bundles, expected = [], {}

    def add(kind, member, **kwargs):
        n = len(bundles) + 1
        mid, first, last, birth, gender = member
        # HRex requires a member ID on the coverage to match; one this payer does
        # not know (an old card) makes the match fall back to demographics.
        args = {"first": first, "last": last, "birth": birth, "gender": gender, "member_id": mid or f"X{n:011d}"}
        args.update({k: v for k, v in kwargs.items() if k in args})
        if args["member_id"] is None:
            args["member_id"] = f"X{n:011d}"
        consent = kwargs.get("consent")
        bundles.append(member_bundle(n, args["first"], args["last"], args["birth"], args["gender"], args["member_id"],
                                     kwargs.get("payor", PAYER_ID), consent(n) if consent else None))
        expected[n] = (kind, mid if kind == "match" else None)

    it = iter(former)
    for _ in range(8):                                        # member ID, same demographics
        add("match", next(it))
    for _ in range(3):                                        # family name changed since
        add("match", next(it), last="Newname")
    for _ in range(3):                                        # unknown member ID: unique demographics
        add("match", next(it), member_id=None)
    m = next(it)                                              # member ID, wrong birth date
    add("nomatch", m, birth=(date.fromisoformat(m[3]) - timedelta(days=400)).isoformat())
    add("nomatch", next(it), payor=OTHER_PAYER_ID)            # coverage with another payer
    add("nomatch", (None, "Quentin", "Zyxwvut", "1971-03-04", "male"))
    add("nomatch", (None, "Ottilie", "Qwertyuiop", "1988-11-30", "female"))
    for first, last, birth, gender in twins:                  # ambiguous demographics
        add("nomatch", (None, first, last, birth, gender))
    add("consentconstraint", next(it), consent=lambda n: hrex_consent(n, policy="sensitive"))
    add("consentconstraint", next(it), consent=lambda n: hrex_consent(n, period={"start": "2025-01-01",
                                                                                  "end": "2025-03-31"}))
    add("consentconstraint", next(it), consent=lambda n: hrex_consent(n, source=OTHER_PAYER_ID))
    request = {"resourceType": "Parameters",
               "meta": {"profile": [PDEX + "StructureDefinition/pdex-parameters-multi-member-match-bundle-in"]},
               "parameter": bundles}
    return request, expected


def kick_off(base: str, group_id: str, client: BackendClient, **values) -> tuple[int, str | None]:
    status, headers, _ = call("POST", f"{base}/Group/{group_id}/$davinci-data-export", parameters(**values),
                              {"Prefer": "respond-async"}, client)
    return status, headers.get("Content-Location")


def wait(poll: str, client: BackendClient) -> tuple[int, dict]:
    """The final status of an export job and its body (a manifest or an OperationOutcome)."""
    started = time.time()
    while True:
        status, _, body = call("GET", poll, headers={"Accept": "application/json"}, client=client)
        if status != 202 or time.time() - started > 900:
            return status, body
        time.sleep(2)


def match_group_of(base: str, bundles: list[dict]) -> dict:
    status, _, response = call("POST", f"{base}/Group/$bulk-member-match",
                               {"resourceType": "Parameters", "parameter": bundles}, client=NEW_PAYER)
    if status != 200:
        raise SystemExit(f"$bulk-member-match failed: HTTP {status} {response}")
    return next(p["resource"] for p in response["parameter"] if p["name"] == "MatchedMembers")


def check_consent_at_export(base: str, checks: Checks, request: dict, expected: dict, match_group: dict) -> None:
    """The consent kept on the match group and checked again when the export runs, and its
    revocation by the new payer (PDex: "The Data Export operation SHALL check the consent
    status for each member at the time of execution")."""
    group = "consent at export"
    by_n = {int(b["part"][0]["resource"]["id"]): b for b in request["parameter"]}
    matched = [n for n, (k, _) in sorted(expected.items()) if k == "match"]
    want = {f"Patient/{expected[n][1]}": next(p["resource"] for p in by_n[n]["part"]
                                               if p["name"] == "Consent")["provision"]["period"] for n in matched}
    checks.add(group, "member.period: each matched member's consent period", want,
               {m["entity"]["reference"]: m.get("period") for m in match_group.get("member", [])})

    # A consent that ends between the match and the export: the new match group's
    # first member's period ends yesterday (edited on the server, as time passing would).
    g2 = match_group_of(base, [by_n[n] for n in matched[:3]])
    refs = [m["entity"]["reference"] for m in g2["member"]]
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    stored = call("GET", f"{base}/Group/{g2['id']}", client=ADMIN)[2]
    stored["member"][0]["period"]["end"] = yesterday
    status, _, _ = call("PUT", f"{base}/Group/{g2['id']}", stored, client=ADMIN)
    checks.add(group, "consent ended yesterday (stored period edited)", 200, status)
    status, poll = kick_off(base, g2["id"], NEW_PAYER, exportType=P2P, _type="Patient")
    status, manifest = wait(poll, NEW_PAYER) if status == 202 else (status, {})
    checks.add(group, "export after the consent ended: HTTP", 200, status)
    got = {f"Patient/{r['id']}" for o in manifest.get("output", []) for r in ndjson(o["url"], NEW_PAYER)}
    checks.add(group, "export: only the members whose consent is in force", sorted(refs[1:]), sorted(got))
    notes = [i["diagnostics"] for e in manifest.get("error", []) for o in ndjson(e["url"], NEW_PAYER)
             for i in o["issue"]]
    checks.add(group, "export: the member left out is reported", True,
               any(d.startswith(refs[0] + ":") and f"ended {yesterday}" in d for d in notes))

    # Revocation after the export finished: the new payer deletes its match group.
    files = [o["url"] for o in manifest.get("output", []) + manifest.get("error", [])]
    status, _, _ = call("DELETE", f"{base}/Group/{g2['id']}", client=NEW_PAYER)
    checks.add(group, "revocation: the new payer deletes its match group", 200, status)
    checks.add(group, "revocation: the finished export's files -> 410", [410] * len(files),
               [call("GET", url, headers={"Accept": "application/fhir+ndjson"}, client=NEW_PAYER)[0]
                for url in files])
    checks.add(group, "revocation: the finished export's status -> 410", 410, wait(poll, NEW_PAYER)[0])
    checks.add(group, "revocation: a new export of the deleted group -> 404", 404,
               kick_off(base, g2["id"], NEW_PAYER, exportType=P2P)[0])

    # Revocation while an export runs: no manifest, no files.
    g3 = match_group_of(base, [by_n[matched[3]]])
    status, poll = kick_off(base, g3["id"], NEW_PAYER, exportType=P2P)
    checks.add(group, "revocation during an export: export accepted", 202, status)
    status, _, _ = call("DELETE", f"{base}/Group/{g3['id']}", client=NEW_PAYER)
    checks.add(group, "revocation during an export: match group deleted", 200, status)
    status, body = wait(poll, NEW_PAYER) if poll else (None, {})
    checks.add(group, "revocation during an export: status -> 410, no manifest", [410, "OperationOutcome"],
               [status, body.get("resourceType") if isinstance(body, dict) else None])


def check_payer_to_payer(base: str, gold: Gold, checks: Checks) -> dict:
    request, expected = build_request(gold)
    MESSAGES.mkdir(parents=True, exist_ok=True)
    (MESSAGES / "bulk-member-match-in.json").write_text(json.dumps(request, indent=1))
    status, _, response = call("POST", f"{base}/Group/$bulk-member-match", request, client=NEW_PAYER)
    checks.add("$bulk-member-match", "HTTP status", 200, status)
    if status != 200:
        return {}
    (MESSAGES / "bulk-member-match-out.json").write_text(json.dumps(response, indent=1))
    groups = {p["name"]: p["resource"] for p in response.get("parameter", [])}
    names = {"match": "MatchedMembers", "nomatch": "NonMatchedMembers",
             "consentconstraint": "ConsentConstrainedMembers"}
    for kind, name in names.items():
        group = groups.get(name, {})
        inputs = {c["id"] for c in group.get("contained", [])}
        checks.add("$bulk-member-match", f"{name}: input members", [str(n) for n, (k, _) in sorted(expected.items())
                                                                         if k == kind], sorted(inputs, key=int))
    matched = {f"Patient/{mid}" for k, mid in expected.values() if k == "match"}
    match_group = groups.get("MatchedMembers", {})
    checks.add("$bulk-member-match", "MatchedMembers: our members", sorted(matched),
               sorted(m["entity"]["reference"] for m in match_group.get("member", [])))
    gid = match_group.get("id")
    stored = group_members(base, gid, NEW_PAYER) if gid else {}
    checks.add("$bulk-member-match", "match group stored on the server", sorted(matched), sorted(stored))
    status, _, bundle = call("GET", f"{base}/Group?identifier="
                                    f"{urllib.parse.quote(NEW_PAYER_ID['system'] + '|' + NEW_PAYER_ID['value'])}"
                                    "&code=match&_elements=id&_count=500", client=NEW_PAYER)
    checks.add("$bulk-member-match", "match group found by the requesting payer's identifier", True,
               gid in {e["resource"]["id"] for e in bundle.get("entry", [])})

    status, _, _ = call("POST", f"{base}/Group/{gid}/$davinci-data-export", parameters(exportType=SNAPSHOT),
                        {"Prefer": "respond-async"}, NEW_PAYER)
    checks.add("refusals", "provider-snapshot on a match group", 400, status)
    started = time.time()
    manifest, got, _ = export(base, gid, NEW_PAYER, exportType=P2P)
    seconds = round(time.time() - started, 1)
    requested_at = datetime.fromisoformat(manifest["transactionTime"])
    cutoff = requested_at.date().replace(year=requested_at.year - 5)
    members = {m.removeprefix("Patient/") for m in matched}
    data = gold.member_data(members)
    in_scope_pas = {pid for (pid,) in gold.rows(
        "SELECT prior_auth_id FROM silver.stg_legacy__prior_auths WHERE decision = 'pending' OR decided_at >= ?",
        [requested_at.replace(tzinfo=None) - timedelta(days=365)])} if members else set()
    data["ExplanationOfBenefit"] = {
        i: r for i, r in data["ExplanationOfBenefit"].items()
        if (r["use"] == "preauthorization" and i in in_scope_pas)
        or (r["use"] == "claim" and r["billablePeriod"]["start"] >= cutoff.isoformat())}
    orgs = {ref.split("/", 1)[1] for rtype in ("Patient", "Coverage", "ExplanationOfBenefit")
            for r in data[rtype].values() for ref in references(r) if ref.startswith("Organization/")}
    data["Organization"] = {i: r for i, r in data["Organization"].items() if i in orgs}
    all_pas = sum(r["use"] == "preauthorization" for r in gold.member_data(members)["ExplanationOfBenefit"].values())
    kept_pas = sum(r["use"] == "preauthorization" for r in data["ExplanationOfBenefit"].values())
    compare_export(checks, "export: payertopayer", gid, data, got)
    check_consent_at_export(base, checks, request, expected, match_group)

    # $member-match (HRex), one member at a time.
    by_n = {int(b["part"][0]["resource"]["id"]): b for b in request["parameter"]}
    first_match = next(n for n, (k, _) in expected.items() if k == "match")
    first_nomatch = next(n for n, (k, _) in expected.items() if k == "nomatch")
    first_constrained = next(n for n, (k, _) in expected.items() if k == "consentconstraint")
    for n, want_status in ((first_match, 200), (first_nomatch, 422), (first_constrained, 422)):
        body = {"resourceType": "Parameters", "parameter": by_n[n]["part"]}
        status, _, resp = call("POST", f"{base}/Patient/$member-match", body, client=NEW_PAYER)
        checks.add("$member-match", f"{expected[n][0]} -> HTTP", want_status, status)
        if want_status == 200:
            got_ref = next((p["valueReference"]["reference"] for p in resp.get("parameter", [])
                            if p["name"] == "MemberId"), None)
            checks.add("$member-match", "MemberId", f"Patient/{expected[n][1]}", got_ref)
    return {"members_requested": len(expected), "matched": len(matched), "export_seconds": seconds,
            "match_group": gid, "lookback_from": cutoff.isoformat(),
            "prior_auths_of_matched_members": all_pas, "prior_auths_in_scope": kept_pas}


def job_dirs(container: str) -> list[str]:
    """The export job directories in a gateway container."""
    out = subprocess.run(["docker", "exec", container, "python", "-c",
                          "import os, tempfile; d = os.path.join(tempfile.gettempdir(), 'fhir-gateway-jobs'); "
                          "print(' '.join(sorted(os.listdir(d))) if os.path.isdir(d) else '')"],
                         capture_output=True, text=True, check=True)
    return out.stdout.split()


def wait_up(base: str) -> None:
    for _ in range(60):
        try:
            if call("GET", f"{base}/metadata")[0] == 200:
                return
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(2)
    raise SystemExit(f"{base} did not come up")


def finished(poll: str, client: BackendClient) -> tuple[int, dict, dict]:
    started = time.time()
    while True:
        status, headers, body = call("GET", poll, headers={"Accept": "application/json"}, client=client)
        if status != 202 or time.time() - started > 900:
            return status, headers, body
        time.sleep(1)


def check_export_expiry(base: str, expiry_base: str, checks: Checks, group_id: str) -> dict:
    """Exports expire: through the gateway instance that keeps them 20 seconds."""
    group = "export expiry"
    wait_up(expiry_base)
    client = provider_client(group_id)
    url = f"{expiry_base}/Group/{group_id}/$davinci-data-export"
    body, prefer = parameters(_type="Patient"), {"Prefer": "respond-async"}
    status, headers, _ = call("POST", url, body, prefer, client)
    checks.add(group, "export accepted", 202, status)
    poll = headers.get("Content-Location")
    status, headers, _ = call("POST", url, body, prefer, client)
    checks.add(group, "a second export while one runs: 429 with Retry-After", [429, True],
               [status, bool(headers.get("Retry-After"))])
    status, headers, manifest = finished(poll, client)
    ended = datetime.now(timezone.utc)
    checks.add(group, "export completed", 200, status)
    expires = parsedate_to_datetime(headers["Expires"])
    time.sleep(3)
    again = call("GET", poll, headers={"Accept": "application/json"}, client=client)[1].get("Expires")
    checks.add(group, "Expires fixed when the job ended (the same on a later poll)", headers["Expires"], again)
    checks.add(group, "Expires: about 20 seconds after the job ended", True,
               timedelta(seconds=15) <= expires - ended <= timedelta(seconds=21))
    files = [o["url"] for o in manifest["output"] + manifest.get("error", [])]
    get = lambda u: call("GET", u, headers={"Accept": "application/fhir+ndjson"}, client=client)[0]  # noqa: E731
    checks.add(group, "before Expires: the files served", [200] * len(files), [get(u) for u in files])
    status, _, count = call("GET", f"{base}/Binary?_summary=count", client=ADMIN)
    checks.add(group, "no copy left on the FHIR server (Binary count)", [200, 0], [status, count.get("total")])
    job_id = poll.rsplit("/", 1)[1]
    checks.add(group, "before Expires: the job's directory on disk", True, job_id in job_dirs(EXPIRY_CONTAINER))

    # Past Expires: the sweeper removes the job, before any request for it.
    time.sleep(max(0.0, (expires - datetime.now(timezone.utc)).total_seconds()) + 7)
    checks.add(group, "after Expires: the job's directory removed (sweeper)", False,
               job_id in job_dirs(EXPIRY_CONTAINER))
    checks.add(group, "after Expires: status 404", 404, call("GET", poll, client=client)[0])
    checks.add(group, "after Expires: files 404", [404] * len(files), [get(u) for u in files])

    # A restart: jobs live in memory, so their files on disk are removed at startup.
    status, headers, _ = call("POST", url, body, prefer, client)
    poll = headers.get("Content-Location")
    status, _, manifest = finished(poll, client)
    files = [o["url"] for o in manifest["output"]]
    job_id = poll.rsplit("/", 1)[1]
    present = job_id in job_dirs(EXPIRY_CONTAINER)
    subprocess.run(["docker", "restart", EXPIRY_CONTAINER], capture_output=True, check=True)
    wait_up(expiry_base)
    checks.add(group, "restart: the finished job's files on disk before, gone after", [True, False],
               [present, job_id in job_dirs(EXPIRY_CONTAINER)])
    checks.add(group, "restart: the job's status and files 404", [404] + [404] * len(files),
               [call("GET", poll, client=client)[0]] + [get(u) for u in files])
    return {"retention_seconds": round((expires - ended).total_seconds())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE, help="FHIR gateway base URL")
    ap.add_argument("--groups", type=int, default=3, help="attribution lists to export")
    ap.add_argument("--expiry-base", default=EXPIRY_BASE,
                    help="the gateway instance that keeps exports 20 seconds (fhir-gateway-expiry)")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    for _ in range(60):
        try:
            if call("GET", f"{base}/metadata")[0] == 200:
                break
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(5)
    status, _, metadata = call("GET", f"{base}/metadata")
    checks = Checks()
    checks.add("server", "fhirVersion", "4.0.1", metadata.get("fhirVersion"))
    checks.add("server", "instantiates the PDex provider-access server",
               True, PDEX + "CapabilityStatement/pdex-provider-access-server" in metadata.get("instantiates", []))
    con = lakehouse.connect(read_only=True)
    try:
        gold = Gold(con)
        groups = [g for (g,) in gold.rows(f"SELECT id FROM gold.fhir_provider_group ORDER BY md5('42:' || id) "
                                          f"LIMIT {args.groups}")]
        check_counts(base, gold, checks)
        check_no_financial_data(base, gold, checks)
        check_attribution(base, gold, checks, groups)
        timings = check_provider_exports(base, gold, checks, groups)
        p2p = check_payer_to_payer(base, gold, checks)
        expiry = check_export_expiry(base, args.expiry_base.rstrip("/"), checks, groups[0])
    finally:
        con.close()

    by_group = defaultdict(lambda: [0, 0])
    for r in checks.results:
        by_group[r["group"]][0] += r["passed"]
        by_group[r["group"]][1] += 1
    report = {"gateway": base, "software": metadata.get("software"),
              "snapshot_export_seconds": timings, "payer_to_payer": p2p, "export_expiry": expiry,
              "passed": sum(r["passed"] for r in checks.results), "total": len(checks.results),
              "groups": {g: {"passed": p, "total": t} for g, (p, t) in by_group.items()},
              "failed": checks.failed, "checks": checks.results}
    REPORT.write_text(json.dumps(report, indent=2))
    for g, (p, t) in by_group.items():
        print(f"{g:55} {p:>4}/{t}")
    print(f"Snapshot exports: {timings}; Payer-to-Payer: {p2p}")
    print(f"{report['passed']}/{report['total']} checks passed. Written to {REPORT}")
    for r in checks.failed[:20]:
        print(f"  FAILED {r['group']} {r['check']}: expected {str(r['expected'])[:150]}, got {str(r['actual'])[:150]}")
    if checks.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
