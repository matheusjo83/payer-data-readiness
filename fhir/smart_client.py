"""SMART clients for the scripts of this repository (publish, API checks).

  BackendClient   SMART Backend Services: a client_credentials request with a
                  JWT assertion signed (ES384) by the client's key in
                  data/fhir_auth/keys/ (make fhir-clients). The access token is
                  cached until shortly before it expires.
  patient_login   SMART App Launch for the Patient Access app (patient-app): the
                  authorization request with PKCE, the member's login form, the
                  redirect with the code (not followed: there is no app behind
                  the redirect URI) and the token request.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import jwt

AUTH_URL = os.environ.get("FHIR_AUTH_URL", "http://localhost:8093")
KEYS_DIR = Path("data/fhir_auth/keys")
PATIENT_APP = "patient-app"
PATIENT_APP_REDIRECT = "http://localhost:8099/callback"
PATIENT_ACCESS_URL = os.environ.get("FHIR_BASE_URL", "http://localhost:8090/fhir")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def post_form(url: str, form: dict) -> tuple[int, dict, bytes]:
    """(status, headers, body) of a form POST; redirects are returned, not followed."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _opener.open(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get(url: str) -> tuple[int, dict, bytes]:
    try:
        with _opener.open(urllib.request.Request(url), timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def assertion(client_id: str, key_path: Path | None = None, audience: str | None = None, lifetime: int = 240,
              jti: str | None = None) -> str:
    """A client assertion JWT (SMART Backend Services), signed with the client's key."""
    key = (key_path or KEYS_DIR / f"{client_id}.pem").read_bytes()
    now = int(time.time())
    claims = {"iss": client_id, "sub": client_id, "aud": audience or f"{AUTH_URL}/token", "iat": now,
              "exp": now + lifetime, "jti": jti or uuid.uuid4().hex}
    return jwt.encode(claims, key, algorithm="ES384", headers={"kid": client_id})


def token_request(client_id: str, scope: str, client_assertion: str | None = None) -> tuple[int, dict]:
    status, _, body = post_form(f"{AUTH_URL}/token", {
        "grant_type": "client_credentials", "scope": scope,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": client_assertion or assertion(client_id)})
    return status, json.loads(body or b"{}")


class BackendClient:
    def __init__(self, client_id: str, scope: str):
        self.client_id, self.scope = client_id, scope
        self.token, self.expires = None, 0.0
        self.lock = threading.Lock()

    def headers(self) -> dict:
        with self.lock:
            if self.token is None or time.time() > self.expires - 30:
                status, body = token_request(self.client_id, self.scope)
                if status != 200:
                    raise SystemExit(f"Token request for {self.client_id} failed: HTTP {status} {body}")
                self.token, self.expires = body["access_token"], time.time() + body["expires_in"]
            return {"Authorization": f"Bearer {self.token}"}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_params(scope: str, challenge: str, state: str, **overrides) -> dict:
    return {"response_type": "code", "client_id": PATIENT_APP, "redirect_uri": PATIENT_APP_REDIRECT, "scope": scope,
            "state": state, "aud": PATIENT_ACCESS_URL, "code_challenge": challenge, "code_challenge_method": "S256",
            **overrides}


def login(params: dict, member_id: str, birth_date: str, decision: str = "allow") -> tuple[int, dict]:
    """Show the login form, then submit it; (status, query of the redirect or {})."""
    status, _, _ = get(f"{AUTH_URL}/authorize?{urllib.parse.urlencode(params)}")
    if status != 200:
        return status, {}
    status, headers, _ = post_form(f"{AUTH_URL}/authorize", {**params, "member_id": member_id,
                                                              "birth_date": birth_date, "decision": decision})
    location = headers.get("Location", "")
    return status, {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).items()}


def exchange_code(code: str, verifier: str) -> tuple[int, dict]:
    status, _, body = post_form(f"{AUTH_URL}/token", {"grant_type": "authorization_code", "code": code,
                                                       "client_id": PATIENT_APP, "redirect_uri": PATIENT_APP_REDIRECT,
                                                       "code_verifier": verifier})
    return status, json.loads(body or b"{}")


def patient_login(member_id: str, birth_date: str, scope: str) -> dict:
    """The token response of a member's login through the Patient Access app."""
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    status, redirect = login(authorize_params(scope, challenge, state), member_id, birth_date)
    if status != 302 or redirect.get("state") != state or "code" not in redirect:
        raise SystemExit(f"Login of {member_id} failed: HTTP {status} {redirect}")
    status, body = exchange_code(redirect["code"], verifier)
    if status != 200:
        raise SystemExit(f"Code exchange for {member_id} failed: HTTP {status} {body}")
    return body
