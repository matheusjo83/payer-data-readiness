"""A provider's EHR, simulated: the client side of Da Vinci CRD, DTR and PAS 2.1.0.

What the provider's systems hold and send to the payer's prior authorization
service (fhir/gateway/prior_auth.py) when a clinician orders a service:

  practitioner        one ordering clinician per provider (US Core Practitioner),
                      with a valid NPI; synthetic, and the EHR's own data, not the
                      payer's (the payer only stores the NPI it receives).
  patient, coverage   the EHR's copy of the member: the payer's member ID and the
                      provider's medical record number (MR), with the CRD and PAS
                      profiles (the DTR answers point at them from both).
  order               a draft ServiceRequest (CRD), sent to the CRD service with the
                      order-select and order-sign hooks.
  questionnaire_response
                      the DTR answers: the member ID and the order's diagnosis and
                      quantity filled from the EHR (source auto), the rest by the
                      clinician (manual, with the clinician as author).
  pas_request, pas_inquiry
                      the PAS request and inquiry Bundles.
  subscription        a PAS Subscription for the provider's decisions.
  Listener            the provider's endpoint for Subscription notifications.
"""

import copy
import hashlib
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAS = "http://hl7.org/fhir/us/davinci-pas/StructureDefinition/"
CRD = "http://hl7.org/fhir/us/davinci-crd/StructureDefinition/"
DTR = "http://hl7.org/fhir/us/davinci-dtr/StructureDefinition/"
BACKPORT = "http://hl7.org/fhir/uv/subscriptions-backport/StructureDefinition/"
SITE = "https://payer-data-readiness.example/"
X12 = "https://codesystem.x12.org/005010/"
CPT = "http://www.ama-assn.org/go/cpt"
ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"
NPI = "http://hl7.org/fhir/sid/us-npi"
POS = "https://www.cms.gov/Medicare/Coding/place-of-service-codes/Place_of_Service_Code_Set"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
PAS_TOPIC = "http://hl7.org/fhir/us/davinci-pas/SubscriptionTopic/PASSubscriptionTopic"
NUCC = "http://nucc.org/provider-taxonomy"
NUCC_VERSION = "26.1"  # the release tx.fhir.org has; the code system requires a version
# What each service's ordering clinician is (NUCC taxonomy) and the PAS service type (X12 1365).
SPECIALTY = {"97110": ("225100000X", "Physical Therapist"), "71046": ("207Q00000X", "Family Medicine Physician")}
SERVICE_TYPE = {"97110": "PT", "71046": "4"}
FAMILY = ["Lee", "Patel", "Nguyen", "Okafor", "Silva", "Kim", "Haddad", "Novak", "Mensah", "Rossi"]
GIVEN = ["Jordan", "Avery", "Riley", "Morgan", "Casey", "Quinn", "Rowan", "Sasha", "Taylor", "Emerson"]


def instant(day_or_time: str) -> str:
    """A Bundle timestamp (an instant) from a date or a dateTime."""
    return day_or_time if "T" in day_or_time else f"{day_or_time}T00:00:00Z"


def narrative(text: str) -> dict:
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml"><p>{text}</p></div>'}


def npi_check_digit(first9: str) -> str:
    """The Luhn check digit of an NPI, computed with the 80840 prefix (CMS)."""
    total = 24  # the prefix 80840 contributes 24
    for i, ch in enumerate(reversed(first9)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return str((10 - total % 10) % 10)


def practitioner(provider_id: str) -> dict:
    h = int(hashlib.sha256(provider_id.encode()).hexdigest(), 16)
    first9 = "1" + f"{h % 10**8:08d}"
    return {
        "resourceType": "Practitioner", "id": f"PR-{provider_id}",
        # PAS's Practitioner profile derives from US Core's, which DTR requires of an author.
        "meta": {"profile": [PAS + "profile-practitioner"]},
        "text": narrative(f"Ordering clinician at provider {provider_id} (synthetic)"),
        "identifier": [{"system": NPI, "value": first9 + npi_check_digit(first9)}],
        "name": [{"family": FAMILY[h % 10], "given": [GIVEN[(h // 10) % 10]]}],
    }


def practitioner_role(provider_id: str, cpt: str) -> dict:
    """The clinician at the provider, as the PAS care team names them."""
    role, display = SPECIALTY.get(cpt, ("207Q00000X", "Family Medicine Physician"))
    return {"resourceType": "PractitionerRole", "id": f"PRR-{provider_id}",
            "meta": {"profile": [PAS + "profile-practitionerrole"]},
            "text": narrative(f"{display} at provider {provider_id}"),
            "practitioner": {"reference": f"Practitioner/PR-{provider_id}"},
            "organization": {"reference": f"Organization/{provider_id}"},
            "code": [{"coding": [{"system": NUCC, "version": NUCC_VERSION, "code": role, "display": display}]}]}


def mrn(provider_id: str, member_id: str) -> dict:
    return {"type": {"coding": [{"system": V2_0203, "code": "MR"}]},
            "system": f"{SITE}provider/{provider_id}/mrn", "value": "MRN-" + member_id.lstrip("M0")}


def member_identifier(member_id: str) -> dict:
    return {"type": {"coding": [{"system": V2_0203, "code": "MB"}]}, "system": SITE + "member-id", "value": member_id}


def ehr_patient(gold_patient: dict, provider_id: str) -> dict:
    p = copy.deepcopy(gold_patient)
    p["meta"] = {"profile": [PAS + "profile-beneficiary", CRD + "profile-patient"]}
    p["text"] = narrative(f"Patient {p['id']}")
    p["identifier"] = [member_identifier(p["id"]), mrn(provider_id, p["id"])]
    return p


def ehr_coverage(gold_coverage: dict) -> dict:
    c = copy.deepcopy(gold_coverage)
    c["meta"] = {"profile": [PAS + "profile-coverage", CRD + "profile-coverage"]}
    c["text"] = narrative(f"Coverage {c['id']}")
    c["identifier"] = [member_identifier(c["subscriberId"])]
    c["relationship"]["coding"].append({"system": X12 + "1069", "code": "18"})
    c["type"].pop("text", None)
    return c


def requestor(gold_org: dict) -> dict:
    o = copy.deepcopy(gold_org)
    o["meta"] = {"profile": [PAS + "profile-requestor"]}
    o["text"] = narrative(o.get("name", o["id"]))
    o["type"] = [{"coding": [{"system": X12 + "98", "code": "X3"}]}]
    for ident in o.get("identifier", []):
        ident.pop("type", None)
    return o


def order(order_id: str, patient: dict, coverage: dict, provider_id: str, cpt: str, dx: str, quantity: int,
          when: str, expedited: bool = False) -> dict:
    return {
        "resourceType": "ServiceRequest", "id": order_id,
        "meta": {"profile": [CRD + "profile-servicerequest"]},
        "text": narrative(f"Order {order_id}: CPT {cpt}, quantity {quantity}"),
        "status": "draft", "intent": "order",
        "priority": "stat" if expedited else "routine",
        "code": {"coding": [{"system": CPT, "code": cpt}]},
        "quantityQuantity": {"value": quantity},
        "subject": {"reference": f"Patient/{patient['id']}"},
        "occurrenceDateTime": when, "authoredOn": when,
        "requester": {"reference": f"Practitioner/PR-{provider_id}"},
        "reasonCode": [{"coding": [{"system": ICD10CM, "code": dx}]}],
        "insurance": [{"reference": f"Coverage/{coverage['id']}"}],
    }


def cds_request(hook: str, patient: dict, coverage: dict, orders: list[dict], provider_id: str) -> dict:
    context = {"userId": f"Practitioner/PR-{provider_id}", "patientId": patient["id"],
               "draftOrders": {"resourceType": "Bundle", "type": "collection",
                               "entry": [{"resource": o} for o in orders]}}
    if hook == "order-select":
        context["selections"] = [f"ServiceRequest/{o['id']}" for o in orders]
    return {"hook": hook, "hookInstance": str(uuid.uuid4()),
            "fhirServer": f"https://ehr.example/{provider_id}/fhir", "context": context,
            "prefetch": {"patient": patient, "coverage": {"resourceType": "Bundle", "type": "searchset",
                                                          "entry": [{"resource": coverage}]}}}


def origin(source: str, provider_id: str, cpt: str) -> list[dict]:
    ext = [{"url": "source", "valueCode": source}]
    if source in ("override", "manual"):
        role, display = SPECIALTY.get(cpt, ("207Q00000X", "Family Medicine Physician"))
        ext.append({"url": "author", "extension": [
            {"url": "role", "valueCodeableConcept": {"coding": [{"system": NUCC, "version": NUCC_VERSION,
                                                                  "code": role, "display": display}]}},
            {"url": "practitioner", "valueReference": {"reference": f"Practitioner/PR-{provider_id}"}}]})
    return [{"url": DTR + "information-origin", "extension": ext}]


def answer_value(item: dict, value) -> dict:
    if item["type"] == "boolean":
        return {"valueBoolean": bool(value)}
    if item["type"] == "integer":
        return {"valueInteger": int(value)}
    if item["type"] == "choice":
        option = next(o["valueCoding"] for o in item["answerOption"] if o["valueCoding"]["code"] == value)
        return {"valueCoding": option}
    return {"valueString": str(value)}


def questionnaire_response(qr_id: str, questionnaire: dict, patient: dict, coverage: dict, order_: dict,
                           provider_id: str, answers: dict) -> dict:
    """The DTR answers: from the EHR (auto) the member ID, the diagnosis and the quantity
    the order holds; from the clinician (manual) the answers given. An answer left out
    of `answers` (and not filled from the EHR) is left unanswered."""
    cpt = order_["code"]["coding"][0]["code"]
    auto = {"member-id": patient["id"],
            "diagnosis": order_["reasonCode"][0]["coding"][0]["code"],
            "visits": order_["quantityQuantity"]["value"]}
    items = []
    for it in questionnaire["item"]:
        link = it["linkId"]
        if link in answers:
            value, source = answers[link], "manual"
        elif link in auto:
            value, source = auto[link], "auto"
        else:
            continue
        items.append({"linkId": link, "text": it["text"],
                      "answer": [{"extension": origin(source, provider_id, cpt), **answer_value(it, value)}]})
    return {
        "resourceType": "QuestionnaireResponse", "id": qr_id,
        "meta": {"profile": [DTR + "dtr-questionnaireresponse"]},
        "text": narrative(f"Documentation for order {order_['id']}"),
        "extension": [
            {"url": DTR + "qr-context", "valueReference": {"reference": f"Coverage/{coverage['id']}"}},
            {"url": DTR + "qr-context", "valueReference": {"reference": f"ServiceRequest/{order_['id']}"}},
            {"url": DTR + "intendedUse", "valueCodeableConcept": {"coding": [{
                "system": "http://hl7.org/fhir/us/davinci-crd/CodeSystem/temp", "code": "withpa"}]}}],
        "questionnaire": f"{questionnaire['url']}|{questionnaire['version']}", "status": "completed",
        "subject": {"reference": f"Patient/{patient['id']}"}, "authored": order_["authoredOn"],
        "author": {"reference": f"Practitioner/PR-{provider_id}"},
        "item": items,
    }


def entry(base: str, res: dict) -> dict:
    return {"fullUrl": f"{base}/{res['resourceType']}/{res['id']}", "resource": res}


def pas_request(request_id: str, patient: dict, coverage: dict, org: dict, order_: dict, qr: dict | None,
                provider_id: str, place: str, estimate: float, ehr_base: str) -> dict:
    cpt = order_["code"]["coding"][0]["code"]
    expedited = order_.get("priority") == "stat"
    claim = {
        "resourceType": "Claim", "id": request_id,
        "meta": {"profile": [PAS + "profile-claim"]},
        "text": narrative(f"Prior authorization request {request_id}: CPT {cpt}"),
        "identifier": [{"system": f"{SITE}provider/{provider_id}/prior-auth-request", "value": request_id}],
        "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
        "use": "preauthorization",
        "patient": {"reference": f"Patient/{patient['id']}"},
        "created": order_["authoredOn"],
        "insurer": {"reference": "Organization/payer"},
        "provider": {"reference": f"Organization/{org['id']}"},
        "priority": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/processpriority",
                                 "code": "stat" if expedited else "normal"}]},
        "careTeam": [{"extension": [{"url": PAS + "extension-careTeamClaimScope", "valueBoolean": True}],
                      "sequence": 1, "provider": {"reference": f"PractitionerRole/PRR-{provider_id}"}}],
        "diagnosis": [{"sequence": 1, "diagnosisCodeableConcept": {"coding": [{
            "system": ICD10CM, "code": order_["reasonCode"][0]["coding"][0]["code"]}]}}],
        "insurance": [{"sequence": 1, "focal": True, "coverage": {"reference": f"Coverage/{coverage['id']}"}}],
        "item": [{
            "extension": [
                {"url": PAS + "extension-serviceItemRequestType",
                 "valueCodeableConcept": {"coding": [{"system": X12 + "1525", "code": "HS"}]}},
                {"url": PAS + "extension-certificationType",
                 "valueCodeableConcept": {"coding": [{"system": X12 + "1322", "code": "I"}]}}],
            "sequence": 1, "careTeamSequence": [1], "diagnosisSequence": [1],
            "category": {"coding": [{"system": X12 + "1365", "code": SERVICE_TYPE.get(cpt, "1")}]},
            "productOrService": {"coding": [{"system": CPT, "code": cpt}]},
            "servicedDate": order_["occurrenceDateTime"][:10],
            "locationCodeableConcept": {"coding": [{"system": POS, "code": place}]},
            "quantity": {"value": order_["quantityQuantity"]["value"]},
            "net": {"value": estimate, "currency": "USD"},
        }],
    }
    if qr is not None:
        claim["supportingInfo"] = [{"sequence": 1, "category": {"coding": [{
            "system": "http://hl7.org/fhir/us/davinci-pas/CodeSystem/PASTempCodes", "code": "additionalInformation"}]},
            "valueReference": {"reference": f"QuestionnaireResponse/{qr['id']}"}}]
    resources = [claim, patient, coverage, org, practitioner_role(provider_id, cpt), practitioner(provider_id)] \
        + ([qr] if qr else [])
    return {"resourceType": "Bundle", "id": f"pas-{request_id}",
            "meta": {"profile": [PAS + "profile-pas-request-bundle"]},
            "identifier": {"system": f"{SITE}provider/{provider_id}/pas-submission", "value": f"SUB-{request_id}"},
            "type": "collection", "timestamp": instant(order_["authoredOn"]),
            "entry": [entry(ehr_base, r) for r in resources]}


def pas_inquiry(request_id: str, patient: dict, coverage: dict, org: dict, order_: dict, provider_id: str,
                place: str, ehr_base: str, when: str) -> dict:
    cpt = order_["code"]["coding"][0]["code"]
    claim = {
        "resourceType": "Claim", "id": f"inquiry-{request_id}",
        "meta": {"profile": [PAS + "profile-claim-inquiry"]},
        "text": narrative(f"Inquiry about prior authorization request {request_id}"),
        "identifier": [{"system": f"{SITE}provider/{provider_id}/prior-auth-request", "value": request_id}],
        "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
        "use": "preauthorization",
        "patient": {"reference": f"Patient/{patient['id']}"}, "created": when,
        "insurer": {"reference": "Organization/payer"}, "provider": {"reference": f"Organization/{org['id']}"},
        "priority": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/processpriority", "code": "normal"}]},
        "insurance": [{"sequence": 1, "focal": True, "coverage": {"reference": f"Coverage/{coverage['id']}"}}],
        "item": [{"sequence": 1,
                  "category": {"coding": [{"system": X12 + "1365", "code": SERVICE_TYPE.get(cpt, "1")}]},
                  "productOrService": {"coding": [{"system": CPT, "code": cpt}]},
                  "servicedDate": order_["occurrenceDateTime"][:10],
                  "locationCodeableConcept": {"coding": [{"system": POS, "code": place}]}}],
    }
    return {"resourceType": "Bundle", "id": f"inquiry-{request_id}",
            "meta": {"profile": [PAS + "profile-pas-inquiry-request-bundle"]},
            "identifier": {"system": f"{SITE}provider/{provider_id}/pas-inquiry", "value": f"INQ-{request_id}"},
            "type": "collection", "timestamp": instant(when),
            "entry": [entry(ehr_base, r) for r in (claim, patient, coverage, org)]}


def subscription(npi: str, endpoint: str, content: str = "full-resource") -> dict:
    return {
        "resourceType": "Subscription",
        "meta": {"profile": [PAS + "profile-subscription"]},
        "text": narrative(f"Notify provider NPI {npi} of decisions on its prior authorization requests"),
        "status": "requested", "reason": "Decisions on my pended prior authorization requests",
        "criteria": PAS_TOPIC,
        "_criteria": {"extension": [{"url": BACKPORT + "backport-filter-criteria",
                                     "valueString": f"orgIdentifier={NPI}|{npi}"}]},
        "channel": {"type": "rest-hook", "endpoint": endpoint, "payload": "application/fhir+json",
                    "_payload": {"extension": [{"url": BACKPORT + "backport-payload-content", "valueCode": content}]}},
    }


class Listener:
    """The provider's rest-hook endpoint: keeps every notification Bundle it receives."""

    def __init__(self, port: int = 8094):
        self.received: list[dict] = []
        self.event = threading.Condition()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with outer.event:
                    outer.received.append(json.loads(body))
                    outer.event.notify_all()
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, predicate, timeout: float = 30) -> list[dict]:
        """The notifications received that satisfy predicate, waiting up to timeout seconds for one."""
        with self.event:
            self.event.wait_for(lambda: any(predicate(b) for b in self.received), timeout)
            return [b for b in self.received if predicate(b)]

    def close(self) -> None:
        self.server.shutdown()
