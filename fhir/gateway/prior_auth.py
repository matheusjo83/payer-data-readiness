"""Prior authorization, the payer side: Da Vinci CRD, DTR and PAS 2.1.0.

Part of the Provider Access gateway (gateway.py, port 8092): every call needs a
provider's SMART Backend Services access token, and acts for the provider the
token was issued to (fhirUser Organization/<id>).

  GET  /cds-services                                  CDS Hooks discovery (CRD)
  POST /cds-services/prior-auth-order-select          CRD order-select and order-sign:
  POST /cds-services/prior-auth-order-sign            for each draft ServiceRequest, the
      coverage information (CRD ext-coverage-information): covered or not on the
      order date, prior authorization needed or not, and, when it is, the
      clinical documentation needed and the DTR questionnaire to fill. Returned
      as a systemAction that updates the order, and a card.
  POST /fhir/Questionnaire/$questionnaire-package     DTR: the questionnaires of the
      orders (or the one named), each in a DTR Questionnaire Package Bundle.
  POST /fhir/Claim/$submit                             PAS: a PAS request Bundle. The
      request is written to the payer's utilization management system, the legacy
      tables pa_req (channel F) and pa_doc (the DTR answers), from where CDC
      carries it to the lakehouse and the Patient Access API (PDex). Answer: a
      PAS response Bundle, approved (X12 306 A1) when the documentation meets the
      service's criteria, pended (A4) otherwise. A request is never denied here:
      only a reviewer denies (fhir/prior_auth_review.py, on the UM system).
  POST /fhir/Claim/$inquire                            PAS: the current decision of a
      request, found by the provider's Claim identifier.
  POST /fhir/Subscription, GET|DELETE /fhir/Subscription/<id>,
  GET  /fhir/Subscription/<id>/$status                 PAS subscriptions (R4
      backport, rest-hook, topic PASSubscriptionTopic, filter
      orgIdentifier=<the provider's NPI>). A handshake is sent when one is
      created; when a pended request of the provider is decided, an
      event-notification with the PAS response Bundle (full-resource) or its
      reference (id-only).

Rules (RULES): 97110 (physical therapy, therapeutic exercise) and 71046 (chest
X-ray, two views) need prior authorization, each with its questionnaire and
criteria; every other service needs none (CRD no-auth, and $submit answers 422).
A request carries one item, the service, with its diagnosis (ICD-10-CM), place
of service, service date, quantity and estimated cost (item.net). The member is
the one whose member ID the Patient or the Coverage carries, with coverage from
this payer on the service date.

Codes of X12 code lists (service type 1365, request type 1525, certification
type 1322, entity 98, review decision 306) are the ones the PAS examples use;
X12 code lists are licensed, so nothing here can verify them.

The subscriptions and the record of what was notified live in memory: a
restart forgets them (decisions made before it are not notified).
"""

import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

PAS = "http://hl7.org/fhir/us/davinci-pas/"
CRD = "http://hl7.org/fhir/us/davinci-crd/"
DTR = "http://hl7.org/fhir/us/davinci-dtr/"
SDC = "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
BACKPORT = "http://hl7.org/fhir/uv/subscriptions-backport/StructureDefinition/"
SITE = "https://payer-data-readiness.example/"
X12 = "https://codesystem.x12.org/005010/"
CPT = "http://www.ama-assn.org/go/cpt"
ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"
NPI = "http://hl7.org/fhir/sid/us-npi"
POS = "https://www.cms.gov/Medicare/Coding/place-of-service-codes/Place_of_Service_Code_Set"
MEMBER_ID_SYSTEM = SITE + "member-id"
PAS_TOPIC = PAS + "SubscriptionTopic/PASSubscriptionTopic"
CRD_TEMP = CRD + "CodeSystem/temp"
INDICATION_CS = SITE + "CodeSystem/chest-imaging-indication"
AUTH_PERIOD_DAYS = 90
LEGACY_PG_DSN = os.environ.get(
    "LEGACY_PG_DSN", "host=localhost port=5433 dbname=payer_legacy user=legacy password=legacy")


class PAError(Exception):
    """Answered as an OperationOutcome with this HTTP status."""

    def __init__(self, status: int, message: str, code: str = "processing"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def instant(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def narrative(text: str) -> dict:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml"><p>{text}</p></div>'}


def codes(concept: dict | None, system: str) -> list[str]:
    return [c.get("code") for c in (concept or {}).get("coding", []) if c.get("system") == system and c.get("code")]


# --- Rules and questionnaires -------------------------------------------------------

def item(link_id: str, text: str, type_: str, **extra) -> dict:
    return {"linkId": link_id, "text": text, "type": type_, "required": True, **extra}


MEMBER_ID_ITEM = item("member-id", "Member ID", "string", readOnly=True, extension=[{
    "url": SDC + "sdc-questionnaire-initialExpression",
    "valueExpression": {"language": "text/fhirpath",
                        "expression": "%patient.identifier.where(type.coding.code = 'MB').value"}}])
DIAGNOSIS_ITEM = item("diagnosis", "Primary diagnosis (ICD-10-CM)", "string")

INDICATIONS = {"persistent-cough": "Cough for three weeks or more", "suspected-pneumonia": "Suspected pneumonia",
               "pre-operative": "Pre-operative evaluation", "other": "Other"}


def answers_of(qr: dict | None) -> dict:
    """linkId -> (answer as text, source code A/O/M), for every answered item."""
    out = {}

    def walk(items):
        for it in items or []:
            for ans in it.get("answer", [])[:1]:
                value = next((v for k, v in ans.items() if k.startswith("value")), None)
                if isinstance(value, dict):
                    value = value.get("code") or value.get("value") or value.get("display")
                if isinstance(value, bool):
                    value = "true" if value else "false"
                origin = next((e for e in ans.get("extension", []) if e.get("url") == DTR + "StructureDefinition/information-origin"), {})
                source = next((e.get("valueCode") for e in origin.get("extension", []) if e.get("url") == "source"), "manual")
                out[it["linkId"]] = (None if value is None else str(value)[:200],
                                     {"auto": "A", "override": "O"}.get(source, "M"))
                walk(ans.get("item"))
            walk(it.get("item"))

    walk((qr or {}).get("item"))
    return out


def as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def therapy_criteria(a: dict) -> list[str]:
    unmet = []
    if a.get("functional-goals") != "true":
        unmet.append("measurable functional goals are not documented")
    if (as_int(a.get("prior-therapy-weeks")) or 0) < 4:
        unmet.append("less than 4 weeks of home exercise or prior therapy")
    if (as_int(a.get("visits")) or 99) > 12:
        unmet.append("more than 12 visits requested")
    return unmet


def imaging_criteria(a: dict) -> list[str]:
    unmet = []
    indication = a.get("indication")
    if indication not in ("persistent-cough", "suspected-pneumonia", "pre-operative"):
        unmet.append("the indication is not one the policy covers")
    if indication == "persistent-cough" and (as_int(a.get("symptom-days")) or 0) < 21:
        unmet.append("cough for less than 21 days")
    if a.get("prior-imaging-30d") != "false":
        unmet.append("chest imaging in the last 30 days, or not answered")
    return unmet


def therapy_review(a: dict) -> str:
    """The reviewer's decision on a pended request (fhir/prior_auth_review.py)."""
    return "A" if a.get("functional-goals") == "true" else "D"


def imaging_review(a: dict) -> str:
    return "A" if a.get("prior-imaging-30d") == "false" else "D"


class Rule:
    def __init__(self, code, display, service_type, questionnaire_id, title, items, criteria, review):
        self.code, self.display, self.service_type = code, display, service_type
        self.questionnaire_url = SITE + "Questionnaire/" + questionnaire_id
        self.questionnaire_id, self.title, self.items = questionnaire_id, title, items
        self.criteria, self.review = criteria, review
        self.version = "1"

    @property
    def canonical(self) -> str:
        return f"{self.questionnaire_url}|{self.version}"

    def questionnaire(self) -> dict:
        return {
            "resourceType": "Questionnaire", "id": self.questionnaire_id,
            "meta": {"profile": [DTR + "StructureDefinition/dtr-std-questionnaire"]},
            "text": narrative(f"{self.title}: documentation for prior authorization of CPT {self.code}"),
            "extension": [{"url": SDC + "sdc-questionnaire-launchContext", "extension": [
                {"url": "name", "valueCoding": {"system": "http://hl7.org/fhir/uv/sdc/CodeSystem/launchContext",
                                                "code": "patient"}},
                {"url": "type", "valueCode": "Patient"}]}],
            "url": self.questionnaire_url, "version": self.version,
            "name": "".join(w.capitalize() for w in self.questionnaire_id.split("-")), "title": self.title,
            "status": "active", "subjectType": ["Patient"],
            "item": self.items,
        }

    def unmet(self, answers: dict) -> list[str]:
        missing = [it["linkId"] for it in self.items if it.get("required") and answers.get(it["linkId"]) is None]
        if missing:
            return [f"not answered: {', '.join(missing)}"]
        return self.criteria(answers)


RULES = {r.code: r for r in [
    Rule("97110", "Therapeutic exercise (physical therapy)", ("PT", "Physical Therapy"), "outpatient-therapy",
         "Outpatient physical therapy",
         [MEMBER_ID_ITEM, DIAGNOSIS_ITEM, item("visits", "Visits requested", "integer"),
          item("functional-goals", "Measurable functional goals documented", "boolean"),
          item("prior-therapy-weeks", "Weeks of home exercise or prior therapy tried", "integer")],
         therapy_criteria, therapy_review),
    Rule("71046", "Chest X-ray, 2 views", ("4", "Diagnostic X-Ray"), "chest-imaging", "Chest imaging",
         [MEMBER_ID_ITEM, DIAGNOSIS_ITEM,
          item("indication", "Clinical indication", "choice",
               answerOption=[{"valueCoding": {"system": INDICATION_CS, "code": k, "display": v}}
                             for k, v in INDICATIONS.items()]),
          item("symptom-days", "Days with symptoms", "integer"),
          item("prior-imaging-30d", "Chest imaging in the last 30 days", "boolean")],
         imaging_criteria, imaging_review),
]}
RULES_BY_QUESTIONNAIRE = {r.questionnaire_url: r for r in RULES.values()}


# --- The payer's data ------------------------------------------------------------------

def member_id_of(patient: dict | None, coverage: dict | None) -> str | None:
    for res in (patient, coverage):
        for ident in (res or {}).get("identifier", []):
            if ident.get("system") == MEMBER_ID_SYSTEM and ident.get("value"):
                return ident["value"]
    sub = (coverage or {}).get("subscriberId")
    return sub or None


def coverage_on(upstream, member_id: str, day: date) -> dict | None:
    """This payer's coverage of the member on that day (Coverage on the provider server)."""
    for cov in upstream.search(f"Coverage?beneficiary=Patient/{member_id}"):
        period = cov.get("period", {})
        start = date.fromisoformat(period["start"][:10]) if period.get("start") else date.min
        end = date.fromisoformat(period["end"][:10]) if period.get("end") else date.max
        if start <= day <= end and cov.get("status") == "active":
            return cov
    return None


def provider_npi(upstream, provider_id: str) -> str | None:
    org = upstream.read("Organization", provider_id) or {}
    return next((i.get("value") for i in org.get("identifier", []) if i.get("system") == NPI), None)


def connect():
    import psycopg  # only the provider gateway needs it
    return psycopg.connect(LEGACY_PG_DSN)


REQUEST_COLS = ("pa_id", "mbr_id", "prv_id", "svc_cd", "urgnt_flg", "req_ts", "dcsn_ts", "dcsn_cd", "req_amt",
                "dx_cd", "pos_cd", "svc_dt", "svc_qty", "src_cd", "sbmsn_id", "rqstr_npi", "upd_ts")


def load_request(cur, where: str, params: list) -> dict | None:
    row = cur.execute(f"SELECT {', '.join(REQUEST_COLS)} FROM pa_req WHERE {where}", params).fetchone()
    return dict(zip(REQUEST_COLS, row)) if row else None


def load_answers(cur, pa_id: str) -> dict:
    return {i: (a, s) for i, a, s in cur.execute(
        "SELECT item_id, ans_txt, ans_src FROM pa_doc WHERE pa_id = %s", [pa_id])}


# --- CRD -------------------------------------------------------------------------------

CDS_SERVICES = {
    "prior-auth-order-select": "order-select",
    "prior-auth-order-sign": "order-sign",
}


def cds_discovery() -> dict:
    return {"services": [{
        "hook": hook, "id": sid,
        "title": "Coverage requirements (Da Vinci CRD)",
        "description": "Whether the payer covers the order and needs prior authorization, and the documentation "
                       "to collect with DTR",
        "prefetch": {"patient": "Patient/{{context.patientId}}",
                     "coverage": "Coverage?patient={{context.patientId}}&status=active"},
    } for sid, hook in CDS_SERVICES.items()]}


def bundle_resources(bundle: dict | None, rtype: str | None = None) -> list[dict]:
    if not bundle:
        return []
    if bundle.get("resourceType") != "Bundle":
        return [bundle] if rtype in (None, bundle.get("resourceType")) else []
    return [e["resource"] for e in bundle.get("entry", [])
            if e.get("resource") and rtype in (None, e["resource"].get("resourceType"))]


def order_date(order: dict) -> date:
    for key in ("occurrenceDateTime", "authoredOn"):
        if order.get(key):
            return date.fromisoformat(order[key][:10])
    return now().date()


def coverage_information(upstream, order: dict, ehr_coverage: dict | None, patient: dict | None,
                         assertion: str) -> tuple[dict, str]:
    """The CRD coverage-information extension for one order, and a summary line."""
    cpt = (codes(order.get("code"), CPT) or [None])[0]
    rule = RULES.get(cpt)
    member = member_id_of(patient, ehr_coverage)
    day = order_date(order)
    covered = bool(member and coverage_on(upstream, member, day))
    ext = [{"url": "coverage", "valueReference": {
        "reference": f"Coverage/{ehr_coverage['id']}" if ehr_coverage and ehr_coverage.get("id") else "Coverage"}},
        {"url": "covered", "valueCode": "covered" if covered else "not-covered"}]
    if not covered:
        summary = "Not covered: no coverage with this payer on the order date"
    elif rule:
        # No doc-purpose (withpa): CRD 2.1.0's invariant crd-ci-q4 is written so that the
        # validator fails it whenever doc-purpose is present and pa-needed is not
        # 'satisfied' (its first operand is a collection, not a boolean).
        ext += [{"url": "pa-needed", "valueCode": "auth-needed"},
                {"url": "doc-needed", "valueCode": "clinical"},
                {"url": "questionnaire", "valueCanonical": rule.canonical}]
        summary = f"Prior authorization needed for CPT {cpt}: complete '{rule.title}' (DTR) and submit (PAS)"
    else:
        ext.append({"url": "pa-needed", "valueCode": "no-auth"})
        summary = f"Covered; no prior authorization needed for CPT {cpt or 'this service'}"
    ext += [{"url": "date", "valueDate": now().date().isoformat()},
            {"url": "coverage-assertion-id", "valueString": assertion}]
    return {"url": CRD + "StructureDefinition/ext-coverage-information", "extension": ext}, summary


def cds_service(upstream, service_id: str, request: dict) -> dict:
    hook = CDS_SERVICES.get(service_id)
    if hook is None:
        raise PAError(404, f"Unknown CDS service {service_id}", "not-found")
    if request.get("hook") != hook:
        raise PAError(400, f"{service_id} answers the {hook} hook, not {request.get('hook')}", "invalid")
    context, prefetch = request.get("context") or {}, request.get("prefetch") or {}
    patient = prefetch.get("patient")
    coverages = bundle_resources(prefetch.get("coverage"), "Coverage")
    if patient is None or not coverages:
        raise PAError(412, "The prefetch must hold the patient and their coverage (this service does not query "
                           "the EHR)", "required")
    orders = bundle_resources(context.get("draftOrders"), "ServiceRequest")
    if hook == "order-select":
        selections = set(context.get("selections") or [])
        orders = [o for o in orders if f"ServiceRequest/{o.get('id')}" in selections] or orders
    cards, actions = [], []
    for n, order in enumerate(orders, 1):
        info, summary = coverage_information(upstream, order, coverages[0], patient,
                                             f"{request.get('hookInstance', uuid.uuid4())}-{n}")
        updated = {**order, "extension": [e for e in order.get("extension", []) if e.get("url") != info["url"]] + [info]}
        actions.append({"type": "update", "description": summary, "resource": updated})
        cards.append({"uuid": str(uuid.uuid4()), "summary": summary[:140],
                      "indicator": "warning" if "needed for" in summary else "info",
                      "source": {"label": "Payer Data Readiness (synthetic payer)"}})
    return {"cards": cards, "systemActions": actions}


# --- DTR -------------------------------------------------------------------------------

def questionnaire_package(params: dict) -> dict:
    if params.get("resourceType") != "Parameters":
        raise PAError(400, "$questionnaire-package takes a Parameters resource", "invalid")
    parts = params.get("parameter", [])
    if not any(p.get("name") == "coverage" and p.get("resource") for p in parts):
        raise PAError(400, "$questionnaire-package needs the coverage parameter (DTR)", "required")
    wanted = [p.get("valueCanonical") for p in parts if p.get("name") == "questionnaire" and p.get("valueCanonical")]
    rules = []
    if wanted:
        for canonical in wanted:
            rule = RULES_BY_QUESTIONNAIRE.get(canonical.split("|")[0])
            if rule is None:
                raise PAError(404, f"Unknown questionnaire {canonical}", "not-found")
            rules.append(rule)
    else:
        for p in parts:
            if p.get("name") == "order" and p.get("resource"):
                rule = RULES.get((codes(p["resource"].get("code"), CPT) or [None])[0])
                if rule and rule not in rules:
                    rules.append(rule)
    out = [{"name": "PackageBundle", "resource": {
        "resourceType": "Bundle", "meta": {"profile": [DTR + "StructureDefinition/DTR-QPackageBundle"]},
        "type": "collection", "timestamp": instant(now()),
        "entry": [{"fullUrl": rule.questionnaire_url, "resource": rule.questionnaire()}]}} for rule in rules]
    if not out:
        out = [{"name": "Outcome", "resource": {"resourceType": "OperationOutcome", "issue": [{
            "severity": "information", "code": "not-found",
            "diagnostics": "No documentation is needed for these orders"}]}}]
    return {"resourceType": "Parameters", "meta": {"profile": [DTR + "StructureDefinition/dtr-qpackage-output-parameters"]},
            "parameter": out}


# --- PAS -------------------------------------------------------------------------------

class Submission:
    """A PAS request (or inquiry) Bundle, with its references resolved inside it."""

    def __init__(self, bundle: dict, claim_profile: str):
        if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "collection":
            raise PAError(400, "The body must be a PAS Bundle of type collection", "invalid")
        self.bundle = bundle
        self.by_ref = {}
        for e in bundle.get("entry", []):
            res = e.get("resource") or {}
            if res.get("resourceType") and res.get("id"):
                self.by_ref[f"{res['resourceType']}/{res['id']}"] = res
            if e.get("fullUrl"):
                self.by_ref[e["fullUrl"]] = res
        first = (bundle.get("entry") or [{}])[0].get("resource") or {}
        if first.get("resourceType") != "Claim":
            raise PAError(400, "The first entry of a PAS Bundle must be the Claim", "invalid")
        self.claim = first
        if self.claim.get("use") != "preauthorization":
            raise PAError(400, "Claim.use must be preauthorization", "invalid")
        self.claim_profile = claim_profile

    def resolve(self, ref: dict | None) -> dict | None:
        target = (ref or {}).get("reference", "")
        if target in self.by_ref:
            return self.by_ref[target]
        tail = "/".join(target.split("/")[-2:])
        return self.by_ref.get(tail)

    def identifier(self) -> str:
        ids = [i.get("value") for i in self.claim.get("identifier", []) if i.get("value")]
        if not ids:
            raise PAError(400, "The Claim needs an identifier (the provider's request number)", "required")
        return ids[0][:40]

    def check_provider(self, upstream, provider_id: str) -> dict:
        """The requesting Organization, which must be the caller (same NPI)."""
        org = self.resolve(self.claim.get("provider"))
        npi = provider_npi(upstream, provider_id)
        org_npis = [i.get("value") for i in (org or {}).get("identifier", []) if i.get("system") == NPI]
        if not org or org.get("resourceType") != "Organization" or npi not in org_npis:
            raise PAError(403, "Claim.provider must be the requesting provider's Organization (its NPI)", "forbidden")
        return org

    def member(self, upstream) -> tuple[str, dict]:
        patient = self.resolve(self.claim.get("patient"))
        coverage = next((self.resolve(i.get("coverage")) for i in self.claim.get("insurance", [])
                         if i.get("focal")), None)
        member = member_id_of(patient, coverage)
        if not member or upstream.read("Patient", member) is None:
            raise PAError(422, "The member is not known to this payer (member ID on the Patient or Coverage)",
                          "not-found")
        return member, patient


def service_of(sub: Submission) -> dict:
    items = sub.claim.get("item", [])
    if len(items) != 1:
        raise PAError(422, "This payer takes one service per prior authorization request", "not-supported")
    it = items[0]
    cpt = (codes(it.get("productOrService"), CPT) or [None])[0]
    if cpt not in RULES:
        raise PAError(422, f"No prior authorization is required for CPT {cpt} (see the CRD coverage information)",
                      "business-rule")
    when = it.get("servicedDate") or (it.get("servicedPeriod") or {}).get("start")
    if not when:
        raise PAError(400, "The item needs servicedDate", "required")
    dx_by_seq = {d.get("sequence"): (codes(d.get("diagnosisCodeableConcept"), ICD10CM) or [None])[0]
                 for d in sub.claim.get("diagnosis", [])}
    dx = dx_by_seq.get((it.get("diagnosisSequence") or [1])[0]) or next((v for v in dx_by_seq.values() if v), None)
    pos = (codes(it.get("locationCodeableConcept"), POS) or [None])[0]
    amount = (it.get("net") or {}).get("value")
    if amount is None and it.get("unitPrice"):
        amount = it["unitPrice"].get("value", 0) * (it.get("quantity") or {}).get("value", 1)
    if not dx or not pos or amount is None:
        raise PAError(400, "The request needs a diagnosis (ICD-10-CM), the place of service and the estimated "
                           "cost (item.net)", "required")
    return {"svc_cd": cpt, "svc_dt": date.fromisoformat(when[:10]), "dx_cd": dx[:8], "pos_cd": pos[:2],
            "svc_qty": int((it.get("quantity") or {}).get("value", 1)), "req_amt": round(float(amount), 2)}


def documentation(sub: Submission) -> dict | None:
    for info in sub.claim.get("supportingInfo", []):
        res = sub.resolve(info.get("valueReference"))
        if res and res.get("resourceType") == "QuestionnaireResponse":
            return res
    return None


def requester_npi(sub: Submission, qr: dict | None) -> str | None:
    candidates = [sub.resolve(t.get("provider")) for t in sub.claim.get("careTeam", [])]
    candidates = [sub.resolve(r.get("practitioner")) if r and r.get("resourceType") == "PractitionerRole" else r
                  for r in candidates]
    if qr:
        candidates.append(sub.resolve(qr.get("author")))
    for res in candidates:
        if res and res.get("resourceType") == "Practitioner":
            for ident in res.get("identifier", []):
                if ident.get("system") == NPI:
                    return ident.get("value")
    return None


REVIEW = {"A": ("A1", "Certified in total"), "D": ("A3", "Not Certified"), "P": ("A4", "Pending")}


def insurer() -> dict:
    return {"resourceType": "Organization", "id": "payer",
            "meta": {"profile": [PAS + "StructureDefinition/profile-insurer"]},
            "text": narrative("Payer Data Readiness (synthetic payer)"),
            "identifier": [{"system": SITE + "payer-id", "value": "payer"}],
            "active": True, "type": [{"coding": [{"system": X12 + "98", "code": "PR"}]}],
            "name": "Payer Data Readiness (synthetic)"}


def response_bundle(base: str, req: dict, sub: Submission | None, inquiry: bool = False,
                    note: str | None = None) -> dict:
    """The PAS response (or inquiry response) Bundle for the request's current state."""
    code, display = REVIEW[req["dcsn_cd"]]
    patient_ref = (sub.claim.get("patient") if sub else None) or {"reference": f"Patient/{req['mbr_id']}"}
    provider_ref = (sub.claim.get("provider") if sub else None) or {"reference": f"Organization/{req['prv_id']}"}
    svc_dt = datetime.strptime(req["svc_dt"], "%Y%m%d").date()
    item_ext = []
    if req["dcsn_cd"] == "A":
        item_ext = [{"url": PAS + "StructureDefinition/extension-itemPreAuthIssueDate",
                     "valueDate": req["dcsn_ts"].date().isoformat()},
                    {"url": PAS + "StructureDefinition/extension-itemPreAuthPeriod", "valuePeriod": {
                        "start": svc_dt.isoformat(), "end": (svc_dt + timedelta(days=AUTH_PERIOD_DAYS)).isoformat()}}]
    created = req["dcsn_ts"] if req["dcsn_cd"] != "P" else now()
    created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    claim_id = (sub.claim.get("id") if sub else None) or req["pa_id"]
    cr = {
        "resourceType": "ClaimResponse", "id": f"{'inquiry-' if inquiry else ''}{req['pa_id']}",
        "meta": {"profile": [PAS + "StructureDefinition/" + ("profile-claiminquiryresponse" if inquiry
                                                              else "profile-claimresponse")]},
        "text": narrative(f"Prior authorization {req['pa_id']}, CPT {req['svc_cd']}: {display}"),
        # The provider's request identifier, as it sent it.
        "identifier": (sub.claim.get("identifier") or [])[:1] if sub else
        [{"system": SITE + f"provider/{req['prv_id']}/prior-auth-request", "value": req["sbmsn_id"]}],
        "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
        "use": "preauthorization",
        "patient": patient_ref, "created": instant(created),
        "insurer": {"reference": "Organization/payer"},
        "requestor": provider_ref,
        "request": {"reference": f"Claim/{claim_id}"},
        "outcome": "complete",
        "preAuthRef": req["pa_id"],
        "item": [{**({"extension": item_ext} if item_ext else {}), "itemSequence": 1, "adjudication": [{
            "extension": [{"url": PAS + "StructureDefinition/extension-reviewAction", "extension": [
                {"url": PAS + "StructureDefinition/extension-reviewActionCode",
                 "valueCodeableConcept": {"coding": [{"system": X12 + "306", "code": code, "display": display}]}}]}],
            "category": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adjudication",
                                     "code": "submitted"}]}}]}],
    }
    if note:
        cr["processNote"] = [{"number": 1, "type": "display", "text": note[:500]}]
    entries = [cr]
    if sub:
        for ref in (sub.claim.get("patient"), sub.claim.get("provider")):
            res = sub.resolve(ref)
            if res and res not in entries:
                entries.append(res)
    entries.append(insurer())
    profile = "profile-pas-inquiry-response-bundle" if inquiry else "profile-pas-response-bundle"
    out = {"resourceType": "Bundle", "id": str(uuid.uuid4()),
           "meta": {"profile": [PAS + "StructureDefinition/" + profile]},
           "type": "collection", "timestamp": instant(created),
           "entry": [{"fullUrl": f"{base}/{r['resourceType']}/{r['id']}", "resource": r} for r in entries]}
    if sub and sub.bundle.get("identifier"):
        out["identifier"] = sub.bundle["identifier"]
    return out


def submit(upstream, base: str, provider_id: str, body: dict, service: "PriorAuthService") -> dict:
    sub = Submission(body, "profile-claim")
    request_id = sub.identifier()
    sub.check_provider(upstream, provider_id)
    member, _ = sub.member(upstream)
    svc = service_of(sub)
    if coverage_on(upstream, member, svc["svc_dt"]) is None:
        raise PAError(422, f"The member has no coverage with this payer on {svc['svc_dt']}", "business-rule")
    rule = RULES[svc["svc_cd"]]
    qr = documentation(sub)
    answers = answers_of(qr) if qr and qr.get("questionnaire", "").split("|")[0] == rule.questionnaire_url else {}
    unmet = rule.unmet({k: v for k, (v, _) in answers.items()}) if qr else ["no documentation (DTR) attached"]
    decision = "P" if unmet else "A"
    urgent = "Y" if "stat" in codes(sub.claim.get("priority"), "http://terminology.hl7.org/CodeSystem/processpriority") \
        else "N"
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('pa_req'))")
        existing = load_request(cur, "prv_id = %s AND sbmsn_id = %s AND src_cd = 'F'", [provider_id, request_id])
        if existing:  # the same request sent again: the current answer, nothing new
            return response_bundle(base, existing, sub)
        next_id = cur.execute("SELECT coalesce(max(substr(pa_id, 3)::bigint), 0) + 1 FROM pa_req").fetchone()[0]
        t = now().replace(tzinfo=None)
        req = {"pa_id": f"PA{next_id:012d}", "mbr_id": member, "prv_id": provider_id, "svc_cd": svc["svc_cd"],
               "urgnt_flg": urgent, "req_ts": t, "dcsn_ts": t if decision == "A" else None, "dcsn_cd": decision,
               "req_amt": svc["req_amt"], "dx_cd": svc["dx_cd"], "pos_cd": svc["pos_cd"],
               "svc_dt": svc["svc_dt"].strftime("%Y%m%d"), "svc_qty": svc["svc_qty"], "src_cd": "F",
               "sbmsn_id": request_id, "rqstr_npi": requester_npi(sub, qr), "upd_ts": t}
        cur.execute(f"INSERT INTO pa_req ({', '.join(REQUEST_COLS)}) VALUES ({', '.join(['%s'] * len(REQUEST_COLS))})",
                    [req[c] for c in REQUEST_COLS])
        for link_id, (answer, source) in answers.items():
            cur.execute("INSERT INTO pa_doc (pa_id, qnr_url, item_id, ans_txt, ans_src, upd_ts) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", [req["pa_id"], qr["questionnaire"][:120], link_id[:40],
                                                             answer, source, t])
        conn.commit()
    if decision == "A":
        service.mark_notified(req["pa_id"])
    note = None if decision == "A" else "Pended for clinical review: " + "; ".join(unmet)
    return response_bundle(base, req, sub, note=note)


def inquire(upstream, base: str, provider_id: str, body: dict) -> dict:
    sub = Submission(body, "profile-claim-inquiry")
    request_id = sub.identifier()
    sub.check_provider(upstream, provider_id)
    member, _ = sub.member(upstream)
    with connect() as conn, conn.cursor() as cur:
        req = load_request(cur, "prv_id = %s AND sbmsn_id = %s AND mbr_id = %s AND src_cd = 'F'",
                           [provider_id, request_id, member])
    if req is None:
        raise PAError(404, "No prior authorization request of this provider and member has that identifier",
                      "not-found")
    return response_bundle(base, req, sub, inquiry=True)


# --- Subscriptions ---------------------------------------------------------------------

class PriorAuthService:
    """The subscriptions (in memory) and the notifier thread."""

    def __init__(self, upstream, public_base: str, interval: float = 2.0):
        self.upstream, self.base, self.interval = upstream, public_base.rstrip("/"), interval
        self.subscriptions: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.notified: set[str] | None = None

    def mark_notified(self, pa_id: str) -> None:
        with self.lock:
            if self.notified is not None:
                self.notified.add(pa_id)

    def start(self) -> None:
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the database may not be up yet; try again
                traceback.print_exc()
            time.sleep(self.interval)

    def tick(self) -> None:
        with connect() as conn, conn.cursor() as cur:
            decided = cur.execute("SELECT pa_id FROM pa_req WHERE src_cd = 'F' AND dcsn_cd <> 'P'").fetchall()
            with self.lock:
                if self.notified is None:  # first run: what was decided before this start is not notified
                    self.notified = {r[0] for r in decided}
                    return
                new = [r[0] for r in decided if r[0] not in self.notified]
            for pa_id in new:
                req = load_request(cur, "pa_id = %s", [pa_id])
                self.notify(req)
                with self.lock:
                    self.notified.add(pa_id)

    def npi_filter(self, provider_id: str) -> str:
        return f"orgIdentifier={NPI}|{provider_npi(self.upstream, provider_id)}"

    def notify(self, req: dict) -> None:
        with self.lock:
            subs = [s for s in self.subscriptions.values()
                    if s["owner"] == req["prv_id"] and s["resource"]["status"] == "active"]
        for s in subs:
            s["events"] += 1
            self.send(s, "event-notification", response_bundle(self.base, req, None))

    def status_parameters(self, s: dict, kind: str, focus: dict | None = None) -> dict:
        params = [{"name": "subscription", "valueReference": {"reference": f"{self.base}/Subscription/{s['id']}"}},
                  {"name": "topic", "valueCanonical": PAS_TOPIC},
                  {"name": "status", "valueCode": s["resource"]["status"]},
                  {"name": "type", "valueCode": kind},
                  {"name": "events-since-subscription-start", "valueString": str(s["events"])}]
        if focus is not None:
            params.append({"name": "notification-event", "part": [
                {"name": "event-number", "valueString": str(s["events"])},
                {"name": "timestamp", "valueInstant": instant(now())},
                {"name": "focus", "valueReference": {"reference": f"{self.base}/Bundle/{focus['id']}"}}]})
        return {"resourceType": "Parameters", "id": str(uuid.uuid4()),
                "meta": {"profile": [BACKPORT + "backport-subscription-status-r4"]}, "parameter": params}

    def notification(self, s: dict, kind: str, focus: dict | None = None) -> dict:
        status = self.status_parameters(s, kind, focus)
        entries = [{"fullUrl": f"urn:uuid:{status['id']}", "resource": status,
                    "request": {"method": "GET", "url": f"{self.base}/Subscription/{s['id']}/$status"},
                    "response": {"status": "200"}}]
        if focus is not None and s["content"] == "full-resource":
            entries.append({"fullUrl": f"{self.base}/Bundle/{focus['id']}", "resource": focus,
                            "request": {"method": "POST", "url": "Bundle"}, "response": {"status": "201"}})
        return {"resourceType": "Bundle", "id": str(uuid.uuid4()),
                "meta": {"profile": [BACKPORT + "backport-subscription-notification-r4"]},
                "type": "history", "timestamp": instant(now()), "entry": entries}

    def send(self, s: dict, kind: str, focus: dict | None = None) -> bool:
        body = json.dumps(self.notification(s, kind, focus)).encode()
        req = urllib.request.Request(s["resource"]["channel"]["endpoint"], data=body, method="POST",
                                     headers={"Content-Type": "application/fhir+json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = 200 <= resp.status < 300
        except (urllib.error.URLError, OSError):
            ok = False
        if not ok:
            s["resource"]["status"] = "error"
        return ok

    def create(self, provider_id: str, resource: dict) -> dict:
        if resource.get("resourceType") != "Subscription":
            raise PAError(400, "The body must be a Subscription", "invalid")
        if resource.get("criteria") != PAS_TOPIC:
            raise PAError(422, f"This server offers the topic {PAS_TOPIC} only", "not-supported")
        filters = [e.get("valueString") for e in resource.get("_criteria", {}).get("extension", [])
                   if e.get("url") == BACKPORT + "backport-filter-criteria"]
        if filters != [self.npi_filter(provider_id)]:
            raise PAError(403, f"The filter must be {self.npi_filter(provider_id)} (the requesting provider)",
                          "forbidden")
        channel = resource.get("channel") or {}
        content = next((e.get("valueCode") for e in channel.get("_payload", {}).get("extension", [])
                        if e.get("url") == BACKPORT + "backport-payload-content"), "id-only")
        if channel.get("type") != "rest-hook" or not str(channel.get("endpoint", "")).startswith(("http://", "https://")):
            raise PAError(422, "This server offers rest-hook channels only", "not-supported")
        if content not in ("id-only", "full-resource"):
            raise PAError(422, "The payload content must be id-only or full-resource", "not-supported")
        sid = str(uuid.uuid4())
        stored = {**resource, "id": sid, "status": "requested"}
        s = {"id": sid, "owner": provider_id, "resource": stored, "events": 0, "content": content}
        with self.lock:
            self.subscriptions[sid] = s
        stored["status"] = "active"
        if not self.send(s, "handshake"):
            stored["status"] = "error"
            stored["error"] = "The handshake to the endpoint failed"
        return stored

    def get(self, provider_id: str, sid: str) -> dict:
        s = self.subscriptions.get(sid)
        if s is None or s["owner"] != provider_id:
            raise PAError(404, "Unknown Subscription", "not-found")
        return s

    def delete(self, provider_id: str, sid: str) -> None:
        self.get(provider_id, sid)
        with self.lock:
            self.subscriptions.pop(sid, None)

    def status_bundle(self, provider_id: str, sid: str) -> dict:
        s = self.get(provider_id, sid)
        status = self.status_parameters(s, "query-status")
        return {"resourceType": "Bundle", "type": "searchset", "total": 1,
                "entry": [{"fullUrl": f"urn:uuid:{status['id']}", "resource": status, "search": {"mode": "match"}}]}


# --- Capability --------------------------------------------------------------------------

def add_capabilities(rest: dict) -> None:
    resources = {r.get("type"): r for r in rest.get("resource", [])}

    def resource(rtype):
        if rtype not in resources:
            resources[rtype] = {"type": rtype}
            rest.setdefault("resource", []).append(resources[rtype])
        return resources[rtype]

    resource("Claim").setdefault("operation", []).extend([
        {"name": "submit", "definition": PAS + "OperationDefinition/Claim-submit"},
        {"name": "inquire", "definition": PAS + "OperationDefinition/Claim-inquiry"}])
    resource("Questionnaire").setdefault("operation", []).append(
        {"name": "questionnaire-package", "definition": DTR + "OperationDefinition/questionnaire-package"})
    sub = resource("Subscription")
    sub["interaction"] = [{"code": "create"}, {"code": "read"}, {"code": "delete"}]
    sub["supportedProfile"] = [PAS + "StructureDefinition/profile-subscription"]
