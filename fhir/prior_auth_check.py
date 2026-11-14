"""Check prior authorization end to end (make fhir-prior-auth-check).

Plays two providers' EHRs (fhir/provider_ehr.py) against the payer's prior
authorization service in the Provider Access gateway (fhir/gateway/prior_auth.py,
Da Vinci CRD, DTR and PAS 2.1.0), and checks every answer against what the
payer's policy says it must be. The expected decisions are written here, in
SCENARIOS, not taken from the service:

  crd            CDS Hooks discovery; order-sign and order-select for a service
                 that needs prior authorization (with its questionnaire), one that
                 does not, a member without coverage on the order date, two orders
                 at once; a call without the prefetch, with the wrong hook, without
                 a token;
  dtr            $questionnaire-package by order and by questionnaire, its items;
                 an order that needs no documentation; a call without coverage;
  pas submit     one request per scenario: approved (X12 306 A1, with its
                 authorization period) or pended (A4, with the reason) on arrival;
                 refusals: a service that needs no prior authorization, a member
                 without coverage on the service date, an unknown member, another
                 provider's Organization, two items, no token; the same request
                 sent again (the same answer, no new request);
  um tables      what the payer's utilization management system holds after the
                 submissions (legacy pa_req, channel F, and pa_doc): member,
                 provider, service, diagnosis, place, date, quantity, estimate,
                 urgency, the ordering clinician's NPI, the decision, and each DTR
                 answer with its source;
  subscription   a PAS Subscription of provider X: created active after a
                 handshake; one filtered on another NPI refused; invisible to
                 another provider;
  review         the simulated reviewer (fhir/prior_auth_review.py) decides the
                 pended requests: provider X is notified of exactly its pended
                 requests, each with the reviewer's decision; $status counts them;
  pas inquire    the decision before and after the review; an unknown request;
  end to end     CDC carries the requests to the lakehouse and dbt builds them:
                 gold.fhir_prior_authorization (PDex) has each with its final
                 decision, diagnosis, date, place, quantity and priority; the
                 documentation is in silver; gold.fct_prior_auth_timeliness counts
                 them within the CMS-0057-F timeframes; published to the Patient
                 Access API, each member's app sees their own decision.

The messages of one exchange (CRD order, DTR package and answers, PAS request,
responses, inquiry, Subscription and a notification) are written to
data/fhir_prior_auth/messages/, and the questionnaires to
data/fhir_prior_auth/definitions/, for make fhir-validate-prior-auth. Writes
data/fhir_prior_auth.json and exits with status 1 if any check fails.

Every call carries a SMART access token: each provider's backend client
(provider-<id>), the Patient Access admin client for the publish, and each
member's own login for the last step. The provider's endpoint for notifications
listens on the host (port 8094), which the gateway reaches as
host.docker.internal.

Usage (from the repository root, after make fhir-publish):
    python fhir/prior_auth_check.py [--base http://localhost:8092/fhir]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg

import provider_ehr as ehr
from api_check import Checks
from smart_client import BackendClient, patient_login

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
import lakehouse  # noqa: E402

REPORT = Path("data/fhir_prior_auth.json")
OUT_DIR = Path("data/fhir_prior_auth")
DEFAULT_BASE = os.environ.get("FHIR_GATEWAY_URL", "http://localhost:8092/fhir")
PATIENT_ACCESS = os.environ.get("FHIR_SERVER_URL", "http://localhost:8090/fhir")
LEGACY_PG_DSN = os.environ.get(
    "LEGACY_PG_DSN", "host=localhost port=5433 dbname=payer_legacy user=legacy password=legacy")
LISTEN_PORT = int(os.environ.get("PRIOR_AUTH_LISTEN_PORT", "8094"))
ENDPOINT = os.environ.get("PRIOR_AUTH_ENDPOINT", f"http://host.docker.internal:{LISTEN_PORT}/notifications")
SCOPE = "system/Patient.rs system/Organization.rs system/Questionnaire.r system/Claim.cr system/Subscription.crd"
EHR_BASE = "https://ehr.example/fhir"
SITE = "https://payer-data-readiness.example/"
CRD_INFO = "http://hl7.org/fhir/us/davinci-crd/StructureDefinition/ext-coverage-information"
THERAPY_Q = SITE + "Questionnaire/outpatient-therapy|1"
IMAGING_Q = SITE + "Questionnaire/chest-imaging|1"
THERAPY_ITEMS = ["member-id", "diagnosis", "visits", "functional-goals", "prior-therapy-weeks"]
IMAGING_ITEMS = ["member-id", "diagnosis", "indication", "symptom-days", "prior-imaging-30d"]
AUTH_PERIOD_DAYS = 90
# CMS-0057-F decision timeframes.
TIMEFRAME_HOURS = {True: 72, False: 168}

# (name, provider X or Y, CPT, diagnosis, quantity, expedited, clinician's answers or None (no
# documentation), decision on arrival, decision after the review). The payer's policy:
#   97110 approved on arrival with measurable functional goals, at least 4 weeks of home
#         exercise or prior therapy and at most 12 visits; the reviewer approves a pended one
#         when functional goals are documented, and denies it otherwise;
#   71046 approved on arrival for persistent cough (21 days or more), suspected pneumonia or a
#         pre-operative evaluation, without chest imaging in the last 30 days; the reviewer
#         approves a pended one when there was no chest imaging in the last 30 days.
#   A request without documentation, or with a required item unanswered, is pended.
SCENARIOS = [
    ("therapy-criteria-met", "X", "97110", "M54.50", 12, False,
     {"functional-goals": True, "prior-therapy-weeks": 6}, "A1", "A1"),
    ("therapy-short-trial", "X", "97110", "M54.50", 12, False,
     {"functional-goals": True, "prior-therapy-weeks": 2}, "A4", "A1"),
    ("therapy-no-goals", "X", "97110", "M25.561", 10, False,
     {"functional-goals": False, "prior-therapy-weeks": 8}, "A4", "A3"),
    ("therapy-many-visits", "Y", "97110", "M54.50", 20, False,
     {"functional-goals": True, "prior-therapy-weeks": 6}, "A4", "A1"),
    ("imaging-pneumonia-expedited", "X", "71046", "J18.9", 1, True,
     {"indication": "suspected-pneumonia", "symptom-days": 3, "prior-imaging-30d": False}, "A1", "A1"),
    ("imaging-short-cough", "X", "71046", "R05.9", 1, False,
     {"indication": "persistent-cough", "symptom-days": 10, "prior-imaging-30d": False}, "A4", "A1"),
    ("imaging-repeat", "Y", "71046", "Z01.818", 1, False,
     {"indication": "pre-operative", "symptom-days": 0, "prior-imaging-30d": True}, "A4", "A3"),
    ("imaging-unanswered", "X", "71046", "J18.9", 1, False,
     {"indication": "suspected-pneumonia"}, "A4", "A3"),
    ("no-documentation", "X", "97110", "M54.50", 12, False, None, "A4", "A3"),
]
PLACE = {"97110": "11", "71046": "22"}
UNIT_ESTIMATE = {"97110": 95.0, "71046": 120.0}


class Http:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.root = self.base.removesuffix("/fhir")

    def call(self, method: str, path: str, body=None, client: BackendClient | None = None, token: str | None = None,
             root: bool = False):
        """(status, parsed JSON); HTTP errors are returned."""
        url = (self.root if root else self.base) + path
        # no-cache: HAPI answers an identical search from its cache for a minute.
        headers = {"Accept": "application/fhir+json", "Content-Type": "application/fhir+json",
                   "Cache-Control": "no-cache"}
        if client:
            headers.update(client.headers())
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            return status, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return status, raw


def review_code(cr: dict) -> str | None:
    try:
        return cr["item"][0]["adjudication"][0]["extension"][0]["extension"][0]["valueCodeableConcept"]["coding"][0]["code"]
    except (KeyError, IndexError):
        return None


def claim_response(bundle: dict) -> dict:
    return next((e["resource"] for e in bundle.get("entry", []) if e["resource"]["resourceType"] == "ClaimResponse"),
                {})


def info_of(order: dict) -> dict:
    """The coverage information of an order, as {sub-extension: value}."""
    ext = next((e for e in order.get("extension", []) if e.get("url") == CRD_INFO), {"extension": []})
    out = {}
    for sub in ext["extension"]:
        value = next(v for k, v in sub.items() if k.startswith("value"))
        out[sub["url"]] = value if not isinstance(value, dict) else value.get("reference")
    return out


def save(name: str, resource: dict) -> None:
    (OUT_DIR / "messages" / name).write_text(json.dumps(resource, indent=1))


class Gold:
    def __init__(self):
        self.con = lakehouse.connect(read_only=True)

    def resource(self, table: str, rid: str) -> dict:
        row = self.con.execute(f"SELECT resource FROM gold.{table} WHERE id = ?", [rid]).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.con.close()


def pick(gold: Gold, today: date) -> dict:
    """Two providers, a member with open coverage for each scenario (and their birth dates),
    and a member with no coverage today, chosen reproducibly."""
    rows = gold.con.execute("""
        SELECT p.id, json_extract_string(p.resource, '$.birthDate'), c.id
        FROM gold.fhir_patient p
        JOIN gold.fhir_coverage c ON json_extract_string(c.resource, '$.beneficiary.reference') = 'Patient/' || p.id
        WHERE json_extract_string(c.resource, '$.period.end') IS NULL AND json_extract_string(p.resource, '$.birthDate') IS NOT NULL
        QUALIFY count(*) OVER (PARTITION BY p.id) = 1
        ORDER BY md5('prior-auth:' || p.id) LIMIT ?""", [len(SCENARIOS)]).fetchall()
    uncovered = gold.con.execute("""
        SELECT p.id, max(c.id) FROM gold.fhir_patient p
        JOIN gold.fhir_coverage c ON json_extract_string(c.resource, '$.beneficiary.reference') = 'Patient/' || p.id
        GROUP BY p.id HAVING max(coalesce(json_extract_string(c.resource, '$.period.end'), '9999-12-31')) < ?
        ORDER BY md5('prior-auth:' || p.id) LIMIT 1""", [today.isoformat()]).fetchone()
    providers = [p for (p,) in gold.con.execute(
        "SELECT provider_id FROM gold.fhir_provider_group ORDER BY md5('prior-auth:' || provider_id) LIMIT 2"
    ).fetchall()]
    return {"members": rows, "uncovered": uncovered, "providers": dict(zip("XY", providers))}


class Provider:
    """One provider's EHR: its client, Organization and clinician."""

    def __init__(self, gold: Gold, provider_id: str):
        self.id = provider_id
        self.client = BackendClient(f"provider-{provider_id}", SCOPE)
        self.org = ehr.requestor(gold.resource("fhir_organization", provider_id))
        self.npi = self.org["identifier"][0]["value"]
        self.clinician_npi = ehr.practitioner(provider_id)["identifier"][0]["value"]

    def member(self, gold: Gold, member_id: str, coverage_id: str) -> tuple[dict, dict]:
        return (ehr.ehr_patient(gold.resource("fhir_patient", member_id), self.id),
                ehr.ehr_coverage(gold.resource("fhir_coverage", coverage_id)))


def check_crd(http: Http, checks: Checks, prov: Provider, patient, coverage, uncovered, when: str) -> dict:
    status, disc = http.call("GET", "/cds-services", root=True)
    checks.add("crd", "discovery lists the order-select and order-sign services", [200, ["order-select", "order-sign"]],
               [status, sorted(s.get("hook") for s in disc.get("services", []))] if status == 200 else [status, None])

    def hook(name, orders, pt=patient, cov=coverage, **changes):
        body = ehr.cds_request(name, pt, cov, orders, prov.id)
        body.update(changes)
        return http.call("POST", f"/cds-services/prior-auth-{name}", body, prov.client, root=True)

    therapy = ehr.order("crd-therapy", patient, coverage, prov.id, "97110", "M54.50", 12, when)
    imaging = ehr.order("crd-imaging", patient, coverage, prov.id, "71046", "J18.9", 1, when)
    visit = ehr.order("crd-visit", patient, coverage, prov.id, "99213", "I10", 1, when)
    status, ans = hook("order-sign", [therapy])
    order = ans["systemActions"][0]["resource"] if status == 200 else {}
    save("crd-order.json", order)
    i = info_of(order)
    checks.add("crd", "order-sign 97110: covered, prior authorization and clinical documentation needed, "
                      "with the physical therapy questionnaire",
               [200, "covered", "auth-needed", "clinical", THERAPY_Q],
               [status, i.get("covered"), i.get("pa-needed"), i.get("doc-needed"), i.get("questionnaire")])
    checks.add("crd", "the coverage information points at the EHR's Coverage and has an assertion ID",
               [f"Coverage/{coverage['id']}", True],
               [i.get("coverage"), bool(i.get("coverage-assertion-id"))])
    status, ans = hook("order-select", [imaging])
    i = info_of(ans["systemActions"][0]["resource"]) if status == 200 else {}
    checks.add("crd", "order-select 71046: prior authorization needed, chest imaging questionnaire",
               [200, "auth-needed", IMAGING_Q], [status, i.get("pa-needed"), i.get("questionnaire")])
    status, ans = hook("order-sign", [visit])
    i = info_of(ans["systemActions"][0]["resource"]) if status == 200 else {}
    checks.add("crd", "order-sign 99213: covered, no prior authorization, no questionnaire",
               [200, "covered", "no-auth", None], [status, i.get("covered"), i.get("pa-needed"), i.get("questionnaire")])
    status, ans = hook("order-sign", [therapy, visit])
    checks.add("crd", "two orders at once: one system action and one card each", [200, 2, 2],
               [status, len(ans.get("systemActions", [])), len(ans.get("cards", []))])
    u_patient, u_coverage = uncovered
    u_order = ehr.order("crd-uncovered", u_patient, u_coverage, prov.id, "97110", "M54.50", 12, when)
    status, ans = hook("order-sign", [u_order], pt=u_patient, cov=u_coverage)
    i = info_of(ans["systemActions"][0]["resource"]) if status == 200 else {}
    checks.add("crd", "a member without coverage on the order date: not-covered, no pa-needed",
               [200, "not-covered", None], [status, i.get("covered"), i.get("pa-needed")])
    status, _ = hook("order-sign", [therapy], prefetch={})
    checks.add("crd", "no prefetch -> 412", 412, status)
    status, _ = http.call("POST", "/cds-services/prior-auth-order-sign",
                          ehr.cds_request("order-select", patient, coverage, [therapy], prov.id), prov.client, root=True)
    checks.add("crd", "the order-select hook sent to the order-sign service -> 400", 400, status)
    status, _ = http.call("POST", "/cds-services/prior-auth-order-sign",
                          ehr.cds_request("order-sign", patient, coverage, [therapy], prov.id), root=True)
    checks.add("crd", "no access token -> 401", 401, status)
    return {"therapy": therapy, "imaging": imaging, "visit": visit}


def check_dtr(http: Http, checks: Checks, prov: Provider, coverage, orders) -> dict:
    def package(*parts):
        return http.call("POST", "/Questionnaire/$questionnaire-package",
                         {"resourceType": "Parameters", "parameter": list(parts)}, prov.client)

    def urls(body):
        return [e["resource"]["url"] + "|" + e["resource"]["version"]
                for p in body.get("parameter", []) if p["name"] == "PackageBundle" for e in p["resource"]["entry"]]

    cov = {"name": "coverage", "resource": coverage}
    status, body = package(cov, {"name": "order", "resource": orders["therapy"]})
    save("dtr-questionnaire-package-out.json", body)
    questionnaires = {}
    for p in body.get("parameter", []):
        for e in p.get("resource", {}).get("entry", []):
            questionnaires[e["resource"]["url"]] = e["resource"]
    checks.add("dtr", "package for the 97110 order: the physical therapy questionnaire", [200, [THERAPY_Q]],
               [status, urls(body)])
    q = next(iter(questionnaires.values()), {})
    checks.add("dtr", "physical therapy questionnaire items", THERAPY_ITEMS, [it["linkId"] for it in q.get("item", [])])
    status, body = package(cov, {"name": "questionnaire", "valueCanonical": IMAGING_Q})
    for p in body.get("parameter", []):
        for e in p.get("resource", {}).get("entry", []):
            questionnaires[e["resource"]["url"]] = e["resource"]
    checks.add("dtr", "package by questionnaire: chest imaging", [200, [IMAGING_Q]], [status, urls(body)])
    q = questionnaires.get(IMAGING_Q.split("|")[0], {})
    checks.add("dtr", "chest imaging questionnaire items", IMAGING_ITEMS, [it["linkId"] for it in q.get("item", [])])
    status, body = package(cov, {"name": "order", "resource": orders["visit"]})
    checks.add("dtr", "package for a 99213 order: no questionnaire, an informational outcome", [200, [], ["Outcome"]],
               [status, urls(body), [p["name"] for p in body.get("parameter", [])]])
    status, _ = package({"name": "order", "resource": orders["therapy"]})
    checks.add("dtr", "no coverage parameter -> 400", 400, status)
    defs = OUT_DIR / "definitions"
    defs.mkdir(parents=True, exist_ok=True)
    for qq in questionnaires.values():
        (defs / f"Questionnaire-{qq['id']}.json").write_text(json.dumps(qq, indent=1))
    return questionnaires


def legacy_rows(run: str) -> tuple[dict, dict]:
    with psycopg.connect(LEGACY_PG_DSN) as conn:
        cols = ["pa_id", "mbr_id", "prv_id", "svc_cd", "urgnt_flg", "dcsn_cd", "dcsn_ts", "req_amt", "dx_cd", "pos_cd",
                "svc_dt", "svc_qty", "src_cd", "sbmsn_id", "rqstr_npi"]
        reqs = {r[13]: dict(zip(cols, r)) for r in conn.execute(
            f"SELECT {', '.join(cols)} FROM pa_req WHERE sbmsn_id LIKE %s", [f"{run}-%"])}
        docs = defaultdict(dict)
        for pa_id, item, ans, src in conn.execute(
                "SELECT d.pa_id, item_id, ans_txt, ans_src FROM pa_doc d JOIN pa_req r USING (pa_id) "
                "WHERE r.sbmsn_id LIKE %s", [f"{run}-%"]):
            docs[pa_id][item] = (ans, src)
    return reqs, docs


def text_answers(answers: dict | None, scenario_cpt: str, member: str, dx: str, qty: int) -> dict:
    """The answers the payer must store: the EHR's (auto) and the clinician's (manual)."""
    out = {"member-id": (member, "A"), "diagnosis": (dx, "A")}
    if scenario_cpt == "97110":
        out["visits"] = (str(qty), "A")
    for k, v in (answers or {}).items():
        out[k] = ("true" if v is True else "false" if v is False else str(v), "M")
    return out if answers is not None else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    http = Http(args.base)
    (OUT_DIR / "messages").mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    when = (today + timedelta(days=7)).isoformat()
    run = f"R{int(time.time())}"
    checks = Checks()
    listener = ehr.Listener(LISTEN_PORT)
    gold = Gold()
    try:
        chosen = pick(gold, today)
        prov = {k: Provider(gold, pid) for k, pid in chosen["providers"].items()}
        members = {}
        for (name, who, *_), (mid, birth, cov_id) in zip(SCENARIOS, chosen["members"]):
            members[name] = (mid, birth, *prov[who].member(gold, mid, cov_id))
        u_mid, u_cov = chosen["uncovered"]
        uncovered = prov["X"].member(gold, u_mid, u_cov)
        X = prov["X"]
        first = members[SCENARIOS[0][0]]
        orders = check_crd(http, checks, X, first[2], first[3], uncovered, when)
        questionnaires = check_dtr(http, checks, X, first[3], orders)

        # --- Subscription of provider X -------------------------------------------------
        sub = {}
        for _ in range(20):  # the listener's port can take a moment to be reachable (Docker Desktop)
            status, sub = http.call("POST", "/Subscription", ehr.subscription(X.npi, ENDPOINT), X.client)
            if status != 201 or sub.get("status") == "active":
                break
            http.call("DELETE", f"/Subscription/{sub['id']}", client=X.client)
            time.sleep(0.5)
        save("pas-subscription.json", sub)
        handshakes = listener.wait(lambda b: any(p.get("valueCode") == "handshake"
                                                 for p in b["entry"][0]["resource"]["parameter"]), 10)
        checks.add("subscription", "created and active after a handshake", [201, "active", True],
                   [status, sub.get("status"), len(handshakes) >= 1])
        bad = ehr.subscription(prov["Y"].npi, ENDPOINT)
        status, _ = http.call("POST", "/Subscription", bad, X.client)
        checks.add("subscription", "filtered on another provider's NPI -> 403", 403, status)
        status, _ = http.call("GET", f"/Subscription/{sub.get('id')}", client=prov["Y"].client)
        checks.add("subscription", "another provider reading it -> 404", 404, status)

        # --- $submit ---------------------------------------------------------------------
        sent = {}
        for name, who, cpt, dx, qty, expedited, answers, on_arrival, _ in SCENARIOS:
            mid, _, patient, coverage = members[name]
            p = prov[who]
            order = ehr.order(f"order-{name}", patient, coverage, p.id, cpt, dx, qty, when, expedited)
            q = questionnaires[(THERAPY_Q if cpt == "97110" else IMAGING_Q).split("|")[0]]
            qr = None if answers is None else ehr.questionnaire_response(
                f"qr-{name}", q, patient, coverage, order, p.id, answers)
            request_id = f"{run}-{name}"
            bundle = ehr.pas_request(request_id, patient, coverage, p.org, order, qr, p.id, PLACE[cpt],
                                     UNIT_ESTIMATE[cpt] * qty, EHR_BASE)
            status, resp = http.call("POST", "/Claim/$submit", bundle, p.client)
            cr = claim_response(resp)
            sent[name] = {"request": bundle, "order": order, "qr": qr, "response": cr, "pa_id": cr.get("preAuthRef"),
                          "member": mid, "provider": p, "patient": patient, "coverage": coverage}
            checks.add("pas submit", f"{name}: {on_arrival} on arrival", [200, on_arrival], [status, review_code(cr)])
            if on_arrival == "A1":
                period = next((e["valuePeriod"] for e in cr.get("item", [{}])[0].get("extension", [])
                               if e["url"].endswith("extension-itemPreAuthPeriod")), {})
                checks.add("pas submit", f"{name}: authorized for {AUTH_PERIOD_DAYS} days from the service date",
                           [when, (date.fromisoformat(when) + timedelta(days=AUTH_PERIOD_DAYS)).isoformat()],
                           [period.get("start"), period.get("end")])
            else:
                checks.add("pas submit", f"{name}: the reason for the pend is given", True,
                           bool(cr.get("processNote")) and "review" in cr["processNote"][0].get("text", ""))
            if name == "therapy-criteria-met":
                save("pas-submit-out-approved.json", resp)
                save("dtr-questionnaire-response.json", qr)
            if name == "therapy-short-trial":
                save("pas-submit-in.json", bundle)
                save("pas-submit-out-pended.json", resp)

        # Refusals and the same request again.
        base = sent["therapy-criteria-met"]
        p, patient, coverage = base["provider"], base["patient"], base["coverage"]
        status, again = http.call("POST", "/Claim/$submit", base["request"], p.client)
        checks.add("pas submit", "the same request sent again: the same prior authorization", [200, base["pa_id"]],
                   [status, claim_response(again).get("preAuthRef")])

        def variant(name, order_kw=None, org=None, pt=patient, cov=coverage, items=1):
            kw = {"cpt": "97110", "dx": "M54.50", "qty": 12, **(order_kw or {})}
            order = ehr.order(f"order-{name}", pt, cov, p.id, kw["cpt"], kw["dx"], kw["qty"], when)
            bundle = ehr.pas_request(f"{run}-refused-{name}", pt, cov, org or p.org, order, None, p.id, "11", 100.0,
                                     EHR_BASE)
            if items == 2:
                claim = bundle["entry"][0]["resource"]
                claim["item"].append({**claim["item"][0], "sequence": 2})
            return bundle

        status, _ = http.call("POST", "/Claim/$submit", variant("no-pa", {"cpt": "99213", "qty": 1}), p.client)
        checks.add("pas submit", "a service that needs no prior authorization (99213) -> 422", 422, status)
        status, _ = http.call("POST", "/Claim/$submit", variant("uncovered", pt=uncovered[0], cov=uncovered[1]),
                              p.client)
        checks.add("pas submit", "a member without coverage on the service date -> 422", 422, status)
        stranger = json.loads(json.dumps(patient))
        stranger["identifier"][0]["value"] = "M999999999"
        stranger_cov = json.loads(json.dumps(coverage))
        stranger_cov["identifier"][0]["value"] = stranger_cov["subscriberId"] = "M999999999"
        status, _ = http.call("POST", "/Claim/$submit", variant("unknown", pt=stranger, cov=stranger_cov), p.client)
        checks.add("pas submit", "an unknown member -> 422", 422, status)
        status, _ = http.call("POST", "/Claim/$submit", variant("other-org", org=prov["Y"].org), p.client)
        checks.add("pas submit", "another provider's Organization as Claim.provider -> 403", 403, status)
        status, _ = http.call("POST", "/Claim/$submit", variant("two-items", items=2), p.client)
        checks.add("pas submit", "two items -> 422", 422, status)
        status, _ = http.call("POST", "/Claim/$submit", base["request"])
        checks.add("pas submit", "no access token -> 401", 401, status)

        # --- the UM tables --------------------------------------------------------------
        reqs, docs = legacy_rows(run)
        checks.add("um tables", "one request per scenario, nothing for the refused ones or the repeat",
                   sorted(f"{run}-{s[0]}" for s in SCENARIOS), sorted(reqs))
        for name, who, cpt, dx, qty, expedited, answers, on_arrival, _ in SCENARIOS:
            row = reqs.get(f"{run}-{name}", {})
            p = prov[who]
            expected = ["F", sent[name]["member"], p.id, cpt, dx, PLACE[cpt], when.replace("-", ""), qty,
                        round(UNIT_ESTIMATE[cpt] * qty, 2), "Y" if expedited else "N", p.clinician_npi,
                        "A" if on_arrival == "A1" else "P", on_arrival == "A1"]
            actual = [row.get("src_cd"), row.get("mbr_id"), row.get("prv_id"), row.get("svc_cd"), row.get("dx_cd"),
                      row.get("pos_cd"), row.get("svc_dt"), row.get("svc_qty"),
                      float(row["req_amt"]) if row.get("req_amt") is not None else None, row.get("urgnt_flg"),
                      row.get("rqstr_npi"), row.get("dcsn_cd"), row.get("dcsn_ts") is not None]
            checks.add("um tables", f"{name}: pa_req as received (channel, member, provider, service, diagnosis, "
                                    "place, date, quantity, estimate, urgency, clinician NPI, decision)",
                       expected, actual)
            checks.add("um tables", f"{name}: pa_doc holds the DTR answers with their source",
                       text_answers(answers, cpt, sent[name]["member"], dx, qty),
                       {k: tuple(v) for k, v in docs.get(row.get("pa_id"), {}).items()})

        # --- $inquire before the review ----------------------------------------------------
        def inquire(name, provider=None, request_id=None):
            s = sent[name]
            pr = provider or s["provider"]
            body = ehr.pas_inquiry(request_id or f"{run}-{name}", s["patient"], s["coverage"], pr.org, s["order"],
                                   pr.id, PLACE[s["order"]["code"]["coding"][0]["code"]], EHR_BASE, when)
            return body, *http.call("POST", "/Claim/$inquire", body, pr.client)

        _, status, resp = inquire("therapy-short-trial")
        checks.add("pas inquire", "therapy-short-trial before the review: A4", [200, "A4"],
                   [status, review_code(claim_response(resp))])
        _, status, _ = inquire("therapy-short-trial", request_id=f"{run}-never-sent")
        checks.add("pas inquire", "an identifier never sent -> 404", 404, status)

        # --- the review and the notifications ----------------------------------------------
        pended = [sent[s[0]]["pa_id"] for s in SCENARIOS if s[7] == "A4"]
        out = subprocess.run([sys.executable, "fhir/prior_auth_review.py", *pended], capture_output=True, text=True)
        checks.add("review", "the reviewer decides every pended request", [0, len(pended)],
                   [out.returncode, int(out.stdout.split()[1]) if out.returncode == 0 else None])
        x_pended = {sent[s[0]]["pa_id"]: s[8] for s in SCENARIOS if s[7] == "A4" and s[1] == "X"}

        def events(b):
            return any(p.get("valueCode") == "event-notification" for p in b["entry"][0]["resource"]["parameter"])

        deadline = time.time() + 30
        while time.time() < deadline and len(listener.wait(events, 2)) < len(x_pended):
            pass
        time.sleep(3)  # anything more would be a notification that should not have come
        notified = {}
        for b in listener.wait(events, 0):
            for e in b["entry"][1:]:
                cr = claim_response(e["resource"])
                notified[cr.get("preAuthRef")] = review_code(cr)
        save("pas-notification.json", next(b for b in listener.wait(events, 0)) if notified else {})
        checks.add("review", "provider X is notified of exactly its pended requests, each with the reviewer's "
                             "decision", dict(sorted(x_pended.items())), dict(sorted(notified.items())))
        status, st = http.call("GET", f"/Subscription/{sub.get('id')}/$status", client=X.client)
        count = next((p.get("valueString") for e in st.get("entry", [])
                      for p in e["resource"]["parameter"] if p["name"] == "events-since-subscription-start"), None)
        checks.add("review", "$status counts the events", [200, str(len(x_pended))], [status, count])

        # --- $inquire after the review ------------------------------------------------------
        for name, *_rest, after in SCENARIOS:
            if _rest[6] != "A4":
                continue
            body, status, resp = inquire(name)
            checks.add("pas inquire", f"{name} after the review: {after}", [200, after],
                       [status, review_code(claim_response(resp))])
            if name == "therapy-short-trial":
                save("pas-inquire-in.json", body)
                save("pas-inquire-out.json", resp)

        # --- end to end: lakehouse, gold, the member's app --------------------------------
        gold.close()
        for label, cmd, cwd in [
                ("CDC sync", [sys.executable, "ingestion/cdc_legacy.py", "sync"], "."),
                ("dbt build of the prior authorization models and their tests",
                 ["dbt", "build", "--profiles-dir", ".", "--select", "cdc_current__pa_req+", "cdc_current__pa_doc+"],
                 "dbt")]:
            done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            checks.add("end to end", f"{label} succeeds", 0, done.returncode)
            if done.returncode != 0:
                print(done.stdout[-3000:], done.stderr[-3000:])
        gold = Gold()
        pa_ids = {s[0]: sent[s[0]]["pa_id"] for s in SCENARIOS}
        published = []
        for name, who, cpt, dx, qty, expedited, answers, _, after in SCENARIOS:
            eob = gold.resource("fhir_prior_authorization", pa_ids[name]) or {}
            published.append(eob)
            it = (eob.get("item") or [{}])[0]
            checks.add("end to end", f"{name}: in gold (PDex) with its final decision, diagnosis, date, place, "
                                     "quantity and priority",
                       [after, dx, when, PLACE[cpt], qty, "stat" if expedited else "normal"],
                       [review_code(eob),
                        (eob.get("diagnosis") or [{}])[0].get("diagnosisCodeableConcept", {}).get("coding", [{}])[0].get("code"),
                        it.get("servicedDate"), it.get("locationCodeableConcept", {}).get("coding", [{}])[0].get("code"),
                        it.get("quantity", {}).get("value"),
                        eob.get("priority", {}).get("coding", [{}])[0].get("code")])
        ids = list(pa_ids.values())
        ph = ", ".join("?" * len(ids))
        n_docs = gold.con.execute(f"SELECT count(*) FROM silver.stg_legacy__prior_auth_documents "
                                  f"WHERE prior_auth_id IN ({ph})", ids).fetchone()[0]
        checks.add("end to end", "the DTR answers are in silver",
                   sum(len(text_answers(s[6], s[2], sent[s[0]]["member"], s[3], s[4])) for s in SCENARIOS), n_docs)
        timely = gold.con.execute(f"SELECT count(*) FILTER (WHERE within_timeframe), count(*) "
                                  f"FROM gold.fct_prior_auth_timeliness WHERE prior_auth_id IN ({ph})", ids).fetchone()
        checks.add("end to end", "every request decided within the CMS-0057-F timeframe (gold)",
                   [len(ids), len(ids)], list(timely))
        admin = BackendClient("admin-patient-access", "system/*.cruds")
        status, tx = Http(PATIENT_ACCESS).call("POST", "", {"resourceType": "Bundle", "type": "transaction", "entry": [
            {"resource": r, "request": {"method": "PUT", "url": f"ExplanationOfBenefit/{r['id']}"}}
            for r in published if r]}, admin)
        checks.add("end to end", "published to the Patient Access API", [200, len(ids)],
                   [status, sum(e.get("response", {}).get("status", "").startswith(("200", "201"))
                                for e in tx.get("entry", []))])
        for name in ("therapy-criteria-met", "therapy-no-goals"):
            mid, birth = members[name][:2]
            token = patient_login(mid, birth, "launch/patient patient/ExplanationOfBenefit.rs")["access_token"]
            pa = Http(PATIENT_ACCESS)
            status, eob = pa.call("GET", f"/ExplanationOfBenefit/{pa_ids[name]}", token=token)
            s2, bundle = pa.call("GET", "/ExplanationOfBenefit?use=preauthorization&_count=100", token=token)
            listed = [e["resource"]["id"] for e in bundle.get("entry", [])] if s2 == 200 else []
            after = next(s[8] for s in SCENARIOS if s[0] == name)
            checks.add("end to end", f"{name}: the member's app reads the decision ({after}) and lists it",
                       [200, after, True], [status, review_code(eob), pa_ids[name] in listed])
    finally:
        listener.close()
        gold.close()

    by_group = defaultdict(lambda: [0, 0])
    for r in checks.results:
        by_group[r["group"]][0] += r["passed"]
        by_group[r["group"]][1] += 1
    report = {"gateway": args.base, "run": run, "service_date": when,
              "providers": {k: v.id for k, v in prov.items()},
              "requests": {s[0]: {"pa_id": sent[s[0]]["pa_id"], "member": sent[s[0]]["member"], "on_arrival": s[7],
                                  "after_review": s[8]} for s in SCENARIOS},
              "passed": sum(r["passed"] for r in checks.results), "total": len(checks.results),
              "groups": {g: {"passed": p, "total": t} for g, (p, t) in by_group.items()},
              "failed": checks.failed, "checks": checks.results}
    REPORT.write_text(json.dumps(report, indent=2, default=str))
    for g, (p, t) in by_group.items():
        print(f"{g:14} {p}/{t}")
    print(f"{report['passed']} of {report['total']} checks passed; written to {REPORT}")
    for r in checks.failed:
        print(f"FAILED [{r['group']}] {r['check']}: expected {r['expected']}, got {r['actual']}")
    if checks.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
