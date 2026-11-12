"""Check the SMART authorization of the FHIR APIs (make fhir-security-check).

Calls the authorization server and both gateways as the clients would, the
allowed requests and the ones that must be refused, and compares the data an
allowed request returns with gold:

  discovery        .well-known/smart-configuration and the CapabilityStatement
                   security of both APIs;
  authentication   no token, a malformed one, one signed by another key, one
                   issued for the other API (401); the HAPI servers have no port
                   on the host;
  token endpoint   Backend Services: a valid client assertion, an unknown
                   client, another client's key, a replayed jti, a wrong
                   audience, an assertion valid too long, a scope the client may
                   not have, a narrower scope granted;
  member login     a wrong birth date, an unregistered redirect URI, no PKCE, a
                   denial, a wrong PKCE verifier, a code used twice; the token's
                   patient context and id_token;
  member app       its patient's Patient, Coverage and claims equal to gold,
                   with and without the patient parameter and across pages;
                   another member's data refused by search, by read and by
                   chain; no writes, operations or bulk export; a token with
                   fewer scopes; refresh token rotation;
  provider         a member on its attribution list: Patient, Coverage and
                   claims equal to gold; a member who opted out, a member not
                   attributed, a search without a member or with two, another
                   provider's list (read, search, export), the Payer-to-Payer
                   operations, Consent and writes refused; an opt-out recorded
                   now applies to the next search; another client cannot read
                   its export job;
  other payer      a match group of another payer refused (read, search,
                   export); a consent naming another payer as the recipient
                   does not count; plain searches refused.

Writes data/fhir_security.json and exits with status 1 if any check fails.

Usage (from the repository root, after make fhir-publish):
    python fhir/security_check.py
"""

import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from api_check import Checks, without_meta
from provider_api_check import PAYER_SCOPE, PROVIDER_SCOPE, member_bundle
from smart_client import (AUTH_URL, KEYS_DIR, PATIENT_ACCESS_URL, BackendClient, assertion, authorize_params,
                          exchange_code, get, login, pkce, post_form, token_request)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
import lakehouse  # noqa: E402

REPORT = Path("data/fhir_security.json")
PATIENT = PATIENT_ACCESS_URL.rstrip("/")
PROVIDER = "http://localhost:8092/fhir"
FULL_PATIENT_SCOPE = ("openid fhirUser launch/patient offline_access patient/Patient.rs patient/Coverage.rs "
                      "patient/ExplanationOfBenefit.rs patient/Organization.rs")
ADMIN = BackendClient("admin-provider-access", "system/*.cruds")
NEW_PAYER = BackendClient("payer-new-payer", PAYER_SCOPE)
OTHER_PAYER = BackendClient("payer-other-payer", PAYER_SCOPE)
PROVIDER_OPT_OUT = "http://hl7.org/fhir/us/davinci-pdex/StructureDefinition/pdex-provider-consent"


def call(method: str, url: str, token: str | None = None, body: dict | None = None, headers: dict | None = None):
    """(status, headers, parsed JSON or {})."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/fhir+json", "Content-Type": "application/fhir+json", "Cache-Control": "no-cache",
        **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            status, hdrs, raw = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, dict(e.headers), e.read()
    try:
        return status, hdrs, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, hdrs, {}


def bearer(client: BackendClient) -> str:
    return client.headers()["Authorization"].removeprefix("Bearer ")


def search_ids(url: str, token: str) -> tuple[int, list[str]]:
    """Every id a search returns, following the next links (HAPI's _getpages)."""
    ids, status = [], None
    while url:
        status, _, bundle = call("GET", url, token)
        if status != 200:
            return status, sorted(ids)
        ids += [e["resource"]["id"] for e in bundle.get("entry", []) if e.get("search", {}).get("mode") != "include"]
        url = next((link["url"] for link in bundle.get("link", []) if link["relation"] == "next"), None)
    return status, sorted(ids)


class Gold:
    def __init__(self, con):
        self.con = con

    def rows(self, sql: str, params=None) -> list:
        return self.con.execute(sql, params or []).fetchall()

    def resource(self, model: str, rid: str) -> dict:
        return json.loads(self.rows(f"SELECT resource FROM {model} WHERE id = ?", [rid])[0][0])

    def ids(self, model: str, field: str, member: str) -> list[str]:
        return sorted(r for (r,) in self.rows(f"SELECT id FROM {model} WHERE resource->>'$.{field}.reference' = ?",
                                               [f"Patient/{member}"]))

    def claims(self, member: str, basis: bool = False) -> list[str]:
        model = "gold.fhir_explanation_of_benefit_basis" if basis else "gold.fhir_explanation_of_benefit"
        return sorted(self.ids(model, "patient", member) + self.ids("gold.fhir_prior_authorization", "patient", member))


def check_discovery(checks: Checks) -> None:
    for name, base, backend in (("patient access", PATIENT, False), ("provider access", PROVIDER, True)):
        status, _, config = call("GET", f"{base}/.well-known/smart-configuration")
        checks.add("discovery", f"{name}: smart-configuration token endpoint", f"{AUTH_URL}/token",
                   config.get("token_endpoint") if status == 200 else status)
        if backend:
            checks.add("discovery", f"{name}: private_key_jwt with ES384", True,
                       "private_key_jwt" in config.get("token_endpoint_auth_methods_supported", [])
                       and "ES384" in config.get("token_endpoint_auth_signing_alg_values_supported", []))
        else:
            checks.add("discovery", f"{name}: authorization endpoint and PKCE S256", [f"{AUTH_URL}/authorize", ["S256"]],
                       [config.get("authorization_endpoint"), config.get("code_challenge_methods_supported")])
        _, _, cs = call("GET", f"{base}/metadata")
        codes = [c.get("code") for r in cs.get("rest", []) for s in r.get("security", {}).get("service", [])
                 for c in s.get("coding", [])]
        checks.add("discovery", f"{name}: CapabilityStatement security SMART-on-FHIR", True, "SMART-on-FHIR" in codes)


def check_authentication(checks: Checks, member: str, patient_token: str, provider_token: str) -> None:
    for name, url in (("patient access", f"{PATIENT}/Patient/{member}"),
                      ("provider access", f"{PROVIDER}/Patient/{member}")):
        status, headers, _ = call("GET", url)
        checks.add("authentication", f"{name}: no token -> 401 with a Bearer challenge", [401, True],
                   [status, headers.get("WWW-Authenticate", "").startswith("Bearer")])
        status, _, _ = call("GET", url, "not-a-jwt")
        checks.add("authentication", f"{name}: malformed token -> 401", 401, status)
        forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        forged = jwt.encode({"iss": AUTH_URL, "aud": url.rsplit("/Patient", 1)[0], "sub": "x", "exp": now + 300,
                             "scope": "system/*.cruds", "role": "admin", "client_id": "x",
                             "api": "patient-access" if "8090" in url else "provider-access"},
                            forged_key, algorithm="RS256", headers={"kid": "forged"})
        status, _, _ = call("GET", url, forged)
        checks.add("authentication", f"{name}: token signed by another key -> 401", 401, status)
    status, _, _ = call("GET", f"{PATIENT}/Patient/{member}", provider_token)
    checks.add("authentication", "a Provider Access token on Patient Access -> 401", 401, status)
    status, _, _ = call("GET", f"{PROVIDER}/Patient/{member}", patient_token)
    checks.add("authentication", "a Patient Access token on Provider Access -> 401", 401, status)
    for port in (8091,):
        with socket.socket() as s:
            s.settimeout(3)
            reachable = s.connect_ex(("127.0.0.1", port)) == 0
        checks.add("authentication", f"no FHIR server on host port {port}", False, reachable)


def check_token_endpoint(checks: Checks, provider_id: str, other_provider_id: str) -> None:
    client = f"provider-{provider_id}"
    status, body = token_request(client, PROVIDER_SCOPE)
    checks.add("token endpoint", "valid client assertion -> 200 with the scopes asked", [200, PROVIDER_SCOPE],
               [status, body.get("scope")])
    status, body = token_request("provider-unknown", PROVIDER_SCOPE,
                                 assertion("provider-unknown", KEYS_DIR / f"{client}.pem"))
    checks.add("token endpoint", "unknown client -> 401 invalid_client", [401, "invalid_client"],
               [status, body.get("error")])
    status, body = token_request(client, PROVIDER_SCOPE,
                                 assertion(client, KEYS_DIR / f"provider-{other_provider_id}.pem"))
    checks.add("token endpoint", "assertion signed with another client's key -> 401", 401, status)
    once = assertion(client)
    first, _ = token_request(client, PROVIDER_SCOPE, once)
    second, body = token_request(client, PROVIDER_SCOPE, once)
    checks.add("token endpoint", "replayed assertion (same jti) -> 200 then 401", [200, 401], [first, second])
    status, _ = token_request(client, PROVIDER_SCOPE, assertion(client, audience="http://localhost:8093/other"))
    checks.add("token endpoint", "assertion for another audience -> 401", 401, status)
    status, _ = token_request(client, PROVIDER_SCOPE, assertion(client, lifetime=3600))
    checks.add("token endpoint", "assertion valid for an hour -> 401", 401, status)
    status, body = token_request(client, "system/Consent.rs")
    checks.add("token endpoint", "only a scope the client may not have -> 400 invalid_scope", [400, "invalid_scope"],
               [status, body.get("error")])
    status, body = token_request(client, "system/Patient.r system/Consent.rs")
    checks.add("token endpoint", "a narrower scope granted, an unregistered one left out", "system/Patient.r",
               body.get("scope"))


def check_member_login(checks: Checks, member: str, birth: str) -> dict:
    verifier, challenge = pkce()
    status, redirect = login(authorize_params(FULL_PATIENT_SCOPE, challenge, "s1"), member, "1900-01-01")
    checks.add("member login", "wrong birth date -> 401, no code", [401, False], [status, "code" in redirect])
    status, _, _ = get(f"{AUTH_URL}/authorize?" + urllib.parse.urlencode(
        authorize_params(FULL_PATIENT_SCOPE, challenge, "s2", redirect_uri="http://evil.example/cb")))
    checks.add("member login", "unregistered redirect URI -> 400 (no redirect)", 400, status)
    status, headers, _ = get(f"{AUTH_URL}/authorize?" + urllib.parse.urlencode(
        {k: v for k, v in authorize_params(FULL_PATIENT_SCOPE, challenge, "s3").items()
         if not k.startswith("code_challenge")}))
    error = urllib.parse.parse_qs(urllib.parse.urlsplit(headers.get("Location", "")).query).get("error")
    checks.add("member login", "no PKCE -> redirect with invalid_request", [302, ["invalid_request"]], [status, error])
    status, redirect = login(authorize_params(FULL_PATIENT_SCOPE, challenge, "s4"), member, birth, decision="deny")
    checks.add("member login", "member denies -> redirect with access_denied", "access_denied", redirect.get("error"))

    status, redirect = login(authorize_params(FULL_PATIENT_SCOPE, challenge, "s5"), member, birth)
    wrong, body = exchange_code(redirect.get("code", ""), "wrong-verifier-" + "x" * 40)
    checks.add("member login", "wrong PKCE verifier -> 400 invalid_grant", [400, "invalid_grant"],
               [wrong, body.get("error")])
    status, redirect = login(authorize_params(FULL_PATIENT_SCOPE, challenge, "s6"), member, birth)
    checks.add("member login", "login -> redirect with code and the same state", [302, "s6", True],
               [status, redirect.get("state"), "code" in redirect])
    status, tokens = exchange_code(redirect["code"], verifier)
    again, body = exchange_code(redirect["code"], verifier)
    checks.add("member login", "code exchanged once, refused the second time", [200, 400], [status, again])
    checks.add("member login", "token carries the patient context", member, tokens.get("patient"))
    id_token = jwt.decode(tokens.get("id_token", ""), options={"verify_signature": False}) if tokens.get("id_token") \
        else {}
    checks.add("member login", "id_token fhirUser is the member", f"{PATIENT}/Patient/{member}", id_token.get("fhirUser"))
    return tokens


def check_member_app(checks: Checks, gold: Gold, tokens: dict, member: str, birth: str, other: str) -> None:
    token = tokens["access_token"]
    status, _, patient = call("GET", f"{PATIENT}/Patient/{member}", token)
    checks.add("member app", "own Patient equal to gold", True,
               status == 200 and without_meta(patient) == without_meta(gold.resource("gold.fhir_patient", member)))
    expected = gold.claims(member)
    checks.add("member app", "own claims (patient=, pages of 10)", [200, expected],
               list(search_ids(f"{PATIENT}/ExplanationOfBenefit?patient=Patient/{member}&_count=10", token)))
    checks.add("member app", "own claims without the patient parameter", [200, expected],
               list(search_ids(f"{PATIENT}/ExplanationOfBenefit?_count=200", token)))
    checks.add("member app", "own coverage without the patient parameter",
               [200, gold.ids("gold.fhir_coverage", "beneficiary", member)],
               list(search_ids(f"{PATIENT}/Coverage", token)))
    checks.add("member app", "Patient search without parameters: only the member", [200, [member]],
               list(search_ids(f"{PATIENT}/Patient", token)))
    other_claim = gold.claims(other)[0]
    other_cov = gold.ids("gold.fhir_coverage", "beneficiary", other)[0]
    for label, method, url in [
        ("another member's claims by search", "GET", f"{PATIENT}/ExplanationOfBenefit?patient=Patient/{other}"),
        ("another member's Patient", "GET", f"{PATIENT}/Patient/{other}"),
        ("another member's claim by id", "GET", f"{PATIENT}/ExplanationOfBenefit/{other_claim}"),
        ("another member's coverage by id", "GET", f"{PATIENT}/Coverage/{other_cov}"),
        ("a chained patient parameter", "GET", f"{PATIENT}/ExplanationOfBenefit?patient.identifier=x"),
        ("an update of its own Patient", "PUT", f"{PATIENT}/Patient/{member}"),
        ("$everything", "GET", f"{PATIENT}/Patient/{member}/$everything"),
        ("a bulk $export", "GET", f"{PATIENT}/$export"),
        ("a type outside the API (Group)", "GET", f"{PATIENT}/Group"),
    ]:
        status, _, _ = call(method, url, token, patient if method == "PUT" else None)
        checks.add("member app", f"{label} -> 403", 403, status)
    org = next((r["reference"] for r in [patient.get("managingOrganization") or {}] if r.get("reference")), None)
    if org is None:
        org = "Organization/payer"
    status, _, _ = call("GET", f"{PATIENT}/{org}", token)
    checks.add("member app", "an Organization is readable", 200, status)

    narrow = None
    verifier, challenge = pkce()
    _, redirect = login(authorize_params("launch/patient patient/Patient.rs", challenge, "n1"), member, birth)
    if "code" in redirect:
        narrow = exchange_code(redirect["code"], verifier)[1].get("access_token")
    status_eob, _, _ = call("GET", f"{PATIENT}/ExplanationOfBenefit", narrow)
    status_pat, _, _ = call("GET", f"{PATIENT}/Patient/{member}", narrow)
    checks.add("member app", "token with patient/Patient.rs only: Patient yes, claims no", [200, 403],
               [status_pat, status_eob])

    refresh = tokens.get("refresh_token", "")
    status, _, body = post_form(f"{AUTH_URL}/token", {"grant_type": "refresh_token", "refresh_token": refresh,
                                                       "client_id": "patient-app"})
    renewed = json.loads(body or b"{}")
    status_read, _, _ = call("GET", f"{PATIENT}/Patient/{member}", renewed.get("access_token"))
    again, _, _ = post_form(f"{AUTH_URL}/token", {"grant_type": "refresh_token", "refresh_token": refresh,
                                                   "client_id": "patient-app"})
    checks.add("member app", "refresh token: a new access token that works, the old refresh token refused",
               [200, 200, True, 400], [status, status_read, renewed.get("refresh_token") not in (None, refresh), again])


def check_provider(checks: Checks, gold: Gold, provider_id: str, other_provider_id: str, member: str,
                   opted_out: str, unattributed: str) -> None:
    client = BackendClient(f"provider-{provider_id}", PROVIDER_SCOPE)
    token = bearer(client)
    other_token = bearer(BackendClient(f"provider-{other_provider_id}", PROVIDER_SCOPE))
    checks.add("provider", "attributed member: claims equal to gold (without financial data)",
               [200, gold.claims(member, basis=True)],
               list(search_ids(f"{PROVIDER}/ExplanationOfBenefit?patient=Patient/{member}&_count=50", token)))
    checks.add("provider", "attributed member: Patient", [200, [member]],
               list(search_ids(f"{PROVIDER}/Patient?_id={member}", token)))
    checks.add("provider", "attributed member: Coverage", [200, gold.ids("gold.fhir_coverage", "beneficiary", member)],
               list(search_ids(f"{PROVIDER}/Coverage?patient=Patient/{member}", token)))
    unattributed_claim = gold.claims(unattributed, basis=True)[0]
    match_request = {"resourceType": "Parameters", "parameter": [member_bundle(1, "A", "B", "1970-01-01", "male",
                                                                               member)]}
    for label, method, url, body in [
        ("member who opted out: claims", "GET", f"{PROVIDER}/ExplanationOfBenefit?patient=Patient/{opted_out}", None),
        ("member who opted out: Patient", "GET", f"{PROVIDER}/Patient/{opted_out}", None),
        ("member not attributed: claims", "GET", f"{PROVIDER}/ExplanationOfBenefit?patient=Patient/{unattributed}",
         None),
        ("member not attributed: claim by id", "GET", f"{PROVIDER}/ExplanationOfBenefit/{unattributed_claim}", None),
        ("search without a member", "GET", f"{PROVIDER}/ExplanationOfBenefit?_count=10", None),
        ("search naming two members", "GET",
         f"{PROVIDER}/ExplanationOfBenefit?patient=Patient/{member},Patient/{unattributed}", None),
        ("another provider's attribution list", "GET", f"{PROVIDER}/Group/attribution-{other_provider_id}", None),
        ("every attribution list", "GET", f"{PROVIDER}/Group?code=pdexprovidergroup", None),
        ("$bulk-member-match", "POST", f"{PROVIDER}/Group/$bulk-member-match", match_request),
        ("Consent", "GET", f"{PROVIDER}/Consent?patient=Patient/{member}", None),
        ("an update of its own attribution list", "PUT", f"{PROVIDER}/Group/attribution-{provider_id}",
         gold.resource("gold.fhir_provider_group", f"attribution-{provider_id}")),
    ]:
        status, _, _ = call(method, url, token, body)
        checks.add("provider", f"{label} -> 403", 403, status)
    status, _, _ = call("POST", f"{PROVIDER}/Group/attribution-{other_provider_id}/$davinci-data-export", token,
                        {"resourceType": "Parameters", "parameter": []}, {"Prefer": "respond-async"})
    checks.add("provider", "export of another provider's attribution list -> 403", 403, status)

    # An opt-out recorded now applies to the next search.
    consent = json.loads(gold.rows("SELECT resource FROM gold.fhir_provider_consent LIMIT 1")[0][0])
    consent.update({"id": "security-check-opt-out", "patient": {"reference": f"Patient/{member}"},
                    "performer": [{"reference": f"Patient/{member}"}]})
    consent["provision"]["period"] = {"start": time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))}
    call("PUT", f"{PROVIDER}/Consent/security-check-opt-out", bearer(ADMIN), consent)
    try:
        status, _, _ = call("GET", f"{PROVIDER}/ExplanationOfBenefit?patient=Patient/{member}", token)
    finally:
        call("DELETE", f"{PROVIDER}/Consent/security-check-opt-out", bearer(ADMIN))
    after, _, _ = call("GET", f"{PROVIDER}/ExplanationOfBenefit?patient=Patient/{member}", token)
    checks.add("provider", "opt-out recorded now: search refused, allowed again once it is removed", [403, 200],
               [status, after])

    # An export job belongs to the client that started it.
    status, headers, _ = call("POST", f"{PROVIDER}/Group/attribution-{provider_id}/$davinci-data-export", token,
                              {"resourceType": "Parameters", "parameter": [
                                  {"name": "patient", "valueReference": {"reference": f"Patient/{member}"}}]},
                              {"Prefer": "respond-async"})
    poll = headers.get("Content-Location", "")
    other_status, _, _ = call("GET", poll, other_token, headers={"Accept": "application/json"})
    own_status = 202
    deadline = time.time() + 300
    while own_status == 202 and time.time() < deadline:
        own_status, _, manifest = call("GET", poll, token, headers={"Accept": "application/json"})
        time.sleep(2)
    file_url = (manifest.get("output") or [{}])[0].get("url", "")
    file_other, _, _ = call("GET", file_url, other_token) if file_url else (None, None, None)
    checks.add("provider", "export job: started 202, status 404 to another provider, 200 to its own, its files "
                           "404 to another provider", [202, 404, 200, 404], [status, other_status, own_status,
                                                                              file_other])


def check_other_payer(checks: Checks, gold: Gold, member: str) -> None:
    first, last, birth, gender = gold.rows("SELECT first_name, last_name, strftime(birth_date, '%Y-%m-%d'), gender "
                                           "FROM gold.dim_member WHERE member_id = ?", [member])[0]
    request = {"resourceType": "Parameters",
               "parameter": [member_bundle(1, first, last, birth, gender, member)]}
    status, _, response = call("POST", f"{PROVIDER}/Group/$bulk-member-match", bearer(NEW_PAYER), request)
    groups = {p["name"]: p["resource"] for p in response.get("parameter", [])}
    gid = groups.get("MatchedMembers", {}).get("id")
    checks.add("other payer", "new payer matches a member (consent naming it)", [200, [f"Patient/{member}"]],
               [status, [m["entity"]["reference"] for m in groups.get("MatchedMembers", {}).get("member", [])]])
    other = bearer(OTHER_PAYER)
    new_payer = "https%3A%2F%2Fnew-payer.example%2Fpayer-id%7Cnew-payer"
    for label, method, url, body, headers in [
        ("another payer's match group", "GET", f"{PROVIDER}/Group/{gid}", None, None),
        ("a search for another payer's match groups", "GET", f"{PROVIDER}/Group?identifier={new_payer}", None, None),
        ("export of another payer's match group", "POST", f"{PROVIDER}/Group/{gid}/$davinci-data-export",
         {"resourceType": "Parameters", "parameter": []}, {"Prefer": "respond-async"}),
        ("a plain Patient search", "GET", f"{PROVIDER}/Patient?_id={member}", None, None),
        ("a provider's attribution list", "GET", f"{PROVIDER}/Group?code=pdexprovidergroup", None, None),
    ]:
        status, _, _ = call(method, url, other, body, headers)
        checks.add("other payer", f"{label} -> 403", 403, status)
    # The other payer sends the same request: its consent names the new payer as the recipient.
    status, _, response = call("POST", f"{PROVIDER}/Group/$bulk-member-match", other, request)
    groups = {p["name"]: p["resource"] for p in response.get("parameter", [])}
    checks.add("other payer", "a consent naming another recipient: consent-constrained, not matched",
               [200, 0, ["1"]], [status, len(groups.get("MatchedMembers", {}).get("member", [])),
                                 [c["id"] for c in groups.get("ConsentConstrainedMembers", {}).get("contained", [])]])


def main() -> None:
    con = lakehouse.connect(read_only=True)
    checks = Checks()
    try:
        gold = Gold(con)
        # A provider with a member who opted out; a member it may see, with claims.
        provider_id, opted_out = gold.rows(
            "SELECT provider_id, member_id FROM gold.provider_attribution WHERE is_opted_out "
            "ORDER BY md5('42:' || provider_id || member_id) LIMIT 1")[0]
        member = gold.rows(
            "SELECT a.member_id FROM gold.provider_attribution a WHERE a.provider_id = ? AND NOT a.is_opted_out "
            "AND EXISTS (SELECT 1 FROM gold.fhir_explanation_of_benefit e "
            "            WHERE e.resource->>'$.patient.reference' = 'Patient/' || a.member_id) "
            "ORDER BY md5('42:' || a.member_id) LIMIT 1", [provider_id])[0][0]
        unattributed = gold.rows(
            "SELECT member_id FROM gold.dim_member m WHERE NOT EXISTS (SELECT 1 FROM gold.provider_attribution a "
            "WHERE a.provider_id = ? AND a.member_id = m.member_id) AND EXISTS (SELECT 1 FROM "
            "gold.fhir_explanation_of_benefit_basis e WHERE e.resource->>'$.patient.reference' = "
            "'Patient/' || m.member_id) ORDER BY md5('42:' || member_id) LIMIT 1", [provider_id])[0][0]
        other_provider_id = gold.rows("SELECT provider_id FROM gold.fhir_provider_group WHERE provider_id <> ? "
                                      "ORDER BY md5('42:' || provider_id) LIMIT 1", [provider_id])[0][0]
        birth = gold.resource("gold.fhir_patient", member)["birthDate"]

        check_discovery(checks)
        tokens = check_member_login(checks, member, birth)
        check_authentication(checks, member, tokens["access_token"],
                             bearer(BackendClient(f"provider-{provider_id}", PROVIDER_SCOPE)))
        check_token_endpoint(checks, provider_id, other_provider_id)
        check_member_app(checks, gold, tokens, member, birth, unattributed)
        check_provider(checks, gold, provider_id, other_provider_id, member, opted_out, unattributed)
        check_other_payer(checks, gold, member)
    finally:
        con.close()

    by_group = defaultdict(lambda: [0, 0])
    for r in checks.results:
        by_group[r["group"]][0] += r["passed"]
        by_group[r["group"]][1] += 1
    report = {"auth": AUTH_URL, "patient_access": PATIENT, "provider_access": PROVIDER,
              "subjects": {"member": member, "provider": provider_id, "member_opted_out": opted_out,
                           "member_not_attributed": unattributed, "other_provider": other_provider_id},
              "passed": sum(r["passed"] for r in checks.results), "total": len(checks.results),
              "groups": {g: {"passed": p, "total": t} for g, (p, t) in by_group.items()},
              "failed": checks.failed, "checks": checks.results}
    REPORT.write_text(json.dumps(report, indent=2))
    for g, (p, t) in by_group.items():
        print(f"{g:20} {p:>4}/{t}")
    print(f"{report['passed']}/{report['total']} checks passed. Written to {REPORT}")
    for r in checks.failed[:30]:
        print(f"  FAILED {r['group']} {r['check']}: expected {str(r['expected'])[:150]}, got {str(r['actual'])[:150]}")
    if checks.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
