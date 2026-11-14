"""FHIR gateway: SMART authorization for the FHIR APIs, and the PDex operations.

Two instances, each in front of one HAPI server, which only they can reach:

  --api patient-access   (fhir-gateway-patient, port 8090, in front of
                         fhir-server) the Patient Access API, for apps a member
                         authorized (SMART App Launch, patient scopes);
  --api provider-access  (fhir-gateway, port 8092, in front of
                         fhir-server-provider) the Provider Access and
                         Payer-to-Payer APIs (SMART Backend Services), with the
                         Da Vinci PDex 2.1.0 operations HAPI does not implement.

Authorization. Every request but GET metadata and
.well-known/smart-configuration needs an access token from fhir-auth
(fhir/auth/server.py) issued for this API (smart.py verifies it); without one,
401. What the token allows depends on who it was issued to:

  a member's app         (patient-access, patient/ scopes) reads and searches
                         Patient, Coverage and ExplanationOfBenefit of its
                         patient only: a search gets the patient parameter added
                         (patient, or _id for Patient), one naming another member
                         is refused, and every answer is checked before it is
                         sent, so a read or a page with another member's data is
                         refused too (403). Organization is readable. Nothing
                         else: no writes, no history, no operations.
  a provider             (provider-access, role provider, fhirUser
                         Organization/<id>) reads its own attribution list
                         (Group/attribution-<id>, or searched by its NPI),
                         exports it with $davinci-data-export, and reads and
                         searches Patient, Coverage and ExplanationOfBenefit of
                         one member at a time: a search must name the member, who
                         must be on the provider's attribution list and have no
                         Provider Access opt-out in force, and the answer is
                         checked the same way (403 otherwise). Organization is
                         readable.
  another payer          (provider-access, role payer, with its payer
                         identifier) calls $bulk-member-match and $member-match,
                         where a consent counts only if it names the calling
                         payer as the recipient, and reads, searches (by its
                         identifier), exports and deletes its own match groups.
  the administration     (role admin, system/*.cruds) everything the server
  client                 offers: publishing, the checks' counts.

Each request also needs the scope of its interaction (r read, s search, c u d
writes) on its resource type; an export needs r on Group and on every type
exported, $bulk-member-match c on Group, $member-match s on Patient, the
deletion of a match group d on Group. A
$davinci-data-export job, its status and its files belong to the client that
started it (404 for any other).

Export jobs. A client may have at most EXPORT_MAX_RUNNING exports running (2 by
default); one more is refused with 429 and Retry-After. Once the gateway has read
what HAPI exported it deletes HAPI's copy (the Binary files and the job), so the
only copy is the job's own. A job ends with its expiry fixed: EXPORT_RETENTION_SECONDS
after it ended (an hour by default), the Expires the completed status announces.
After it the status and the files answer 404, and a sweeper removes expired jobs
and their files. Jobs live in memory: the gateway removes their directory when it
starts, so a restart leaves no file behind.

The PDex operations (provider-access):

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
        consent       Payer-to-Payer: a member whose consent period, kept on
                      the match group (member.period), does not cover the day
                      the export runs is left out, so a consent that ends after
                      the match applies (PDex: "The Data Export operation SHALL
                      check the consent status for each member at the time of
                      execution");
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
      can export it), NonMatchedMembers and ConsentConstrainedMembers. Each
      matched member's entry carries its consent's period (member.period);
      characteristic.period.start is the date of the match.

  DELETE [base]/Group/[id]   (a match group, by the payer that requested it)
      Revokes the consent (PDex: "the New Payer manages the Consent Revocation
      process"): the group is deleted, a new export of it is refused (404), and
      its exports, finished or running, answer 410: no manifest, no files.

  POST [base]/Patient/$member-match   (Da Vinci HRex 1.1.0, one member)
      200 with MemberIdentifier and MemberId, or 422 with an OperationOutcome
      when there is no unique match or the consent cannot be complied with.

  Prior authorization (Da Vinci CRD, DTR and PAS 2.1.0, for providers): CDS Hooks
  at /cds-services, Questionnaire/$questionnaire-package, Claim/$submit,
  Claim/$inquire and PAS Subscriptions, in prior_auth.py.

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

Python with PyJWT, cryptography and psycopg (fhir/gateway/Dockerfile), this directory
mounted.

A second instance of the same API (fhir-gateway-expiry, port 8095, for the checks)
accepts the first one's tokens (--token-audience), keeps exports 20 seconds, and
runs no prior authorization service (--no-prior-auth).

Usage:
    python fhir/gateway/gateway.py --api provider-access [--port 8092] [--upstream http://fhir-server-provider:8080/fhir]
        [--public-base http://localhost:8092/fhir] [--auth http://localhost:8093]
        [--auth-public http://localhost:8093] [--token-audience URL] [--no-prior-auth]
    environment: EXPORT_RETENTION_SECONDS (3600), EXPORT_MAX_RUNNING (2)
"""

import argparse
import json
import os
import re
import shutil
import sys
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smart import Token, Unauthorized, Verifier, security, smart_configuration  # noqa: E402
import prior_auth  # noqa: E402

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

    def __init__(self, status: int, message: str, code: str = "processing", headers: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.code, self.headers = status, message, code, headers or {}


def forbidden(message: str) -> FhirError:
    return FhirError(403, message, "forbidden")


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


def discard_upstream_export(upstream: Upstream, poll: str, manifest: dict) -> None:
    """Delete the Binary files of a finished HAPI export, and the job itself."""
    for output in manifest.get("output", []) + manifest.get("error", []):
        status, _, data = upstream.request("DELETE", output["url"], headers={"Accept": "application/fhir+json"})
        if status not in (200, 204, 404, 410):
            print(f"Could not delete the upstream export file {output['url']}: HTTP {status} {data[:200]!r}",
                  flush=True)
    upstream.request("DELETE", poll)


def revoked(group_id: str) -> FhirError:
    return FhirError(410, f"Group/{group_id} was deleted by the requesting payer (the members' consent was "
                          "revoked); its exports are no longer available", "deleted")


class ExportJob:
    def __init__(self, upstream: Upstream, group_id: str, params: dict, request_url: str, owner: str | None = None):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.upstream, self.group_id, self.params, self.request_url = upstream, group_id, params, request_url
        self.status, self.progress, self.manifest, self.error = "running", "queued", None, None
        self.dir = JOBS_DIR / self.id
        self.cancelled = False
        self.revoked = False
        self.expires_at: datetime | None = None  # set when the job ends: then + RETENTION

    def revoke(self) -> None:
        """The match group was deleted: the job fails and its files are removed."""
        error = revoked(self.group_id)
        self.revoked = self.cancelled = True
        self.status, self.error, self.manifest = "failed", (error.status, outcome(error.message, error.code)), None
        self.expires_at = datetime.now(timezone.utc) + RETENTION
        shutil.rmtree(self.dir, ignore_errors=True)

    def single(self, name: str) -> str | None:
        values = [v for v in self.params.get(name, []) if v]
        if len(values) > 1:
            raise FhirError(400, f"{name} can be given only once", "invalid")
        return values[0] if values else None

    def listed(self, name: str) -> list[str]:
        return [x.strip() for v in self.params.get(name, []) if v for x in v.split(",") if x.strip()]

    def run(self, public_base: str) -> None:
        try:
            result = ("completed", self.export(public_base), None)
        except FhirError as e:
            result = ("failed", None, (e.status, outcome(e.message, e.code)))
        except Exception as e:  # noqa: BLE001 - reported to the client as a failed job
            traceback.print_exc()
            result = ("failed", None, (500, outcome(f"Export failed: {e}", "exception")))
        if self.revoked:  # revoke() has set the outcome
            return
        # The expiry is fixed when the job ends, before the status shows it ended.
        self.expires_at = datetime.now(timezone.utc) + RETENTION
        self.status, self.manifest, self.error = result

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
        if group is None:
            raise revoked(self.group_id)
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
        if kind == "payertopayer":
            # PDex: "The Data Export operation SHALL check the consent status for each
            # member at the time of execution": the consent period kept at the match.
            for ref in list(selected):
                period = members[ref].get("period")
                if not in_period(period, started.date()):
                    when = (f"ended {period['end'][:10]}" if period.get("end")
                            and date.fromisoformat(period["end"][:10]) < started.date()
                            else f"starts {period.get('start', '')[:10]}")
                    issues.append(outcome(f"{ref}: the member's consent to Payer-to-Payer exchange {when}; left out",
                                          "suppressed", "information"))
                    selected.remove(ref)
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

        # Take what HAPI exported, then delete HAPI's copies (its Binary files and
        # the job), so the only copy left is this job's, which expires.
        self.progress = "downloading"
        try:
            exported = []
            for output in upstream_manifest.get("output", []):
                status, _, data = self.upstream.request("GET", output["url"],
                                                        headers={"Accept": "application/fhir+ndjson"})
                if status != 200:
                    raise FhirError(502, f"Upstream export file {output['url']} failed with HTTP {status}")
                exported.append(data)
        finally:
            discard_upstream_export(self.upstream, poll, upstream_manifest)

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
        for data in exported:
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

        # A match group deleted while the export ran (the requesting payer revoked
        # the consent) leaves no files.
        if self.cancelled:
            raise FhirError(404, "Export cancelled", "not-found")
        if kind == "payertopayer" and self.upstream.read("Group", self.group_id) is None:
            raise revoked(self.group_id)

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


def consent_complies(consent: dict | None, day: date, caller: dict | None = None) -> tuple[bool, str, dict | None]:
    """(complies, reason, recipient identifier). With caller (the requesting payer's
    identifier, from its access token), the consent must name it as the recipient."""
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
    if caller and not same_identifier(recipient, caller):
        return False, "the consent names another recipient than the requesting payer", recipient
    if POLICY_SENSITIVE in policies:
        return False, "only non-sensitive data was consented, and this payer does not label sensitive data", recipient
    if POLICY_REGULAR not in policies:
        return False, "no HRex consent policy", recipient
    return True, "all data (regular policy)", recipient


def consent_period(consent: dict) -> dict | None:
    """The period of an HRex consent (provision.period), as start and end only."""
    period = (consent.get("provision") or {}).get("period") or {}
    return {k: period[k] for k in ("start", "end") if period.get(k)} or None


def result_group(code: str, display: str, profile: str, recipient: dict | None, entries: list) -> dict:
    """A PDex Member Match / No Match Group. entries: (input Patient, our Patient id or None, reason[,
    consent period]). A matched member's consent period goes on its member.period, which the
    export checks again when it runs."""
    gid = f"p2p-{code}-{uuid.uuid4().hex[:16]}"
    contained, members = [], []
    used = set()
    for n, (patient, our_id, reason, *period) in enumerate(entries, start=1):
        # The contained copy keeps the id the requester gave the Patient, so each
        # result can be traced to its MemberBundle (a position when it has none).
        cid = patient.get("id") if re.fullmatch(r"[A-Za-z0-9\-.]{1,64}", str(patient.get("id"))) else None
        cid = cid if cid and cid not in used else f"member-{n}"
        used.add(cid)
        copy = {k: v for k, v in patient.items() if k not in ("meta", "text")}
        copy["id"] = cid
        contained.append(copy)
        member = {"entity": {
            "extension": [{"url": MATCH_PARAMETERS_EXT, "valueReference": {"reference": f"#{cid}"}}],
            "reference": f"Patient/{our_id}" if our_id else f"#{cid}",
            "display": reason}}
        if period and period[0]:
            member["period"] = period[0]
        members.append(member)
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


def bulk_member_match(upstream: Upstream, body: bytes, caller: dict | None = None) -> dict:
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
        consent = bundle_part(parts, "Consent")
        complies, consent_reason, recipient = consent_complies(consent, day, caller)
        if recipient:
            recipients.append(recipient)
        our_id, reason = match_member(upstream, patient, coverage)
        if our_id is None:
            nomatch.append((patient, None, reason))
        elif not complies:
            constrained.append((patient, None, consent_reason))
        else:
            matched.append((patient, our_id, reason, consent_period(consent)))
    # The requesting payer: the caller (its access token), or without one the
    # recipient the consents name (one request, one requester).
    requester = caller or (recipients[0] if recipients and all(r == recipients[0] for r in recipients) else None)
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


def member_match(upstream: Upstream, body: bytes, caller: dict | None = None) -> dict:
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
        complies, consent_reason, _ = consent_complies(consent, today(), caller)
        if not complies:
            raise FhirError(422, f"The consent cannot be complied with: {consent_reason}", "business-rule")
    return {"resourceType": "Parameters", "parameter": [
        {"name": "MemberIdentifier", "valueIdentifier": {
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MB"}]},
            "system": MEMBER_ID_SYSTEM, "value": our_id, "assigner": {"identifier": PAYER_ID}}},
        {"name": "MemberId", "valueReference": {"reference": f"Patient/{our_id}"}}]}


def capability_statement(upstream: Upstream, api: str, auth_public: str) -> dict:
    status, cs = upstream.json("GET", "metadata")
    if status != 200:
        raise FhirError(502, f"Upstream metadata failed with HTTP {status}")
    for rest in cs.get("rest", []):
        rest["security"] = security(auth_public, backend=api == "provider-access")
    if api == "patient-access":
        return cs
    cs["instantiates"] = [PDEX + "CapabilityStatement/pdex-provider-access-server",
                          PDEX + "CapabilityStatement/pdex-payer-access-server"]
    for rest in cs.get("rest", []):
        prior_auth.add_capabilities(rest)
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


# --- Authorization ----------------------------------------------------------------

COMPARTMENT_TYPES = ("Patient", "Coverage", "ExplanationOfBenefit")
# The search parameters that name the member, by type.
PATIENT_PARAMS = {"Patient": ("_id",), "Coverage": ("patient", "beneficiary"), "ExplanationOfBenefit": ("patient",)}
TYPE_RE = re.compile(r"^[A-Z][A-Za-z]+$")
ID_RE = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


def interaction(path: str, method: str, query: dict) -> tuple[str, str | None, str | None]:
    """(interaction, resource type, id) of a REST request; 'other' for anything else."""
    parts = path.split("/") if path else []
    if not parts:
        if method == "GET" and "_getpages" in query:
            return "page", None, None
        return ("transaction" if method == "POST" else "other"), None, None
    if not TYPE_RE.match(parts[0]):
        return "other", None, None
    rtype = parts[0]
    if len(parts) == 1:
        return {"GET": "search", "POST": "create"}.get(method, "other"), rtype, None
    if not ID_RE.match(parts[1]):
        return "other", rtype, None
    if len(parts) == 2:
        return {"GET": "read", "PUT": "update", "PATCH": "update", "DELETE": "delete"}.get(method, "other"), rtype, \
            parts[1]
    if len(parts) == 4 and parts[2] == "_history" and method == "GET":
        return "vread", rtype, parts[1]
    return "other", rtype, parts[1]


def named_patients(rtype: str, query: dict) -> set[str]:
    """The members a search names (Patient ids), through the patient parameter of its type."""
    names, found = PATIENT_PARAMS[rtype], set()
    for key, values in query.items():
        base = re.split(r"[:.]", key, maxsplit=1)[0]
        if base not in names:
            continue
        if key != base:
            raise forbidden(f"Modifiers and chains on {base} are not allowed: name the member directly")
        for value in values:
            for v in value.split(","):
                v = v.strip()
                found.add(v.removeprefix("Patient/") if rtype != "Patient" or base != "_id" else v)
    return found


def keep_member_field(rtype: str, query: dict) -> dict:
    """With _elements, also ask for the element that names the member, so the answer can be checked."""
    if rtype in ("Coverage", "ExplanationOfBenefit") and query.get("_elements"):
        field = "beneficiary" if rtype == "Coverage" else "patient"
        return {**query, "_elements": [",".join([*query["_elements"], field])]}
    return query


def check_answer(data: bytes, allowed: set[str], allowed_types: tuple = ("Organization",)) -> None:
    """Refuse (403) an answer that holds a resource outside what the caller may see:
    a compartment resource of a member not in `allowed` ('Patient/<id>'), or a type
    that is neither a compartment type nor in allowed_types."""
    try:
        body = json.loads(data)
    except json.JSONDecodeError as e:
        raise forbidden("The answer could not be checked") from e
    if body.get("resourceType") == "Bundle":
        resources = [e["resource"] for e in body.get("entry", []) if "resource" in e]
    else:
        resources = [body]
    for r in resources:
        rtype = r.get("resourceType")
        if rtype in COMPARTMENT_TYPES:
            if patient_of(r) not in allowed:
                raise forbidden(f"The answer holds data of a member this client may not see ({rtype}/{r.get('id')})")
        elif rtype not in (*allowed_types, "OperationOutcome"):
            raise forbidden(f"The answer holds a {rtype}, which this client may not see")


def require(token: Token, rtype: str, permission: str, context: str = "system") -> None:
    if not token.can(context, rtype, permission):
        raise forbidden(f"The access token has no scope allowing '{permission}' on {rtype}")


# --- HTTP ----------------------------------------------------------------------------

JOBS: dict[str, ExportJob] = {}
JOBS_LOCK = threading.Lock()
# How long a finished export (its manifest and files) stays available, and how
# many exports one client may have running at a time.
RETENTION = timedelta(seconds=int(os.environ.get("EXPORT_RETENTION_SECONDS", "3600")))
MAX_RUNNING_PER_CLIENT = int(os.environ.get("EXPORT_MAX_RUNNING", "2"))


def http_date(at: datetime) -> str:
    return at.strftime("%a, %d %b %Y %H:%M:%S GMT")


def sweep_jobs(now: datetime | None = None) -> int:
    """Remove the expired jobs and their files; the number removed."""
    now = now or datetime.now(timezone.utc)
    with JOBS_LOCK:
        expired = [j for j in JOBS.values() if j.expires_at and j.expires_at <= now]
        for job in expired:
            JOBS.pop(job.id, None)
    for job in expired:
        shutil.rmtree(job.dir, ignore_errors=True)
    return len(expired)


def sweeper() -> None:
    interval = max(1.0, min(60.0, RETENTION.total_seconds() / 4))
    while True:
        threading.Event().wait(interval)
        try:
            sweep_jobs()
        except Exception:  # noqa: BLE001 - the sweeper keeps running
            traceback.print_exc()
EXPORT_RE = re.compile(r"^Group/([A-Za-z0-9\-.]{1,64})/\$davinci-data-export$")
BLOCKED_RE = re.compile(r"(^|/)\$export$")
PRIOR_AUTH_RE = re.compile(r"^(Claim/\$(submit|inquire)|Questionnaire/\$questionnaire-package|"
                           r"Subscription(/[A-Za-z0-9\-]{1,64}(/\$status)?)?)$")
HAPI_EXPORT_RE = re.compile(r"(^|/)\$export(-poll-status)?$|^Binary/")
PASSTHROUGH_HEADERS = ("Accept", "Content-Type", "Prefer", "Cache-Control", "If-Match", "If-None-Exist")


class Handler(BaseHTTPRequestHandler):
    server_version = "payer-fhir-gateway"
    upstream: Upstream = None
    api: str = "provider-access"
    verifier: Verifier = None
    auth_public: str = ""
    prior_auth: "prior_auth.PriorAuthService" = None

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
        if not hasattr(self, "_body"):
            length = int(self.headers.get("Content-Length") or 0)
            self._body = self.rfile.read(length) if length else b""
        return self._body

    def authorize(self) -> Token:
        try:
            token = self.verifier.verify(self.headers.get("Authorization"))
        except Unauthorized as e:
            raise FhirError(401, str(e), "login", {"WWW-Authenticate": f'Bearer realm="{self.api}", '
                                                                      f'error="invalid_token"'}) from e
        if token.claims.get("api") != self.api:
            raise FhirError(401, f"The access token was not issued for the {self.api} API", "login")
        self.token = token
        return token

    def handle_any(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if self.api == "provider-access" and (parsed.path == "/cds-services" or parsed.path.startswith("/cds-services/")):
            return self.cds_hooks(parsed.path.strip("/"))
        if not (parsed.path == "/fhir" or parsed.path.startswith("/fhir/")):
            raise FhirError(404, "The FHIR base is /fhir", "not-found")
        path = parsed.path[len("/fhir"):].strip("/")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        method = self.command

        if path == "metadata" and method == "GET":
            return self.send(200, capability_statement(self.upstream, self.api, self.auth_public))
        if path == ".well-known/smart-configuration" and method == "GET":
            return self.send(200, smart_configuration(self.auth_public, backend=self.api == "provider-access"),
                             "application/json")
        token = self.authorize()

        if path.startswith("$export-status/") or path.startswith("$export-file/"):
            return self.export_job(path, method)
        if self.api == "patient-access":
            if token.role == "admin":
                require(token, "*", "r")
                return self.passthrough(path, parsed.query)
            return self.patient_access(token, path, query, method)
        if PRIOR_AUTH_RE.match(path):
            return self.prior_authorization(token, path, method)
        if BLOCKED_RE.search(path):
            raise FhirError(400, "Bulk $export is not offered here: it would bypass the opt-out check. Use "
                                 "POST Group/[id]/$davinci-data-export", "not-supported")
        if (m := EXPORT_RE.match(path)) and method in ("POST", "GET"):
            return self.start_export(m.group(1), path, parsed, query, method)
        if path == "Group/$bulk-member-match" and method == "POST":
            if token.role not in ("payer", "admin"):
                raise forbidden("$bulk-member-match is for other payers (Payer-to-Payer)")
            require(token, "Group", "c")
            return self.send(200, bulk_member_match(self.upstream, self.body(), token.payer_identifier))
        if path == "Patient/$member-match" and method == "POST":
            if token.role not in ("payer", "admin"):
                raise forbidden("$member-match is for other payers (Payer-to-Payer)")
            require(token, "Patient", "s")
            return self.send(200, member_match(self.upstream, self.body(), token.payer_identifier))
        if token.role == "admin":
            require(token, "*", "r")
            return self.passthrough(path, parsed.query)
        if token.role == "provider":
            return self.provider_access(token, path, query, method)
        if token.role == "payer":
            return self.payer_access(token, path, query, method)
        raise forbidden("This client has no role on this API")

    # --- per client ---------------------------------------------------------------

    def rest_scope(self, token: Token, kind: str, rtype: str | None, context: str = "system") -> None:
        permission = {"read": "r", "vread": "r", "search": "s"}.get(kind)
        if permission is None or rtype is None:
            raise forbidden(f"This client may only read and search (not {self.command} {kind})")
        require(token, rtype, permission, context)

    def patient_access(self, token: Token, path: str, query: dict, method: str) -> None:
        """A member's app: its patient's compartment, and Organizations."""
        if not token.patient:
            raise forbidden("The access token has no patient context (launch/patient)")
        if HAPI_EXPORT_RE.search(path):
            raise forbidden("Bulk export is not offered to apps")
        kind, rtype, _ = interaction(path, method, query)
        allowed = {f"Patient/{token.patient}"}
        if kind == "page":
            if not any("s" in perms for (ctx, _), perms in token.scopes.items() if ctx == "patient"):
                raise forbidden("The access token has no search scope")
        else:
            if rtype not in (*COMPARTMENT_TYPES, "Organization"):
                raise forbidden(f"{rtype or 'This request'} is not offered to apps")
            self.rest_scope(token, kind, rtype, "patient")
            if kind == "search" and rtype in COMPARTMENT_TYPES:
                named = named_patients(rtype, query)
                if named - {token.patient}:
                    raise forbidden("The search names another member than the one who authorized the app")
                if not named:
                    query = {**query, PATIENT_PARAMS[rtype][0]: [token.patient if rtype == "Patient"
                                                                 else f"Patient/{token.patient}"]}
            query = keep_member_field(rtype, query)
        self.forward_checked(path, query, allowed)

    def provider_scope(self, token: Token) -> tuple[str, dict, set[str]]:
        """The provider's attribution list (its Group id and resource) and the members
        it may see: on the list, and without a Provider Access opt-out in force."""
        provider = (token.fhir_user or "").removeprefix("Organization/")
        group_id = f"attribution-{provider}"
        group = self.upstream.read("Group", group_id) if provider else None
        if group is None or group_kind(group) != "provider":
            raise forbidden("This provider has no attribution list")
        members = {m["entity"]["reference"] for m in group.get("member", [])
                   if not m.get("inactive") and m.get("entity", {}).get("reference", "").startswith("Patient/")}
        return group_id, group, members - opted_out_members(self.upstream, today())

    def provider_access(self, token: Token, path: str, query: dict, method: str) -> None:
        kind, rtype, rid = interaction(path, method, query)
        group_id, group, allowed = self.provider_scope(token)
        if rtype == "Group":
            self.rest_scope(token, kind, rtype)
            if kind in ("read", "vread") and rid != group_id:
                raise forbidden("A provider may read only its own attribution list")
            if kind == "search":
                npis = {f"{i.get('system')}|{i.get('value')}" for i in group.get("identifier", [])}
                ids = [v for k, vs in query.items() if k == "_id" for v in vs]
                identifiers = [v for k, vs in query.items() if k == "identifier" for v in vs]
                if not (ids == [group_id] or (identifiers and all(v in npis for v in identifiers))):
                    raise forbidden("A provider may search only its own attribution list (by its NPI or id)")
            return self.forward_checked(path, query, set(), ("Group", "Organization"), group_ids={group_id})
        if rtype == "Organization":
            self.rest_scope(token, kind, rtype)
            return self.forward_checked(path, query, set())
        if kind == "page":
            return self.forward_checked(path, query, allowed, ("Group", "Organization"), group_ids={group_id})
        if rtype not in COMPARTMENT_TYPES:
            raise forbidden(f"{rtype or 'This request'} is not offered to providers")
        self.rest_scope(token, kind, rtype)
        if kind == "search":
            named = named_patients(rtype, query)
            if len(named) != 1:
                raise forbidden("A provider's search must name one member (patient, or _id for Patient)")
            member = f"Patient/{next(iter(named))}"
            if member not in allowed:
                raise forbidden(f"{member} is not on this provider's attribution list or has opted out of "
                                "Provider Access")
            return self.forward_checked(path, keep_member_field(rtype, query), {member})
        return self.forward_checked(path, keep_member_field(rtype, query), allowed)

    def payer_access(self, token: Token, path: str, query: dict, method: str) -> None:
        kind, rtype, rid = interaction(path, method, query)
        if rtype != "Group":
            raise forbidden("Another payer may only match members and export its match groups")
        caller = token.payer_identifier or {}
        if kind == "delete":
            return self.revoke_match_group(token, rid, caller)
        self.rest_scope(token, kind, rtype)
        if kind == "search":
            wanted = f"{caller.get('system')}|{caller.get('value')}"
            if [v for k, vs in query.items() if k == "identifier" for v in vs] != [wanted]:
                raise forbidden("A payer may search only its own match groups (identifier=<its payer identifier>)")
        status, headers, data = self.fetch(path, query)
        if status == 200:
            body = json.loads(data)
            groups = [e["resource"] for e in body.get("entry", [])] if body.get("resourceType") == "Bundle" else [body]
            for g in groups:
                # A search is limited to the caller's identifier above (and _elements may
                # leave out what identifies a match group); a read is checked in full.
                if g.get("resourceType") == "Group" and kind != "search" and not (
                        group_kind(g) == "payertopayer"
                        and any(same_identifier(i, caller) for i in g.get("identifier", []))):
                    raise forbidden("This match group belongs to another payer")
                if g.get("resourceType") not in ("Group", "OperationOutcome"):
                    raise forbidden(f"The answer holds a {g.get('resourceType')}, which this client may not see")
        self.send(status, data, headers.get("Content-Type", "application/fhir+json"))

    def revoke_match_group(self, token: Token, group_id: str, caller: dict) -> None:
        """The requesting payer deletes its match group (PDex: "the New Payer manages
        the Consent Revocation process"): no further export of it, and its exports'
        files, finished or not, are no longer served."""
        require(token, "Group", "d")
        group = self.upstream.read("Group", group_id)
        if group is None:
            raise FhirError(404, f"Group/{group_id} not found", "not-found")
        if group_kind(group) != "payertopayer" or not any(same_identifier(i, caller)
                                                          for i in group.get("identifier", [])):
            raise forbidden("A payer may delete only its own match groups")
        status, _, data = self.upstream.request("DELETE", f"Group/{urllib.parse.quote(group_id)}",
                                                headers={"Accept": "application/fhir+json"})
        if status not in (200, 204):
            raise FhirError(502, f"Upstream delete failed with HTTP {status}: {data[:300].decode(errors='replace')}")
        jobs = [j for j in list(JOBS.values()) if j.group_id == group_id]
        for job in jobs:
            job.revoke()
        self.send(200, outcome(f"Group/{group_id} deleted: the consent is revoked, {len(jobs)} export(s) withdrawn",
                               "informational", "information"))

    # --- prior authorization (Da Vinci CRD, DTR, PAS: prior_auth.py) ------------------

    def provider_id(self, token: Token) -> str:
        if token.role != "provider" or not (token.fhir_user or "").startswith("Organization/"):
            raise forbidden("Prior authorization is for providers (a token acting for an Organization)")
        return token.fhir_user.removeprefix("Organization/")

    def json_body(self) -> dict:
        try:
            body = json.loads(self.body() or b"null")
        except json.JSONDecodeError as e:
            raise FhirError(400, f"The body is not JSON: {e}", "invalid") from e
        if not isinstance(body, dict):
            raise FhirError(400, "The body must be a JSON object", "invalid")
        return body

    def cds_hooks(self, path: str) -> None:
        # Discovery is public, as metadata is; a service call needs a provider's token.
        if path == "cds-services" and self.command == "GET":
            return self.send(200, prior_auth.cds_discovery(), "application/json")
        self.provider_id(self.authorize())
        if self.command != "POST":
            raise FhirError(405, "A CDS service is called with POST", "not-supported")
        answer = prior_auth.cds_service(self.upstream, path.removeprefix("cds-services/"), self.json_body())
        self.send(200, answer, "application/json")

    def prior_authorization(self, token: Token, path: str, method: str) -> None:
        if self.prior_auth is None:
            raise FhirError(501, "Prior authorization is not offered by this instance (--no-prior-auth)",
                            "not-supported")
        provider = self.provider_id(token)
        base = self.public_base()
        if path == "Questionnaire/$questionnaire-package" and method == "POST":
            require(token, "Questionnaire", "r")
            return self.send(200, prior_auth.questionnaire_package(self.json_body()))
        if path == "Claim/$submit" and method == "POST":
            require(token, "Claim", "c")
            return self.send(200, prior_auth.submit(self.upstream, base, provider, self.json_body(), self.prior_auth))
        if path == "Claim/$inquire" and method == "POST":
            require(token, "Claim", "r")
            return self.send(200, prior_auth.inquire(self.upstream, base, provider, self.json_body()))
        parts = path.split("/")
        if path == "Subscription" and method == "POST":
            require(token, "Subscription", "c")
            created = self.prior_auth.create(provider, self.json_body())
            return self.send(201, created, headers={"Location": f"{base}/Subscription/{created['id']}"})
        if len(parts) == 2 and method == "GET":
            require(token, "Subscription", "r")
            return self.send(200, self.prior_auth.get(provider, parts[1])["resource"])
        if len(parts) == 2 and method == "DELETE":
            require(token, "Subscription", "d")
            self.prior_auth.delete(provider, parts[1])
            return self.send(200, outcome("Subscription deleted", "informational", "information"))
        if len(parts) == 3 and parts[2] == "$status" and method == "GET":
            require(token, "Subscription", "r")
            return self.send(200, self.prior_auth.status_bundle(provider, parts[1]))
        raise FhirError(405, f"{method} {path} is not offered", "not-supported")

    # --- $davinci-data-export -------------------------------------------------------

    def start_export(self, group_id: str, path: str, parsed, query: dict, method: str) -> None:
        token = self.token
        if method == "GET":
            raise FhirError(405, "$davinci-data-export SHALL be submitted with POST (PDex)", "not-supported")
        if "respond-async" not in (self.headers.get("Prefer") or ""):
            raise FhirError(400, "$davinci-data-export requires the header Prefer: respond-async", "invalid")
        require(token, "Group", "r")
        if token.role == "provider":
            own, _, _ = self.provider_scope(token)
            if group_id != own:
                raise forbidden("A provider may export only its own attribution list")
        elif token.role == "payer":
            group = self.upstream.read("Group", group_id)
            if group is None:  # never created, or deleted (a revoked consent)
                raise FhirError(404, f"Group/{group_id} not found", "not-found")
            if group_kind(group) != "payertopayer" or not any(
                    same_identifier(i, token.payer_identifier or {}) for i in group.get("identifier", [])):
                raise forbidden("A payer may export only its own match groups")
        elif token.role != "admin":
            raise forbidden("This client may not export")
        job = ExportJob(self.upstream, group_id, export_parameters(query, self.body()),
                        f"{self.public_base()}/{path}" + (f"?{parsed.query}" if parsed.query else ""),
                        token.client_id)
        job.prepare()
        for rtype in job.types:
            require(token, rtype, "r")
        with JOBS_LOCK:
            running = sum(j.owner == token.client_id and j.status == "running" for j in JOBS.values())
            if running >= MAX_RUNNING_PER_CLIENT:
                # Bulk Data: "429 Too Many Requests" with Retry-After, as a server may.
                raise FhirError(429, f"This client already has {running} export(s) running (at most "
                                     f"{MAX_RUNNING_PER_CLIENT}); retry when one has finished", "throttled",
                                {"Retry-After": "20"})
            JOBS[job.id] = job
        threading.Thread(target=job.run, args=(self.public_base(),), daemon=True).start()
        return self.send(202, outcome("Export accepted", "informational", "information"),
                         headers={"Content-Location": f"{self.public_base()}/$export-status/{job.id}"})

    def export_job(self, path: str, method: str) -> None:
        kind, job_id, name = (path.split("/") + ["", ""])[:3]
        job = JOBS.get(job_id)
        if job is None or (job.owner != self.token.client_id and self.token.role != "admin"):
            raise FhirError(404, "Unknown export job", "not-found")
        if job.expires_at and job.expires_at <= datetime.now(timezone.utc):
            # Past the Expires the manifest announced: gone, as if swept already.
            sweep_jobs()
            raise FhirError(404, f"The export expired at {http_date(job.expires_at)}; its files were deleted",
                            "not-found")
        if kind == "$export-file":
            if job.revoked:
                raise revoked(job.group_id)
            if method != "GET" or not re.fullmatch(r"[A-Za-z]+\.ndjson", name) or not (job.dir / name).exists():
                raise FhirError(404, "Unknown export file", "not-found")
            return self.send(200, (job.dir / name).read_bytes(), "application/fhir+ndjson")
        if method == "DELETE":
            job.cancelled = True
            with JOBS_LOCK:
                JOBS.pop(job.id, None)
            shutil.rmtree(job.dir, ignore_errors=True)
            return self.send(202, outcome("Export cancelled", "informational", "information"))
        if job.status == "running":
            return self.send(202, b"", headers={"X-Progress": job.progress, "Retry-After": "2"})
        if job.status == "failed":
            status, oo = job.error
            return self.send(status, oo)
        return self.send(200, job.manifest, "application/json", headers={"Expires": http_date(job.expires_at)})

    # --- upstream -------------------------------------------------------------------

    def fetch(self, path: str, query: dict) -> tuple[int, dict, bytes]:
        """A GET to the upstream server, with its URLs rewritten to this gateway."""
        headers = {k: self.headers[k] for k in PASSTHROUGH_HEADERS if self.headers.get(k)}
        qs = urllib.parse.urlencode(query, doseq=True)
        status, resp_headers, data = self.upstream.request("GET", f"{path}?{qs}" if qs else path, None, headers)
        return status, resp_headers, data.replace(self.upstream.base.encode(), self.public_base().encode())

    def forward_checked(self, path: str, query: dict, allowed: set[str], allowed_types: tuple = ("Organization",),
                        group_ids: set[str] = frozenset()) -> None:
        """Forward a read or search and check the answer before sending it."""
        status, headers, data = self.fetch(path, query)
        if status == 200:
            check_answer(data, allowed, allowed_types)
            if group_ids:
                body = json.loads(data)
                items = [e["resource"] for e in body.get("entry", [])] if body.get("resourceType") == "Bundle" \
                    else [body]
                if any(r.get("resourceType") == "Group" and r.get("id") not in group_ids for r in items):
                    raise forbidden("The answer holds another provider's attribution list")
        self.send(status, data, headers.get("Content-Type", "application/fhir+json"))

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
            self.send(e.status, outcome(e.message, e.code), headers=e.headers)
        except prior_auth.PAError as e:
            self.send(e.status, outcome(e.message, e.code))
        except Exception as e:  # noqa: BLE001 - answered as an OperationOutcome
            traceback.print_exc()
            self.send(500, outcome(f"Gateway error: {e}", "exception"))

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = dispatch

    def log_message(self, fmt, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", choices=["patient-access", "provider-access"],
                    default=os.environ.get("GATEWAY_API", "provider-access"))
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--upstream", default=os.environ.get("UPSTREAM_FHIR_URL", "http://localhost:8091/fhir"))
    ap.add_argument("--public-base", default=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8092/fhir"),
                    help="this gateway's FHIR base, the audience its access tokens must have")
    ap.add_argument("--token-audience", default=os.environ.get("TOKEN_AUDIENCE"),
                    help="the audience its access tokens must have, when not its own FHIR base (a second "
                         "instance of the same API)")
    ap.add_argument("--no-prior-auth", action="store_true", default=os.environ.get("PRIOR_AUTH") == "off",
                    help="do not run the prior authorization service (its notifications), as a second instance")
    ap.add_argument("--auth", default=os.environ.get("AUTH_INTERNAL_URL", "http://localhost:8093"),
                    help="the authorization server, as this gateway reaches it (for its keys)")
    ap.add_argument("--auth-public", default=os.environ.get("AUTH_PUBLIC_URL", "http://localhost:8093"),
                    help="the authorization server, as clients reach it (the token issuer)")
    args = ap.parse_args()
    shutil.rmtree(JOBS_DIR, ignore_errors=True)
    Handler.upstream = Upstream(args.upstream)
    Handler.api = args.api
    Handler.auth_public = args.auth_public.rstrip("/")
    Handler.verifier = Verifier(f"{args.auth.rstrip('/')}/jwks", args.auth_public,
                                args.token_audience or args.public_base)
    threading.Thread(target=sweeper, daemon=True).start()
    if args.api == "provider-access" and not args.no_prior_auth:
        Handler.prior_auth = prior_auth.PriorAuthService(Handler.upstream, args.public_base)
        Handler.prior_auth.start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"FHIR gateway ({args.api}) on port {args.port}, upstream {args.upstream}, exports kept "
          f"{int(RETENTION.total_seconds())} s, at most {MAX_RUNNING_PER_CLIENT} running per client", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
