"""Publish the gold FHIR resources to the FHIR server (make fhir-publish).

Loads, in this order, so every reference points to a resource already on the
server:

  1. the search parameters CARIN Blue Button and Da Vinci PDex require that base
     FHIR R4 lacks (fhir/server/search_parameters/: ExplanationOfBenefit type,
     service-date, service-start-date, billable-period-start and use), before any
     data, so the server indexes the resources with them;
  2. Organization, Patient, Coverage, ExplanationOfBenefit (claims, legacy and
     PBM) and ExplanationOfBenefit (prior authorizations), from the gold models.

Each batch is a FHIR transaction of PUTs (update by id), so a run either loads a
batch completely or not at all, and publishing again replaces the resources
instead of duplicating them. `make fhir-up` recreates the server empty, so a
resource removed from gold does not linger. Writes data/fhir_publish.json.

Usage (from the repository root, the server running):
    python fhir/publish.py [--base http://localhost:8090/fhir] [--batch 500]
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

LAKEHOUSE = "data/lakehouse.duckdb"
SEARCH_PARAMETERS = Path("fhir/server/search_parameters")
REPORT = Path("data/fhir_publish.json")
DEFAULT_BASE = os.environ.get("FHIR_BASE_URL", "http://localhost:8090/fhir")
# (label, gold model), in dependency order.
MODELS = [
    ("Organization", "gold.fhir_organization"),
    ("Patient", "gold.fhir_patient"),
    ("Coverage", "gold.fhir_coverage"),
    ("ExplanationOfBenefit (claims)", "gold.fhir_explanation_of_benefit"),
    ("ExplanationOfBenefit (prior authorizations)", "gold.fhir_prior_authorization"),
]


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE, help="FHIR server base URL")
    ap.add_argument("--batch", type=int, default=500, help="resources per transaction")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    wait_for_server(base)
    report = {"server": base, "loads": []}

    params = [json.loads(p.read_text()) for p in sorted(SEARCH_PARAMETERS.glob("*.json"))]
    started = time.time()
    put_all(base, params, args.batch)
    report["search_parameters"] = [p["url"] for p in params]
    print(f"Search parameters: {len(params)} ({time.time() - started:.1f} s)")

    con = duckdb.connect(LAKEHOUSE, read_only=True)
    try:
        for label, model in MODELS:
            resources = [json.loads(r) for (r,) in con.execute(f"SELECT resource FROM {model} ORDER BY id").fetchall()]
            started = time.time()
            put_all(base, resources, args.batch)
            seconds = round(time.time() - started, 1)
            report["loads"].append({"resources": label, "model": model, "count": len(resources), "seconds": seconds})
            print(f"{label}: {len(resources)} ({seconds} s)")
    finally:
        con.close()
    REPORT.write_text(json.dumps(report, indent=2))
    print(f"Written to {REPORT}")


if __name__ == "__main__":
    main()
