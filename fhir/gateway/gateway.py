"""FHIR gateway for the Provider Access and Payer-to-Payer APIs (CMS-0057-F).

Implements the Da Vinci PDex 2.1.0 operations that HAPI does not, in front of
the HAPI server that holds the data those APIs share (fhir-server-provider: the
claims without financial data, the prior authorizations, the attribution lists
and the opt-outs). Every other request is passed through to that server.

  POST [base]/Group/[id]/$davinci-data-export   (Da Vinci ATR 2.1.0, profiled by PDex)
      Bulk export of the members of an attribution list (Provider Access, a PDex
      Provider Group) or of a Payer-to-Payer match (a PDex Member Match Group).
      Asynchronous (Prefer: respond-async): 202 with a status URL, which answers
      202 while the export runs and then 200 with a Bulk Data manifest. HAPI's
      Group $export extracts the data (with _type, _typeFilter and _since); the
      gateway then applies, to every exported resource:
        patient       only the members named (a reference to a non-member is
                      ignored and reported in an OperationOutcome);
        opt-outs      Provider Access: a member whose opt-out (PDex Provider
                      Access Consent) is in force when the export runs is left
                      out, so an opt-out recorded after the list was built
                      applies ("SHALL check the consent status for each member
                      at the time of execution");
        exportType    hl7.fhir.us.davinci-pdex#provider-snapshot (default for an
                      attribution list), #provider-download (the same data,
                      recorded as transmitted) and #provider-delta (only what
                      changed since each member's last transmission), or
                      #payertopayer (the only one allowed on a match group);
        lookback      Payer-to-Payer: claims with a service date in the 5 years
                      before the request, and prior authorizations that are
                      pending or changed status in the last 12 months;
        _until        resources last updated after it are left out.
      After a provider-download or provider-delta export, each member's
      last-transmission extensions (lastTransmitted, lastResources,
      lastFilters) are updated on the Group. The Group itself is not exported,
      and Organizations only when the exported resources reference them.

  POST [base]/Group/$bulk-member-match   (PDex 2.1.0, Payer-to-Payer)
      Matches each MemberBundle (HRex patient demographics, the coverage with
      this payer, the member's consent) and answers 200 with the PDex response
      Parameters: MatchedMembers (stored on the server, so the requesting payer
      can export it), NonMatchedMembers and ConsentConstrainedMembers.

  POST [base]/Patient/$member-match   (Da Vinci HRex 1.1.0, one member)
      200 with MemberIdentifier and MemberId, or 422 with an OperationOutcome
      when there is no unique match or the consent cannot be complied with.

  GET  [base]/metadata
      HAPI's CapabilityStatement, with these operations and the PDex
      provider-access and payer-access server CapabilityStatements declared.

  [base]/$export, [base]/Patient/$export, [base]/Group/[id]/$export
      Refused (400): they would bypass the opt-out check.

Matching (one MemberBundle): the member ID on the coverage to match (its MB
identifier in this payer's member ID system, or subscriberId), confirmed by the
birth date and the family or given name; without one, a unique Patient with
the same family name, given name, birth date and gender. No candidate, or more
than one, is no match (HRex: "A maximum of a SINGLE unique match"). A coverage
whose payor identifier is another payer's is no match. The consent of a matched
member must be active, in force today, allow this payer to disclose (a source
actor with this payer's identifier) to a recipient, and ask for the regular
policy (all data): this payer does not label sensitive data, so a request for
non-sensitive data only (#sensitive) cannot be complied with and the member is
consent-constrained.

No authorization (SMART Backend Services is a backlog item): any client can
call these operations. Standard library only, so the container runs the
official Python image with this file mounted.

Usage:
    python fhir/gateway/gateway.py [--port 8092] [--upstream http://localhost:8091/fhir]
"""

import argparse
import json
import os
import re
import shutil
import tempfile
import threading
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PDEX = "http://hl7.org/fhir/us/davinci-pdex/"
HREX = "http://hl7.org/fhir/us/davinci-hrex/"
ATR = "http://hl7.org/fhir/us/davinci-atr/"
MEMBER_ID_SYSTEM = "https://payer-data-readiness.example/member-id"
PAYER_ID = {"system": "https://payer-data-readiness.example/payer-id", "value": "payer"}
PROVIDER_CONSENT = PDEX + "StructureDefinition/pdex-provider-consent"
PROVIDER_GROUP = PDEX + "StructureDefinition/pdex-provider-group"
MATCH_GROUP = PDEX + "StructureDefinition/pdex-member-match-group"
NO_MATCH_GROUP = PDEX + "StructureDefinition/pdex-member-no-match-group"
MATCH_RESULT_CS = PDEX + "CodeSystem/PdexMultiMemberMatchResultCS"
MATCH_PARAMETERS_EXT = PDEX + "StructureDefinition/base-ext-match-parameters"
LAST_TRANSMISSION_EXT = PDEX + "StructureDefinition/base-ext-last-transmission"
LAST_TYPES_EXT = PDEX + "StructureDefinition/base-ext-last-types"
LAST_TYPEFILTER_EXT = PDEX + "StructureDefinition/base-ext-last-typefilter"
WHEN_ADJUDICATED_EXT = PDEX + "StructureDefinition/base-ext-when-adjudicated"
POLICY_REGULAR = HREX + "StructureDefinition-hrex-consent.html#regular"
POLICY_SENSITIVE = HREX + "StructureDefinition-hrex-consent.html#sensitive"
EXPORT_TYPES_PROVIDER = {"hl7.fhir.us.davinci-pdex#provider-snapshot", "hl7.fhir.us.davinci-pdex#provider-download",
                         "hl7.fhir.us.davinci-pdex#provider-delta"}
EXPORT_TYPE_P2P = "hl7.fhir.us.davinci-pdex#payertopayer"
DEFAULT_TYPES = ["Patient", "Coverage", "ExplanationOfBenefit", "Organization"]
LOOKBACK_YEARS = 5
PRIOR_AUTH_STATUS_CHANGE_DAYS = 365
JOBS_DIR = Path(tempfile.gettempdir()) / "fhir-gateway-jobs"


class FhirError(Exception):
    """An error answered as an OperationOutcome."""

    def __init__(self, status: int, message: str, code: str = "processing"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def outcome(message: str, code: str = "processing", severity: str = "error") -> dict:
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": severity, "code": code, "diagnostics": message}]}


# --- Upstream (HAPI) -------------------------------------------------------------

class Upstream:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def request(self, method: str, path_or_url: str, body: bytes | None = None, headers: dict | None = None,
                timeout: int = 300):
        """(status, headers, body) of a request to the upstream server; HTTP errors are returned, not raised."""
        url = path_or_url if path_or_url.startswith("http") else f"{self.base}/{path_or_url.lstrip('/')}"
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, method: str, path: str, resource: dict | None = None, headers: dict | None = None):
        body = json.dumps(resource).encode() if resource is not None else None
        status, _, data = self.request(method, path, body, {
            "Accept": "application/fhir+json", "Content-Type": "application/fhir+json",
            "Cache-Control": "no-cache", **(headers or {})})
        return status, json.loads(data) if data else {}

    def read(self, rtype: str, rid: str) -> dict | None:
        status, resource = self.json("GET", f"{rtype}/{urllib.parse.quote(rid)}")
        return resource if status == 200 else None

    def search(self, query: str) -> list[dict]:
        """Every resource a search returns, following the next links."""
        found, url = [], query + ("&" if "?" in query else "?") + "_count=500"
        while url:
            status, bundle = self.json("GET", url)
            if status != 200:
                raise FhirError(502, f"Upstream search {query} failed with HTTP {status}")
            found += [e["resource"] for e in bundle.get("entry", [])
                      if e.get("search", {}).get("mode", "match") == "match"]
            url = next((link["url"] for link in bundle.get("link", []) if link["relation"] == "next"), None)
        return found


# --- Small FHIR helpers ----------------------------------------------------------

def today() -> date:
    return datetime.now(timezone.utc).date()


def parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def in_period(period: dict | None, day: date) -> bool:
    period = period or {}
    start, end = period.get("start"), period.get("end")
    return (not start or date.fromisoformat(start[:10]) <= day) and (not end or day <= date.fromisoformat(end[:10]))


def normalized(text: str | None) -> str:
    """Lower case, without accents, spaces or punctuation, for comparing names."""
    text = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^a-z]", "", "".join(c for c in text if not unicodedata.combining(c)).lower())


def same_identifier(a: dict | None, b: dict) -> bool:
    return bool(a) and a.get("system") == b["system"] and a.get("value") == b["value"]


def extension(resource: dict, url: str):
    return next((e for e in resource.get("extension", []) if e.get("url") == url), None)


def patient_of(resource: dict) -> str | None:
    """The member ('Patient/<id>') a resource belongs to, for the types exported."""
    rtype = resource["resourceType"]
    if rtype == "Patient":
        return f"Patient/{resource['id']}"
    ref = {"Coverage": "beneficiary", "ExplanationOfBenefit": "patient", "Consent": "patient"}.get(rtype)
    return (resource.get(ref) or {}).get("reference") if ref else None


def references(node) -> set[str]:
    if isinstance(node, dict):
        found = {node["reference"]} if isinstance(node.get("reference"), str) else set()
        return found.union(*(references(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(references(v) for v in node)) if node else set()
    return set()


def opted_out_members(upstream: Upstream, day: date) -> set[str]:
    """Members with a Provider Access opt-out in force on `day`."""
    consents = upstream.search(f"Consent?_profile={urllib.parse.quote(PROVIDER_CONSENT)}")
    return {c["patient"]["reference"] for c in consents
            if c.get("status") == "active" and c.get("provision", {}).get("type") == "deny"
            and in_period(c["provision"].get("period"), day)}


def group_kind(group: dict) -> str:
    profiles = group.get("meta", {}).get("profile", [])
    codes = {c.get("code") for c in group.get("code", {}).get("coding", [])}
    if any(p.startswith(PROVIDER_GROUP) for p in profiles) or "pdexprovidergroup" in codes:
        return "provider"
    if any(p.startswith(MATCH_GROUP) for p in profiles) and "match" in codes:
        return "payertopayer"
    return "other"


# --- $davinci-data-export ----------------------------------------------------------

def export_parameters(query: dict, body: bytes) -> dict:
    """Operation parameters from the query string and/or a Parameters body."""
    params = {k: list(v) for k, v in query.items()}
    if body:
        try:
            resource = json.loads(body)
        except json.JSONDecodeError as e:
            raise FhirError(400, f"The body is not JSON: {e}", "invalid") from e
        if resource.get("resourceType") != "Parameters":
            raise FhirError(400, "The body must be a Parameters resource", "invalid")
        for p in resource.get("parameter", []):
            value = next((v for k, v in p.items() if k.startswith("value")), None)
            if isinstance(value, dict):
                value = value.get("reference")
            params.setdefault(p["name"], []).append(value)
    return params


class ExportJob:
    def __init__(self, upstream: Upstream, group_id: str, params: dict, request_url: str):
        self.id = uuid.uuid4().hex
        self.upstream, self.group_id, self.params, self.request_url = upstream, group_id, params, request_url
        self.status, self.progress, self.manifest, self.error = "running", "queued", None, None
        self.dir = JOBS_DIR / self.id
        self.cancelled = False

    def single(self, name: str) -> str | None:
        values = [v for v in self.params.get(name, []) if v]
        if len(values) > 1:
            raise FhirError(400, f"{name} can be given only once", "invalid")
        return values[0] if values else None

    def listed(self, name: str) -> list[str]:
        return [x.strip() for v in self.params.get(name, []) if v for x in v.split(",") if x.strip()]

    def run(self, public_base: str) -> None:
        try:
            self.manifest = self.export(public_base)
            self.status = "completed"
        except FhirError as e:
            self.status, self.error = "failed", (e.status, outcome(e.message, e.code))
        except Exception as e:  # noqa: BLE001 - reported to the client as a failed job
            traceback.print_exc()
            self.status, self.error = "failed", (500, outcome(f"Export failed: {e}", "exception"))

    def prepare(self) -> None:
        """Check the request before accepting it (raises FhirError)."""
        group = self.upstream.read("Group", self.group_id)
        if group is None:
            raise FhirError(404, f"Group/{self.group_id} not found", "not-found")
        kind = group_kind(group)
        export_type = self.single("exportType")
        if kind == "provider":
            export_type = export_type or "hl7.fhir.us.davinci-pdex#provider-snapshot"
            if export_type not in EXPORT_TYPES_PROVIDER:
                raise FhirError(400, f"exportType {export_type} is not allowed on an attribution list; use one of "
                                     f"{sorted(EXPORT_TYPES_PROVIDER)}", "invalid")
        elif kind == "payertopayer":
            export_type = export_type or EXPORT_TYPE_P2P
            if export_type != EXPORT_TYPE_P2P:
                raise FhirError(400, f"exportType {export_type} is not allowed on a match group; use {EXPORT_TYPE_P2P}",
                                "invalid")
        else:
            raise FhirError(400, "$davinci-data-export applies to a PDex Provider Group or Member Match Group",
                            "not-supported")
        types = self.listed("_type") or DEFAULT_TYPES
        unsupported = sorted(set(types) - set(DEFAULT_TYPES))
        if unsupported:
            raise FhirError(400, f"_type not available: {unsupported} (available: {DEFAULT_TYPES})", "not-supported")
        self.single("_since")
        self.single("_until")
        self.kind, self.export_type, self.types = kind, export_type, types

    def export(self, public_base: str) -> dict:
        kind, export_type, types = self.kind, self.export_type, self.types
        group = self.upstream.read("Group", self.group_id)
        type_filters = self.listed("_typeFilter")
        since, until = self.single("_since"), self.single("_until")
        until_at = parse_instant(until)

        members = {m["entity"]["reference"]: m for m in group.get("member", [])
                   if not m.get("inactive") and m.get("entity", {}).get("reference", "").startswith("Patient/")}
        issues = []
        requested = self.listed("patient")
        if requested:
            for ref in requested:
                if ref not in members:
                    issues.append(outcome(f"{ref} is not a member of Group/{self.group_id}; ignored",
                                          "not-found", "warning"))
            selected = [r for r in requested if r in members]
        else:
            selected = list(members)
        started = datetime.now(timezone.utc)
        if kind == "provider":
            opted_out = opted_out_members(self.upstream, started.date())
            for ref in sorted(set(selected) & opted_out):
                issues.append(outcome(f"{ref} has opted out of the Provider Access API; left out",
                                      "suppressed", "information"))
            selected = [r for r in selected if r not in opted_out]
        selected_set = set(selected)

        # Kick off HAPI's Group export and wait for it.
        self.progress = "extracting"
        query = {"_type": ",".join(types)}
        if type_filters:
            query["_typeFilter"] = type_filters
        if since:
            query["_since"] = since
        status, headers, data = self.upstream.request(
            "GET", f"Group/{urllib.parse.quote(self.group_id)}/$export?{urllib.parse.urlencode(query, doseq=True)}",
            headers={"Prefer": "respond-async", "Accept": "application/fhir+json"})
        poll = headers.get("Content-Location")
        if status != 202 or not poll:
            detail = data[:500].decode(errors="replace")
            raise FhirError(502, f"Upstream export kick-off failed with HTTP {status}: {detail}")
        upstream_manifest = None
        while upstream_manifest is None:
            if self.cancelled:
                self.upstream.request("DELETE", poll)
                raise FhirError(404, "Export cancelled", "not-found")
            status, _, data = self.upstream.request("GET", poll, headers={"Accept": "application/json"})
            if status == 200:
                upstream_manifest = json.loads(data)
            elif status != 202:
                detail = data[:500].decode(errors="replace")
                raise FhirError(502, f"Upstream export failed with HTTP {status}: {detail}")
            else:
                threading.Event().wait(2)

        # Filter what HAPI exported.
        self.progress = "filtering"
        cutoff = years_before(started.date(), LOOKBACK_YEARS)
        status_change_since = started - timedelta(days=PRIOR_AUTH_STATUS_CHANGE_DAYS)
        last_sent = {}
        if export_type == "hl7.fhir.us.davinci-pdex#provider-delta":
            for ref, m in members.items():
                ext = extension(m, LAST_TRANSMISSION_EXT)
                last_sent[ref] = parse_instant(ext.get("valueDateTime")) if ext else None
        kept, organizations, counts = {}, {}, {}
        for output in upstream_manifest.get("output", []):
            _, _, data = self.upstream.request("GET", output["url"], headers={"Accept": "application/fhir+ndjson"})
            for line in data.decode().splitlines():
                if not line.strip():
                    continue
                resource = json.loads(line)
                rtype = resource["resourceType"]
                if rtype == "Organization":
                    organizations[f"Organization/{resource['id']}"] = resource
                    continue
                member = patient_of(resource)
                if member not in selected_set:
                    continue
                updated = parse_instant(resource.get("meta", {}).get("lastUpdated"))
                if until_at and updated and updated > until_at:
                    continue
                if export_type == "hl7.fhir.us.davinci-pdex#provider-delta":
                    sent = last_sent.get(member)
                    if sent and updated and updated <= sent:
                        continue
                if export_type == EXPORT_TYPE_P2P and rtype == "ExplanationOfBenefit" and not p2p_in_scope(
                        resource, cutoff, status_change_since):
                    continue
                kept.setdefault(rtype, []).append(resource)
        if "Organization" in types:
            needed = set().union(*(references(r) for rs in kept.values() for r in rs)) if kept else set()
            kept["Organization"] = [organizations[r] for r in sorted(needed) if r in organizations]

        # Write the files and the manifest.
        self.dir.mkdir(parents=True, exist_ok=True)
        output = []
        for rtype in types:
            resources = kept.get(rtype, [])
            counts[rtype] = len(resources)
            if not resources:
                continue
            name = f"{rtype}.ndjson"
            (self.dir / name).write_text("".join(json.dumps(r) + "\n" for r in resources))
            output.append({"type": rtype, "url": f"{public_base}/$export-file/{self.id}/{name}",
                           "count": len(resources)})
        errors = []
        if issues:
            (self.dir / "OperationOutcome.ndjson").write_text("".join(json.dumps(o) + "\n" for o in issues))
            errors.append({"type": "OperationOutcome",
                           "url": f"{public_base}/$export-file/{self.id}/OperationOutcome.ndjson"})
        transaction_time = upstream_manifest.get("transactionTime") or started.isoformat()

        if export_type in ("hl7.fhir.us.davinci-pdex#provider-download", "hl7.fhir.us.davinci-pdex#provider-delta"):
            self.progress = "recording transmission"
            self.record_transmission(selected_set, transaction_time, types, type_filters)
        return {"transactionTime": transaction_time, "request": self.request_url, "requiresAccessToken": False,
                "output": output, "error": errors,
                "extension": {"exportType": export_type, "group": f"Group/{self.group_id}",
                              "members": len(selected), "lookbackFrom": cutoff.isoformat()
                              if export_type == EXPORT_TYPE_P2P else None}}

    def record_transmission(self, members: set[str], when: str, types: list[str], type_filters: list[str]) -> None:
        """Set the last-transmission extensions of the members exported (PDex: SHALL be updated)."""
        group = self.upstream.read("Group", self.group_id)
        for m in group.get("member", []):
            if m.get("entity", {}).get("reference") not in members:
                continue
            exts = [e for e in m.get("extension", [])
                    if e.get("url") not in (LAST_TRANSMISSION_EXT, LAST_TYPES_EXT, LAST_TYPEFILTER_EXT)]
            exts.append({"url": LAST_TRANSMISSION_EXT, "valueDateTime": when})
            exts.append({"url": LAST_TYPES_EXT, "valueString": ",".join(types)})
            if type_filters:
                exts.append({"url": LAST_TYPEFILTER_EXT, "valueString": ",".join(type_filters)})
            m["extension"] = exts
        status, body = self.upstream.json("PUT", f"Group/{urllib.parse.quote(self.group_id)}", group)
        if status not in (200, 201):
            raise FhirError(502, f"Could not record the transmission on Group/{self.group_id}: HTTP {status} {body}")


def p2p_in_scope(eob: dict, cutoff: date, status_change_since: datetime) -> bool:
    """Payer-to-Payer scope of an ExplanationOfBenefit (PDex 2.1.0, bulk Payer-to-Payer):
    a claim with a service date no earlier than 5 years before the request; a prior
    authorization that is pending or changed status (was decided) in the last 12 months."""
    if eob.get("use") == "preauthorization":
        if eob.get("outcome") == "queued":
            return True
        decided = [parse_instant(e.get("valueDateTime")) for item in eob.get("item", [])
                   for a in item.get("adjudication", []) for e in a.get("extension", [])
                   if e.get("url") == WHEN_ADJUDICATED_EXT]
        decided += [parse_instant(e.get("valueDateTime")) for e in eob.get("extension", [])
                    if e.get("url") == WHEN_ADJUDICATED_EXT]
        return any(d and d >= status_change_since for d in decided)
    start = (eob.get("billablePeriod") or {}).get("start")
    served = [start] + [i.get("servicedDate") for i in eob.get("item", [])]
    return any(d and date.fromisoformat(d[:10]) >= cutoff for d in served)


# --- Member match ------------------------------------------------------------------

def bundle_part(parts: list[dict], name: str) -> dict | None:
    return next((p.get("resource") for p in parts if p.get("name") == name), None)


def coverage_member_id(coverage: dict) -> str | None:
    for ident in coverage.get("identifier", []):
        codes = {c.get("code") for c in ident.get("type", {}).get("coding", [])}
        if ident.get("system") == MEMBER_ID_SYSTEM or (ident.get("system") is None and "MB" in codes):
            return ident.get("value")
    return coverage.get("subscriberId")


def names(patient: dict) -> tuple[set[str], set[str]]:
    family = {normalized(n.get("family")) for n in patient.get("name", []) if n.get("family")}
    given = {normalized(g) for n in patient.get("name", []) for g in n.get("given", [])[:1]}
    return family - {""}, given - {""}


def match_member(upstream: Upstream, patient: dict, coverage: dict) -> tuple[str | None, str]:
    """(our Patient id or None, reason)."""
    payors = [p.get("identifier") for p in coverage.get("payor", []) if p.get("identifier")]
    if payors and not any(same_identifier(p, PAYER_ID) for p in payors):
        return None, "the coverage to match is with another payer"
    family, given = names(patient)
    birth = patient.get("birthDate")
    member_id = coverage_member_id(coverage)
    if member_id:
        found = upstream.search(f"Patient?identifier={urllib.parse.quote(MEMBER_ID_SYSTEM + '|' + member_id)}")
        confirmed = [p for p in found if p.get("birthDate") == birth
                     and (names(p)[0] & family or names(p)[1] & given)]
        if len(confirmed) == 1:
            return confirmed[0]["id"], f"member ID {member_id}, birth date and name"
        if found:
            return None, f"member ID {member_id} found, but the birth date and name do not confirm it"
    if not (family and given and birth and patient.get("gender")):
        return None, "no member ID on the coverage and incomplete demographics"
    query = (f"Patient?family={urllib.parse.quote(patient['name'][0].get('family', ''))}"
             f"&birthdate={birth}&gender={patient['gender']}")
    found = [p for p in upstream.search(query) if names(p)[1] & given and names(p)[0] & family]
    if len(found) == 1:
        return found[0]["id"], "family name, given name, birth date and gender"
    if found:
        return None, f"{len(found)} members have the same demographics"
    return None, "no member with these demographics"


def consent_complies(consent: dict | None, day: date) -> tuple[bool, str, dict | None]:
    """(complies, reason, recipient identifier)."""
    if not consent or consent.get("resourceType") != "Consent":
        return False, "no consent", None
    provision = consent.get("provision") or {}
    actors = provision.get("actor", [])

    def actor(role_code):
        return next((a.get("reference", {}) for a in actors
                     if any(c.get("code") == role_code for c in a.get("role", {}).get("coding", []))), None)

    recipient = (actor("IRCP") or {}).get("identifier")
    policies = {p.get("uri") for p in consent.get("policy", [])}
    if consent.get("status") != "active":
        return False, f"consent status {consent.get('status')}", recipient
    if provision.get("type", "permit") != "permit":
        return False, "the consent does not permit disclosure", recipient
    if not in_period(provision.get("period"), day):
        return False, "the consent is not in force today", recipient
    if not same_identifier((actor("performer") or {}).get("identifier"), PAYER_ID):
        return False, "the consent does not name this payer as the source", recipient
    if not recipient:
        return False, "the consent names no recipient", recipient
    if POLICY_SENSITIVE in policies:
        return False, "only non-sensitive data was consented, and this payer does not label sensitive data", recipient
    if POLICY_REGULAR not in policies:
        return False, "no HRex consent policy", recipient
    return True, "all data (regular policy)", recipient


def result_group(code: str, display: str, profile: str, recipient: dict | None, entries: list) -> dict:
    """A PDex Member Match / No Match Group. entries: (input Patient, our Patient id or None, reason)."""
    gid = f"p2p-{code}-{uuid.uuid4().hex[:16]}"
    contained, members = [], []
    used = set()
    for n, (patient, our_id, reason) in enumerate(entries, start=1):
        # The contained copy keeps the id the requester gave the Patient, so each
        # result can be traced to its MemberBundle (a position when it has none).
        cid = patient.get("id") if re.fullmatch(r"[A-Za-z0-9\-.]{1,64}", str(patient.get("id"))) else None
        cid = cid if cid and cid not in used else f"member-{n}"
        used.add(cid)
        copy = {k: v for k, v in patient.items() if k not in ("meta", "text")}
        copy["id"] = cid
        contained.append(copy)
        members.append({"entity": {
            "extension": [{"url": MATCH_PARAMETERS_EXT, "valueReference": {"reference": f"#{cid}"}}],
            "reference": f"Patient/{our_id}" if our_id else f"#{cid}",
            "display": reason}})
    coding = {"system": MATCH_RESULT_CS, "code": code, "display": display}
    group = {
        "resourceType": "Group", "id": gid,
        "meta": {"profile": [profile]},
        "text": {"status": "generated", "div": '<div xmlns="http://www.w3.org/1999/xhtml"><p>'
                 f"{display} members of a Payer-to-Payer request: {len(entries)}</p></div>"},
        "contained": contained,
        "type": "person", "actual": True,
        "code": {"coding": [coding]},
        "name": f"{display} members",
        "quantity": len(entries),
        "managingEntity": {"reference": "Organization/payer"},
        "member": members,
    }
    if recipient:
        group["identifier"] = [recipient]
        group["characteristic"] = [{"code": {"coding": [coding]}, "valueReference": {"identifier": recipient},
                                    "exclude": False, "period": {"start": today().isoformat()}}]
    if not contained:
        del group["contained"]
        del group["member"]
    return group


def bulk_member_match(upstream: Upstream, body: bytes) -> dict:
    try:
        params = json.loads(body or b"{}")
    except json.JSONDecodeError as e:
        raise FhirError(400, f"The body is not JSON: {e}", "invalid") from e
    bundles = [p.get("part", []) for p in params.get("parameter", []) if p.get("name") == "MemberBundle"]
    if params.get("resourceType") != "Parameters" or not bundles:
        raise FhirError(400, "Expected a Parameters resource with at least one MemberBundle", "invalid")
    day = today()
    matched, nomatch, constrained, recipients = [], [], [], []
    for parts in bundles:
        patient, coverage = bundle_part(parts, "MemberPatient"), bundle_part(parts, "CoverageToMatch")
        if not patient or not coverage:
            raise FhirError(400, "Each MemberBundle needs MemberPatient and CoverageToMatch", "required")
        complies, consent_reason, recipient = consent_complies(bundle_part(parts, "Consent"), day)
        if recipient:
            recipients.append(recipient)
        our_id, reason = match_member(upstream, patient, coverage)
        if our_id is None:
            nomatch.append((patient, None, reason))
        elif not complies:
            constrained.append((patient, None, consent_reason))
        else:
            matched.append((patient, our_id, reason))
    # The requesting payer: the recipient the consents name (one request, one requester).
    requester = recipients[0] if recipients and all(r == recipients[0] for r in recipients) else None
    match_group = result_group("match", "Matched", MATCH_GROUP, requester, matched)
    status, stored = upstream.json("PUT", f"Group/{match_group['id']}", match_group)
    if status not in (200, 201):
        raise FhirError(502, f"Could not store the match group: HTTP {status} {stored}")
    out = [{"name": "MatchedMembers", "resource": stored}]
    if nomatch:
        out.append({"name": "NonMatchedMembers",
                    "resource": result_group("nomatch", "Not Matched", NO_MATCH_GROUP, requester, nomatch)})
    if constrained:
        out.append({"name": "ConsentConstrainedMembers",
                    "resource": result_group("consentconstraint", "Consent Constraint", NO_MATCH_GROUP, requester,
                                             constrained)})
    return {"resourceType": "Parameters",
            "meta": {"profile": [PDEX + "StructureDefinition/pdex-parameters-multi-member-match-bundle-out"]},
            "parameter": out}


def member_match(upstream: Upstream, body: bytes) -> dict:
    try:
        params = json.loads(body or b"{}")
    except json.JSONDecodeError as e:
        raise FhirError(400, f"The body is not JSON: {e}", "invalid") from e
    parts = params.get("parameter", [])
    patient, coverage = bundle_part(parts, "MemberPatient"), bundle_part(parts, "CoverageToMatch")
    if not patient or not coverage:
        raise FhirError(400, "MemberPatient and CoverageToMatch are required", "required")
    our_id, reason = match_member(upstream, patient, coverage)
    if our_id is None:
        raise FhirError(422, f"No unique match: {reason}", "not-found")
    consent = bundle_part(parts, "Consent")
    if consent is not None:
        complies, consent_reason, _ = consent_complies(consent, today())
        if not complies:
            raise FhirError(422, f"The consent cannot be complied with: {consent_reason}", "business-rule")
    return {"resourceType": "Parameters", "parameter": [
        {"name": "MemberIdentifier", "valueIdentifier": {
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MB"}]},
            "system": MEMBER_ID_SYSTEM, "value": our_id, "assigner": {"identifier": PAYER_ID}}},
        {"name": "MemberId", "valueReference": {"reference": f"Patient/{our_id}"}}]}


def capability_statement(upstream: Upstream) -> dict:
    status, cs = upstream.json("GET", "metadata")
    if status != 200:
        raise FhirError(502, f"Upstream metadata failed with HTTP {status}")
    cs["instantiates"] = [PDEX + "CapabilityStatement/pdex-provider-access-server",
                          PDEX + "CapabilityStatement/pdex-payer-access-server"]
    for rest in cs.get("rest", []):
        # The Bulk $export operations are refused here (see the module docstring).
        rest["operation"] = [op for op in rest.get("operation", []) if op.get("name") != "export"]
        for res in rest.get("resource", []):
            res["operation"] = [op for op in res.get("operation", []) if op.get("name") != "export"]
            if res.get("type") == "Group":
                res.setdefault("operation", []).extend([
                    {"name": "davinci-data-export", "definition": ATR + "OperationDefinition/davinci-data-export"},
                    {"name": "bulk-member-match", "definition": PDEX + "OperationDefinition/BulkMemberMatch"}])
            if res.get("type") == "Patient":
                res.setdefault("operation", []).append(
                    {"name": "member-match", "definition": HREX + "OperationDefinition/member-match"})
    return cs


# --- HTTP ----------------------------------------------------------------------------

JOBS: dict[str, ExportJob] = {}
EXPORT_RE = re.compile(r"^Group/([A-Za-z0-9\-.]{1,64})/\$davinci-data-export$")
BLOCKED_RE = re.compile(r"(^|/)\$export$")
PASSTHROUGH_HEADERS = ("Accept", "Content-Type", "Prefer", "Cache-Control", "If-Match", "If-None-Exist")


class Handler(BaseHTTPRequestHandler):
    server_version = "payer-fhir-gateway"
    upstream: Upstream = None

    def public_base(self) -> str:
        return f"http://{self.headers.get('Host', 'localhost')}/fhir"

    def send(self, status: int, body: dict | bytes, content_type: str = "application/fhir+json",
             headers: dict | None = None) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def handle_any(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if not (parsed.path == "/fhir" or parsed.path.startswith("/fhir/")):
            raise FhirError(404, "The FHIR base is /fhir", "not-found")
        path = parsed.path[len("/fhir"):].strip("/")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        method = self.command

        if path == "metadata" and method == "GET":
            return self.send(200, capability_statement(self.upstream))
        if (m := EXPORT_RE.match(path)) and method in ("POST", "GET"):
            if method == "GET":
                raise FhirError(405, "$davinci-data-export SHALL be submitted with POST (PDex)", "not-supported")
            if "respond-async" not in (self.headers.get("Prefer") or ""):
                raise FhirError(400, "$davinci-data-export requires the header Prefer: respond-async", "invalid")
            job = ExportJob(self.upstream, m.group(1), export_parameters(query, self.body()),
                            f"{self.public_base()}/{path}" + (f"?{parsed.query}" if parsed.query else ""))
            job.prepare()
            JOBS[job.id] = job
            threading.Thread(target=job.run, args=(self.public_base(),), daemon=True).start()
            return self.send(202, outcome("Export accepted", "informational", "information"),
                             headers={"Content-Location": f"{self.public_base()}/$export-status/{job.id}"})
        if path.startswith("$export-status/"):
            job = JOBS.get(path.split("/", 1)[1])
            if job is None:
                raise FhirError(404, "Unknown export job", "not-found")
            if method == "DELETE":
                job.cancelled = True
                JOBS.pop(job.id, None)
                shutil.rmtree(job.dir, ignore_errors=True)
                return self.send(202, outcome("Export cancelled", "informational", "information"))
            if job.status == "running":
                return self.send(202, b"", headers={"X-Progress": job.progress, "Retry-After": "2"})
            if job.status == "failed":
                status, oo = job.error
                return self.send(status, oo)
            return self.send(200, job.manifest, "application/json",
                             headers={"Expires": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
                                 "%a, %d %b %Y %H:%M:%S GMT")})
        if path.startswith("$export-file/") and method == "GET":
            _, job_id, name = (path.split("/") + ["", ""])[:3]
            job = JOBS.get(job_id)
            if job is None or not re.fullmatch(r"[A-Za-z]+\.ndjson", name) or not (job.dir / name).exists():
                raise FhirError(404, "Unknown export file", "not-found")
            return self.send(200, (job.dir / name).read_bytes(), "application/fhir+ndjson")
        if path == "Group/$bulk-member-match" and method == "POST":
            return self.send(200, bulk_member_match(self.upstream, self.body()))
        if path == "Patient/$member-match" and method == "POST":
            return self.send(200, member_match(self.upstream, self.body()))
        if BLOCKED_RE.search(path):
            raise FhirError(400, "Bulk $export is not offered here: it would bypass the opt-out check. Use "
                                 "POST Group/[id]/$davinci-data-export", "not-supported")
        return self.passthrough(path, parsed.query)

    def passthrough(self, path: str, query: str) -> None:
        headers = {k: self.headers[k] for k in PASSTHROUGH_HEADERS if self.headers.get(k)}
        body = self.body() if self.command in ("POST", "PUT", "PATCH") else None
        status, resp_headers, data = self.upstream.request(
            self.command, f"{path}?{query}" if query else path, body, headers)
        data = data.replace(self.upstream.base.encode(), self.public_base().encode())
        extra = {k: v.replace(self.upstream.base, self.public_base()) for k, v in resp_headers.items()
                 if k.lower() in ("location", "content-location", "etag", "last-modified")}
        self.send(status, data, resp_headers.get("Content-Type", "application/fhir+json"), extra)

    def dispatch(self) -> None:
        try:
            self.handle_any()
        except FhirError as e:
            self.send(e.status, outcome(e.message, e.code))
        except Exception as e:  # noqa: BLE001 - answered as an OperationOutcome
            traceback.print_exc()
            self.send(500, outcome(f"Gateway error: {e}", "exception"))

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = dispatch

    def log_message(self, fmt, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--upstream", default=os.environ.get("UPSTREAM_FHIR_URL", "http://localhost:8091/fhir"))
    args = ap.parse_args()
    shutil.rmtree(JOBS_DIR, ignore_errors=True)
    Handler.upstream = Upstream(args.upstream)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"FHIR gateway on port {args.port}, upstream {args.upstream}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
