"""SMART on FHIR for the gateway: access token verification and scopes.

An access token is a JWT the authorization server (fhir/auth/server.py) signs
with RS256; the gateway verifies it with the server's public keys (its /jwks,
fetched again when a token names an unknown key), and requires the issuer, an
expiry in the future and, as audience, this gateway's own FHIR base, so a
token issued for one API is refused by the other.

Scopes are SMART v2 (`<context>/<type>.<permissions>`, permissions among c, r,
u, d, s), with the SMART v1 forms read, write and * understood as rs, cud and
cruds. Scopes with a query (granular scopes) are ignored: the gateway does not
apply their filter, so it does not grant them.
"""

import json
import re
import threading
import time
import urllib.request

import jwt

SCOPE_RE = re.compile(r"^(patient|user|system)/(\*|[A-Z][A-Za-z]+)\.(\*|read|write|c?r?u?d?s?)$")
V1_PERMISSIONS = {"read": "rs", "write": "cud", "*": "cruds"}
SMART_SECURITY_SYSTEM = "http://terminology.hl7.org/CodeSystem/restful-security-service"
OAUTH_URIS = "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris"


class Unauthorized(Exception):
    """No valid access token: 401 with a WWW-Authenticate challenge."""


class Token:
    def __init__(self, claims: dict):
        self.claims = claims
        self.client_id = claims.get("client_id")
        self.role = claims.get("role")              # backend clients: provider, payer or admin
        self.patient = claims.get("patient")        # Patient Access: the patient context
        self.fhir_user = claims.get("fhirUser")
        self.payer_identifier = claims.get("payer_identifier")
        self.scopes: dict[tuple[str, str], set[str]] = {}
        for scope in claims.get("scope", "").split():
            m = SCOPE_RE.match(scope)
            if m and m.group(3):
                context, rtype, perms = m.groups()
                self.scopes.setdefault((context, rtype), set()).update(V1_PERMISSIONS.get(perms, perms))

    def can(self, context: str, rtype: str, permission: str) -> bool:
        return any(permission in self.scopes.get((context, t), ()) for t in (rtype, "*"))


class Verifier:
    def __init__(self, jwks_url: str, issuer: str, audience: str):
        self.jwks_url, self.issuer, self.audience = jwks_url, issuer.rstrip("/"), audience.rstrip("/")
        self.keys: dict[str, object] = {}
        self.fetched = 0.0
        self.lock = threading.Lock()

    def key(self, kid: str | None):
        with self.lock:
            if kid not in self.keys and time.time() - self.fetched > 5:
                self.fetched = time.time()
                try:
                    with urllib.request.urlopen(self.jwks_url, timeout=10) as resp:
                        jwks = json.loads(resp.read())
                    self.keys = {k.get("kid"): jwt.PyJWK(k).key for k in jwks.get("keys", [])}
                except OSError as e:
                    raise Unauthorized(f"The authorization server's keys are not available: {e}") from e
            if kid not in self.keys:
                raise Unauthorized("The token is signed with an unknown key")
            return self.keys[kid]

    def verify(self, authorization: str | None) -> Token:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthorized("An access token is required (Authorization: Bearer)")
        raw = authorization[len("Bearer "):].strip()
        try:
            header = jwt.get_unverified_header(raw)
            claims = jwt.decode(raw, self.key(header.get("kid")), algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iss", "aud", "scope"]})
        except jwt.PyJWTError as e:
            raise Unauthorized(f"The access token is not valid: {e}") from e
        return Token(claims)


def smart_configuration(auth_public: str, backend: bool) -> dict:
    """[base]/.well-known/smart-configuration (SMART App Launch 2.2)."""
    auth_public = auth_public.rstrip("/")
    config = {
        "issuer": auth_public,
        "jwks_uri": f"{auth_public}/jwks",
        "token_endpoint": f"{auth_public}/token",
        "grant_types_supported": ["client_credentials"],
        "token_endpoint_auth_methods_supported": ["private_key_jwt"],
        "token_endpoint_auth_signing_alg_values_supported": ["RS384", "ES384"],
        "scopes_supported": ["system/*.rs"],
        "capabilities": ["client-confidential-asymmetric", "permission-v2", "permission-v1"],
    }
    if not backend:
        config.update({
            "authorization_endpoint": f"{auth_public}/authorize",
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["openid", "fhirUser", "launch/patient", "offline_access", "online_access",
                                 "patient/*.rs"],
            "capabilities": ["launch-standalone", "client-public", "context-standalone-patient", "permission-patient",
                             "permission-offline", "permission-online", "permission-v2", "permission-v1",
                             "sso-openid-connect"],
        })
    return config


def security(auth_public: str, backend: bool) -> dict:
    """CapabilityStatement.rest.security: the SMART service and the OAuth endpoints."""
    auth_public = auth_public.rstrip("/")
    uris = [{"url": "token", "valueUri": f"{auth_public}/token"}]
    if not backend:
        uris.append({"url": "authorize", "valueUri": f"{auth_public}/authorize"})
    return {"extension": [{"url": OAUTH_URIS, "extension": uris}],
            "service": [{"coding": [{"system": SMART_SECURITY_SYSTEM, "code": "SMART-on-FHIR"}]}],
            "description": "SMART on FHIR: " + ("Backend Services (client credentials, private_key_jwt)" if backend
                                                else "App Launch (authorization code with PKCE), patient scopes")}
