"""Publish the gold FHIR resources to the FHIR servers (make fhir-publish).

Two servers, each with the data of its APIs:

  patient-access    (fhir-server, port 8090) the Patient Access API: the claims
                    with financial data (CARIN Blue Button), the prior
                    authorizations (PDex), Patient, Coverage, Organization.
  provider-access   (fhir-server-provider, port 8091, behind fhir-gateway) the
                    Provider Access and Payer-to-Payer APIs: the claims without
                    financial data (CARIN Basis profiles, same IDs), the prior
                    authorizations, Patient, Coverage, Organization, the
                    attribution lists (PDex Provider Group) and the opt-outs (PDex
                    Provider Access Consent).

Loads, in this order, so every reference points to a resource already on the
server:

  1. the search parameters CARIN Blue Button and Da Vinci PDex require that base
     FHIR R4 lacks (fhir/server/search_parameters/: ExplanationOfBenefit type,
     service-date, service-start-date, billable-period-start and use), before any
     data, so the server indexes the resources with them;
  2. Organization, Patient, Coverage, ExplanationOfBenefit (claims, legacy and
     PBM) and ExplanationOfBenefit (prior authorizations), from the gold models,
     then, on the provider-access server, Consent and Group.

Each batch is a FHIR transaction of PUTs (update by id), so a run either loads a
batch completely or not at all, and publishing again replaces the resources
instead of duplicating them. `make fhir-up` recreates the server empty, so a
resource removed from gold does not linger. Writes data/fhir_publish.json
(patient-access) and data/fhir_publish_provider.json (provider-access).

Usage (from the repository root, the servers running):
    python fhir/publish.py [--server all|patient-access|provider-access] [--batch 500]
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
import lakehouse  # noqa: E402

SEARCH_PARAMETERS = Path("fhir/server/search_parameters")
# (label, gold model), in dependency order.
MODELS = [
    ("Organization", "gold.fhir_organization"),
    ("Patient", "gold.fhir_patient"),
    ("Coverage", "gold.fhir_coverage"),
    ("ExplanationOfBenefit (claims)", "gold.fhir_explanation_of_benefit"),
    ("ExplanationOfBenefit (prior authorizations)", "gold.fhir_prior_authorization"),
]
PROVIDER_MODELS = [
    ("Organization", "gold.fhir_organization"),
    ("Patient", "gold.fhir_patient"),
    ("Coverage", "gold.fhir_coverage"),
    ("ExplanationOfBenefit (claims, without financial data)", "gold.fhir_explanation_of_benefit_basis"),
    ("ExplanationOfBenefit (prior authorizations)", "gold.fhir_prior_authorization"),
    ("Consent (Provider Access opt-outs)", "gold.fhir_provider_consent"),
    ("Group (attribution lists)", "gold.fhir_provider_group"),
]
# server -> (base URL, models, report)
SERVERS = {
    "patient-access": (os.environ.get("FHIR_BASE_URL", "http://localhost:8090/fhir"), MODELS,
                       Path("data/fhir_publish.json")),
    "provider-access": (os.environ.get("FHIR_PROVIDER_BASE_URL", "http://localhost:8091/fhir"), PROVIDER_MODELS,
                        Path("data/fhir_publish_provider.json")),
}


def request(method: str, url: str, body: dict | None = None, timeout: int = 300) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/fhir+json", "Accept": "application/fhir+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read()[:2000].decode(errors="replace")
        raise SystemExit(f"{method} {url} failed with HTTP {e.code}: {detail}") from e


def wait_for_server(base: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{base}/metadata", timeout=10) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        if time.time() > deadline:
            raise SystemExit(f"The FHIR server at {base} did not answer within {timeout} s (make fhir-up)")
        time.sleep(5)


def transaction(resources: list[dict]) -> dict:
    return {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}
        for r in resources]}


def put_all(base: str, resources: list[dict], batch: int) -> None:
    for start in range(0, len(resources), batch):
        _, response = request("POST", base, transaction(resources[start:start + batch]))
        failed = [e for e in response.get("entry", [])
                  if not e.get("response", {}).get("status", "").startswith(("200", "201"))]
        if failed:
            raise SystemExit(f"Transaction rejected entries: {failed[:3]}")


def publish(name: str, base: str, models: list, report_path: Path, batch: int) -> None:
    base = base.rstrip("/")
    wait_for_server(base)
    report = {"server": base, "apis": name, "loads": []}

    params = [json.loads(p.read_text()) for p in sorted(SEARCH_PARAMETERS.glob("*.json"))]
    started = time.time()
    put_all(base, params, batch)
    report["search_parameters"] = [p["url"] for p in params]
    print(f"[{name}] Search parameters: {len(params)} ({time.time() - started:.1f} s)")

    con = lakehouse.connect(read_only=True)
    try:
        for label, model in models:
            resources = [json.loads(r) for (r,) in con.execute(f"SELECT resource FROM {model} ORDER BY id").fetchall()]
            started = time.time()
            put_all(base, resources, batch)
            seconds = round(time.time() - started, 1)
            report["loads"].append({"resources": label, "model": model, "count": len(resources), "seconds": seconds})
            print(f"[{name}] {label}: {len(resources)} ({seconds} s)")
    finally:
        con.close()
    report_path.write_text(json.dumps(report, indent=2))
    print(f"[{name}] Written to {report_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", choices=["all", *SERVERS], default="all", help="which server to load")
    ap.add_argument("--batch", type=int, default=500, help="resources per transaction")
    args = ap.parse_args()
    for name, (base, models, report_path) in SERVERS.items():
        if args.server in ("all", name):
            publish(name, base, models, report_path, args.batch)


if __name__ == "__main__":
    main()
