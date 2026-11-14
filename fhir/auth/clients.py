"""Register the SMART clients of the FHIR APIs (make fhir-clients).

Writes data/fhir_auth/clients.json, which the authorization server (fhir-auth)
reads, and a private key for each backend client in data/fhir_auth/keys/
(ES384, PEM), which the clients of this repository sign their assertions with.
Keys that exist are kept, so running it again only adds what is missing (the
providers come from gold, so it runs after dbt).

  patient-app                public client of the Patient Access API (SMART App
                             Launch, authorization code with PKCE): patient/
                             scopes on Patient, Coverage, ExplanationOfBenefit and
                             Organization, openid fhirUser, offline_access.
  provider-<provider id>     one backend client per provider with an attribution
                             list (Provider Access), acting for Organization/<id>:
                             read and search of Patient, Coverage,
                             ExplanationOfBenefit, Group and Organization; and prior
                             authorization (DTR questionnaires, PAS $submit and
                             $inquire, PAS subscriptions), with the one endpoint
                             its subscriptions may notify (notification_endpoints).
  payer-new-payer,           backend clients of other payers (Payer-to-Payer), each
  payer-other-payer          with its payer identifier: member match (system/Group.c,
                             system/Patient.s), the export of its match groups, and
                             their deletion when a member's consent is revoked
                             (system/Group.d).
  admin-patient-access,      the payer's own administration client of each API
  admin-provider-access      (system/*.cruds): publishing (make fhir-publish) and the
                             checks' counts and fault injection.

Usage (from the repository root, after make dbt):
    python fhir/auth/clients.py
"""

import json
import os
import sys
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ingestion"))
import lakehouse  # noqa: E402

AUTH_DIR = Path("data/fhir_auth")
KEYS_DIR = AUTH_DIR / "keys"
PATIENT_APP_REDIRECT = "http://localhost:8099/callback"
PAYERS = {
    "payer-new-payer": {"system": "https://new-payer.example/payer-id", "value": "new-payer"},
    "payer-other-payer": {"system": "https://other-payer.example/payer-id", "value": "other-payer"},
}
PATIENT_SCOPES = ["openid", "fhirUser", "launch/patient", "offline_access", "online_access",
                  "patient/Patient.rs", "patient/Coverage.rs", "patient/ExplanationOfBenefit.rs",
                  "patient/Organization.rs"]
PROVIDER_SCOPES = ["system/Patient.rs", "system/Coverage.rs", "system/ExplanationOfBenefit.rs", "system/Group.rs",
                   "system/Organization.rs",
                   # Prior authorization: DTR $questionnaire-package, PAS $submit (c) and $inquire (r),
                   # PAS subscriptions.
                   "system/Questionnaire.r", "system/Claim.cr", "system/Subscription.crd"]
PAYER_SCOPES = ["system/Group.crds", "system/Patient.rs", "system/Coverage.r", "system/ExplanationOfBenefit.r",
                "system/Organization.r"]
ADMIN_SCOPES = ["system/*.cruds"]
# Each provider's endpoint for PAS notifications: its EHR's rest-hook, here the
# simulated EHR of make fhir-prior-auth-check, on the host (one path per provider).
ENDPOINTS_DIR = AUTH_DIR / "endpoints"
NOTIFICATION_BASE = os.environ.get("PROVIDER_NOTIFICATION_BASE", "http://host.docker.internal:8094/notifications")


def public_jwk(client_id: str) -> dict:
    """The client's public key as a JWK; the private key is created on first use."""
    path = KEYS_DIR / f"{client_id}.pem"
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    else:
        key = ec.generate_private_key(ec.SECP384R1())
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        path.chmod(0o600)
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": client_id, "alg": "ES384", "use": "sig"}


def backend(client_id: str, api: str, role: str, scopes: list[str], **extra) -> dict:
    return {"client_id": client_id, "type": "backend", "api": api, "role": role, "scopes": scopes,
            "jwks": {"keys": [public_jwk(client_id)]}, **extra}


def main() -> None:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    con = lakehouse.connect(read_only=True)
    try:
        providers = [p for (p,) in con.execute("SELECT provider_id FROM gold.fhir_provider_group ORDER BY 1").fetchall()]
    finally:
        con.close()
    clients = [
        {"client_id": "patient-app", "name": "Member health app", "type": "public", "api": "patient-access",
         "redirect_uris": [PATIENT_APP_REDIRECT], "scopes": PATIENT_SCOPES},
        backend("admin-patient-access", "patient-access", "admin", ADMIN_SCOPES),
        backend("admin-provider-access", "provider-access", "admin", ADMIN_SCOPES),
        *(backend(cid, "provider-access", "payer", PAYER_SCOPES, payer_identifier=ident)
          for cid, ident in PAYERS.items()),
        *(backend(f"provider-{p}", "provider-access", "provider", PROVIDER_SCOPES, fhirUser=f"Organization/{p}",
                  notification_endpoints=[f"{NOTIFICATION_BASE}/{p}"])
          for p in providers),
    ]
    out = AUTH_DIR / "clients.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps({"clients": clients}, indent=1))
    tmp.replace(out)
    # The registered notification endpoints, apart from the keys: the gateway reads
    # only this directory.
    endpoints = {c["client_id"]: c["notification_endpoints"] for c in clients if c.get("notification_endpoints")}
    ENDPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    out_e = ENDPOINTS_DIR / "notification_endpoints.json"
    tmp = out_e.with_suffix(".tmp")
    tmp.write_text(json.dumps(endpoints, indent=1))
    tmp.replace(out_e)
    print(f"{len(clients)} clients ({len(providers)} providers) written to {out}; keys in {KEYS_DIR}; "
          f"notification endpoints in {out_e}")


if __name__ == "__main__":
    main()
