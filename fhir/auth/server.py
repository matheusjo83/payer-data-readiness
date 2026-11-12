"""SMART authorization server for the FHIR APIs (service fhir-auth, port 8093).

A minimal authorization server for the two SMART flows the CMS APIs use. It is
a demonstration, not a production identity provider: clients are registered in
a file, signing keys live in memory, and the member login is simulated.

  Patient Access (SMART App Launch 2.2, standalone launch)
      GET  /authorize   authorization code request with PKCE (S256 required),
                        `aud` the Patient Access FHIR base. Shows a login form.
      POST /authorize   the member logs in with their member ID and birth date
                        (a stand-in for the payer's member portal), checked
                        against the Patient Access FHIR server; a redirect to the
                        app with a one-time code (60 s).
      POST /token       grant_type=authorization_code: the code and the PKCE
                        verifier for an access token with the patient context
                        (`patient`), an `id_token` with `fhirUser` when openid
                        and fhirUser were asked for, and a refresh token with
                        offline_access or online_access;
                        grant_type=refresh_token: a new access token (the refresh
                        token is rotated).

  Provider Access and Payer-to-Payer (SMART Backend Services)
      POST /token       grant_type=client_credentials with a signed JWT
                        (client_assertion, RS384 or ES384) checked against the
                        public key registered for the client: iss = sub = the
                        client, aud = this token endpoint, exp at most 5 minutes
                        ahead, a jti never seen before.

  GET  /jwks            the public key that signs the access tokens (RS256); the
                        FHIR gateways verify tokens with it.

Scopes are SMART v2 (patient/ExplanationOfBenefit.rs, system/Group.rs, ...); a
client gets the scopes it asked for that its registration allows, and a request
with none of them is refused (invalid_scope). The access token is a JWT for
the FHIR base of one API (`aud`): its claims carry the scopes, the client, the
patient context, and for a backend client its role (provider or payer), the
provider it acts for (`fhirUser`, Organization/<id>) or the payer's identifier.

Clients come from a JSON file (`--clients`, written by fhir/auth/clients.py),
read again whenever it changes. Each client has: client_id, api
(patient-access or provider-access), type (public: the Patient Access app,
with redirect_uris; backend: with jwks, its public keys), scopes (the ones it
may get), and for a backend client role, fhirUser or payer_identifier.

Usage:
    python fhir/auth/server.py [--port 8093] [--clients data/fhir_auth/clients.json]
        [--public-url http://localhost:8093] [--patient-fhir http://localhost:8090/fhir]
"""

import argparse
import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ACCESS_TOKEN_SECONDS = 300
CODE_SECONDS = 60
REFRESH_TOKEN_SECONDS = 24 * 3600
ASSERTION_MAX_SECONDS = 300
ASSERTION_ALGORITHMS = ["RS384", "ES384"]
JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
MEMBER_ID_SYSTEM = "https://payer-data-readiness.example/member-id"


class OAuthError(Exception):
    """An OAuth error response (RFC 6749 section 5.2)."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


RESOURCE_SCOPE = re.compile(r"^(patient|user|system)/(\*|[A-Z][A-Za-z]+)\.(\*|read|write|c?r?u?d?s?)$")
V1_PERMISSIONS = {"read": "rs", "write": "cud", "*": "cruds"}


def within(requested: str, allowed: set[str]) -> bool:
    """Whether a requested scope is one the client may get: listed, or a resource scope
    narrower than a listed one (same context, the same type or *, fewer permissions)."""
    if requested in allowed:
        return True
    m = RESOURCE_SCOPE.match(requested)
    if not m or not m.group(3):
        return False
    context, rtype, perms = m.groups()
    perms = set(V1_PERMISSIONS.get(perms, perms))
    for scope in allowed:
        a = RESOURCE_SCOPE.match(scope)
        if a and a.group(1) == context and a.group(2) in (rtype, "*") \
                and perms <= set(V1_PERMISSIONS.get(a.group(3), a.group(3))):
            return True
    return False


def b64url_sha256(text: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()


class Clients:
    """The registered clients, read again when the file changes."""

    def __init__(self, path: Path):
        self.path, self.mtime, self.by_id = path, None, {}
        self.lock = threading.Lock()

    def get(self, client_id: str | None) -> dict | None:
        with self.lock:
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                return None
            if mtime != self.mtime:
                self.by_id = {c["client_id"]: c for c in json.loads(self.path.read_text())["clients"]}
                self.mtime = mtime
            return self.by_id.get(client_id or "")


class Authority:
    def __init__(self, public_url: str, clients: Clients, audiences: dict, patient_fhir: str):
        self.public_url = public_url.rstrip("/")
        self.token_url = f"{self.public_url}/token"
        self.clients, self.audiences, self.patient_fhir = clients, audiences, patient_fhir.rstrip("/")
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = uuid.uuid4().hex[:16]
        self.codes, self.refresh_tokens, self.pending, self.seen_jti = {}, {}, {}, {}
        self.lock = threading.Lock()

    # --- keys and tokens ---------------------------------------------------------

    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]}

    def sign(self, claims: dict) -> str:
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def granted(self, client: dict, requested: str) -> list[str]:
        allowed = set(client.get("scopes", []))
        scopes = [s for s in requested.split() if within(s, allowed)]
        if not scopes:
            raise OAuthError("invalid_scope", f"None of the requested scopes is allowed for {client['client_id']}")
        return scopes

    def access_token(self, client: dict, scopes: list[str], sub: str, extra: dict) -> dict:
        now = int(time.time())
        claims = {"iss": self.public_url, "aud": self.audiences[client["api"]], "sub": sub,
                  "client_id": client["client_id"], "scope": " ".join(scopes), "api": client["api"],
                  "iat": now, "exp": now + ACCESS_TOKEN_SECONDS, "jti": uuid.uuid4().hex, **extra}
        return {"access_token": self.sign(claims), "token_type": "Bearer", "expires_in": ACCESS_TOKEN_SECONDS,
                "scope": " ".join(scopes)}

    # --- Patient Access: authorization code with PKCE ------------------------------

    def check_authorize(self, q: dict) -> dict:
        """The authorization request, checked; errors that cannot go back to the app raise OAuthError."""
        client = self.clients.get(q.get("client_id"))
        if not client or client.get("type") != "public":
            raise OAuthError("unauthorized_client", "Unknown client_id")
        if q.get("redirect_uri") not in client.get("redirect_uris", []):
            raise OAuthError("invalid_request", "redirect_uri is not registered for this client")
        problems = []
        if q.get("response_type") != "code":
            problems.append(("unsupported_response_type", "response_type must be code"))
        if q.get("aud", "").rstrip("/") != self.audiences["patient-access"]:
            problems.append(("invalid_request", f"aud must be {self.audiences['patient-access']}"))
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            problems.append(("invalid_request", "PKCE with code_challenge_method=S256 is required"))
        if not q.get("state"):
            problems.append(("invalid_request", "state is required"))
        if problems:
            raise RedirectError(q["redirect_uri"], q.get("state"), *problems[0])
        try:
            scopes = self.granted(client, q.get("scope", ""))
        except OAuthError as e:
            raise RedirectError(q["redirect_uri"], q.get("state"), e.error, e.description) from e
        return {"client": client, "scopes": scopes, "redirect_uri": q["redirect_uri"], "state": q["state"],
                "code_challenge": q["code_challenge"]}

    def find_member(self, member_id: str, birth_date: str) -> str | None:
        """The Patient id of the member with this member ID and birth date, from the Patient Access server."""
        query = urllib.parse.urlencode({"identifier": f"{MEMBER_ID_SYSTEM}|{member_id}"})
        req = urllib.request.Request(f"{self.patient_fhir}/Patient?{query}", headers={"Accept": "application/fhir+json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                bundle = json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError):
            return None
        found = [e["resource"] for e in bundle.get("entry", []) if e["resource"].get("birthDate") == birth_date]
        return found[0]["id"] if len(found) == 1 else None

    def issue_code(self, request: dict, patient: str) -> str:
        code = secrets.token_urlsafe(32)
        with self.lock:
            self.codes[code] = {**request, "patient": patient, "expires": time.time() + CODE_SECONDS}
        return code

    def token_from_code(self, form: dict) -> dict:
        with self.lock:
            grant = self.codes.pop(form.get("code", ""), None)
        if not grant or grant["expires"] < time.time():
            raise OAuthError("invalid_grant", "The code is unknown, used or expired")
        client = grant["client"]
        if form.get("client_id") != client["client_id"] or form.get("redirect_uri") != grant["redirect_uri"]:
            raise OAuthError("invalid_grant", "client_id or redirect_uri differ from the authorization request")
        if b64url_sha256(form.get("code_verifier", "")) != grant["code_challenge"]:
            raise OAuthError("invalid_grant", "The PKCE code_verifier does not match the code_challenge")
        return self.patient_tokens(client, grant["scopes"], grant["patient"])

    def patient_tokens(self, client: dict, scopes: list[str], patient: str) -> dict:
        extra = {"patient": patient} if "launch/patient" in scopes else {}
        if "fhirUser" in scopes:
            extra["fhirUser"] = f"{self.audiences['patient-access']}/Patient/{patient}"
        response = {**self.access_token(client, scopes, f"Patient/{patient}", extra), **({"patient": patient}
                                                                                          if "patient" in extra else {})}
        if "openid" in scopes and "fhirUser" in scopes:
            now = int(time.time())
            response["id_token"] = self.sign({"iss": self.public_url, "aud": client["client_id"],
                                              "sub": f"Patient/{patient}", "fhirUser": extra["fhirUser"],
                                              "iat": now, "exp": now + ACCESS_TOKEN_SECONDS})
        if "offline_access" in scopes or "online_access" in scopes:
            refresh = secrets.token_urlsafe(32)
            with self.lock:
                self.refresh_tokens[refresh] = {"client_id": client["client_id"], "scopes": scopes,
                                                "patient": patient, "expires": time.time() + REFRESH_TOKEN_SECONDS}
            response["refresh_token"] = refresh
        return response

    def token_from_refresh(self, form: dict) -> dict:
        with self.lock:
            grant = self.refresh_tokens.pop(form.get("refresh_token", ""), None)
        if not grant or grant["expires"] < time.time() or grant["client_id"] != form.get("client_id"):
            raise OAuthError("invalid_grant", "The refresh token is unknown, used, expired or not this client's")
        client = self.clients.get(grant["client_id"])
        scopes = grant["scopes"]
        if form.get("scope"):
            narrower = [s for s in form["scope"].split() if s in scopes]
            if not narrower:
                raise OAuthError("invalid_scope", "A refreshed token cannot have scopes the original grant lacks")
            scopes = narrower
        return self.patient_tokens(client, scopes, grant["patient"])

    # --- Backend Services: client credentials with a signed JWT ---------------------

    def token_from_assertion(self, form: dict) -> dict:
        if form.get("client_assertion_type") != JWT_BEARER or not form.get("client_assertion"):
            raise OAuthError("invalid_client", f"client_assertion_type must be {JWT_BEARER}", 401)
        assertion = form["client_assertion"]
        try:
            unverified = jwt.decode(assertion, options={"verify_signature": False})
            header = jwt.get_unverified_header(assertion)
        except jwt.PyJWTError as e:
            raise OAuthError("invalid_client", f"The client assertion is not a JWT: {e}", 401) from e
        client = self.clients.get(unverified.get("iss"))
        if not client or client.get("type") != "backend":
            raise OAuthError("invalid_client", "Unknown client", 401)
        if header.get("alg") not in ASSERTION_ALGORITHMS:
            raise OAuthError("invalid_client", f"The assertion must be signed with {ASSERTION_ALGORITHMS}", 401)
        keys = [k for k in client["jwks"]["keys"] if not header.get("kid") or k.get("kid") == header["kid"]]
        claims = None
        for key in keys:
            try:
                claims = jwt.decode(assertion, jwt.PyJWK(key).key, algorithms=[header["alg"]], audience=self.token_url,
                                    options={"require": ["iss", "sub", "aud", "exp", "jti"]})
                break
            except jwt.InvalidSignatureError:
                continue
            except jwt.PyJWTError as e:
                raise OAuthError("invalid_client", f"The client assertion is not valid: {e}", 401) from e
        if claims is None:
            raise OAuthError("invalid_client", "The client assertion signature does not verify", 401)
        if claims["sub"] != claims["iss"]:
            raise OAuthError("invalid_client", "iss and sub of the assertion must both be the client_id", 401)
        if claims["exp"] > time.time() + ASSERTION_MAX_SECONDS:
            raise OAuthError("invalid_client", "The assertion expires more than 5 minutes ahead", 401)
        with self.lock:
            now = time.time()
            self.seen_jti = {k: v for k, v in self.seen_jti.items() if v > now}
            key = f"{claims['iss']}|{claims['jti']}"
            if key in self.seen_jti:
                raise OAuthError("invalid_client", "The assertion jti was already used", 401)
            self.seen_jti[key] = claims["exp"]
        scopes = self.granted(client, form.get("scope", ""))
        extra = {"role": client["role"]}
        if client.get("fhirUser"):
            extra["fhirUser"] = client["fhirUser"]
        if client.get("payer_identifier"):
            extra["payer_identifier"] = client["payer_identifier"]
        return self.access_token(client, scopes, client["client_id"], extra)


class RedirectError(Exception):
    """An authorization error sent back to the app's redirect URI."""

    def __init__(self, redirect_uri: str, state: str | None, error: str, description: str):
        super().__init__(description)
        self.location = redirect_uri + "?" + urllib.parse.urlencode(
            {"error": error, "error_description": description, **({"state": state} if state else {})})


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Member login</title></head>
<body><h1>Sign in to share your health plan data</h1>
<p>{client} asks for: {scopes}</p>{error}
<form method="post" action="/authorize">{hidden}
<label>Member ID <input name="member_id" autocomplete="off"></label>
<label>Birth date <input name="birth_date" placeholder="YYYY-MM-DD"></label>
<button name="decision" value="allow">Sign in and allow</button>
<button name="decision" value="deny">Deny</button>
</form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "payer-fhir-auth"
    authority: Authority = None

    def send(self, status: int, body: dict | str, headers: dict | None = None) -> None:
        data = (json.dumps(body) if isinstance(body, dict) else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if isinstance(body, dict) else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}

    def login_page(self, request: dict, params: dict, error: str = "", status: int = 200) -> None:
        hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                         for k, v in params.items() if k in ("client_id", "redirect_uri", "response_type", "scope",
                                                             "state", "aud", "code_challenge",
                                                             "code_challenge_method"))
        self.send(status, LOGIN_PAGE.format(client=html.escape(request["client"].get("name", request["client"]
                                                                                    ["client_id"])),
                                            scopes=html.escape(" ".join(request["scopes"])),
                                            error=f"<p role=alert>{html.escape(error)}</p>" if error else "",
                                            hidden=hidden))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        try:
            if parsed.path == "/jwks":
                return self.send(200, self.authority.jwks())
            if parsed.path == "/authorize":
                params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                return self.login_page(self.authority.check_authorize(params), params)
            self.send(404, {"error": "not_found"})
        except RedirectError as e:
            self.send(302, "", {"Location": e.location})
        except OAuthError as e:
            self.send(e.status, {"error": e.error, "error_description": e.description})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        form = self.form()
        try:
            if path == "/authorize":
                request = self.authority.check_authorize(form)
                if form.get("decision") != "allow":
                    raise RedirectError(request["redirect_uri"], request["state"], "access_denied",
                                        "The member did not allow access")
                patient = self.authority.find_member(form.get("member_id", ""), form.get("birth_date", ""))
                if patient is None:
                    return self.login_page(request, form, "Member ID and birth date do not match a member", 401)
                code = self.authority.issue_code(request, patient)
                location = request["redirect_uri"] + "?" + urllib.parse.urlencode(
                    {"code": code, "state": request["state"]})
                return self.send(302, "", {"Location": location})
            if path == "/token":
                grant = form.get("grant_type")
                if grant == "authorization_code":
                    return self.send(200, self.authority.token_from_code(form))
                if grant == "refresh_token":
                    return self.send(200, self.authority.token_from_refresh(form))
                if grant == "client_credentials":
                    return self.send(200, self.authority.token_from_assertion(form))
                raise OAuthError("unsupported_grant_type", f"grant_type {grant} is not supported")
            self.send(404, {"error": "not_found"})
        except RedirectError as e:
            self.send(302, "", {"Location": e.location})
        except OAuthError as e:
            self.send(e.status, {"error": e.error, "error_description": e.description})

    def log_message(self, fmt, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--clients", default=os.environ.get("AUTH_CLIENTS", "data/fhir_auth/clients.json"))
    ap.add_argument("--public-url", default=os.environ.get("AUTH_PUBLIC_URL", "http://localhost:8093"))
    ap.add_argument("--patient-access-aud", default=os.environ.get("PATIENT_ACCESS_URL", "http://localhost:8090/fhir"))
    ap.add_argument("--provider-access-aud", default=os.environ.get("PROVIDER_ACCESS_URL",
                                                                   "http://localhost:8092/fhir"))
    ap.add_argument("--patient-fhir", default=os.environ.get("PATIENT_FHIR_UPSTREAM", "http://localhost:8090/fhir"),
                    help="Patient Access FHIR server the member login is checked against")
    args = ap.parse_args()
    Handler.authority = Authority(args.public_url, Clients(Path(args.clients)),
                                  {"patient-access": args.patient_access_aud.rstrip("/"),
                                   "provider-access": args.provider_access_aud.rstrip("/")}, args.patient_fhir)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"SMART authorization server on port {args.port}, issuer {args.public_url}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
