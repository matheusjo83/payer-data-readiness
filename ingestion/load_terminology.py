"""Load public code system releases into the bronze layer, as reference data.

Two code systems the FHIR resources use are published free of charge by the
US government, so the lakehouse can check every code against them, not only
the sample the HL7 validator sees:

  ICD-10-CM   CMS "code descriptions in tabular order", one release per fiscal
              year (October 1 to September 30), FY2021 to FY2026, which covers
              the claims' service dates. Each release lists the categories as
              well as the codes, with a flag for the codes valid for
              submission (billable). Pinned by URL and SHA-256.
  NDC         The FDA National Drug Code Directory (package file). The FDA
              publishes only the current directory, updated daily, so it cannot
              be pinned: its Last-Modified date and SHA-256 are recorded with
              the load, in bronze.terminology_releases.

Licensed code systems (CPT, NUBC revenue codes) are not loaded; the checks
report them as unverified.

Files are cached in data/terminology/, so later runs work offline. Usage (from
the repository root):
    python ingestion/load_terminology.py
    python ingestion/load_terminology.py --refresh-ndc   # download the NDC directory again
"""

import argparse
import hashlib
import re
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pyarrow as pa

from common import connect, logged_load, new_run_id

CACHE_DIR = Path("data/terminology")
CMS = "https://www.cms.gov/files/zip/"
# Fiscal year -> (URL, SHA-256 of the zip). The October release of each year;
# the April addenda some years have are not loaded.
ICD10CM_RELEASES = {
    2021: (CMS + "2021-code-descriptions-tabular-order-updated-12162020.zip",
           "479be4d5773fb2e8357c5edb8a56bbd48da4d2bca443a7c42527ad102e3b388c"),
    2022: (CMS + "2022-code-descriptions-tabular-order.zip",
           "423206a194dd6db8384c3b39699ad3c1efa19718677fa81690557732a31bf560"),
    2023: (CMS + "2023-code-descriptions-tabular-order.zip",
           "cc7158228f6de01aa08650cc3afe446973a879dd78d44f1fec7150d45cfe1e06"),
    2024: (CMS + "2024-code-descriptions-tabular-order.zip",
           "4b30a68a011edb4aaa5ad3747971ef26f3be09eb925821d78b0d4584be977e3e"),
    2025: (CMS + "2025-code-descriptions-tabular-order.zip",
           "4f6c65f0034736d2b02fa9eb1128aab564fa673dd4d99363f8b2167af3994816"),
    2026: (CMS + "2026-code-descriptions-tabular-order.zip",
           "55a9124a27ca78a4a5c41e1101eda064ec2e08d105ff9e78d1fbd1a83298ef11"),
}
NDC_URL = "https://www.accessdata.fda.gov/cder/ndctext.zip"
# cms.gov rejects requests without a browser-like User-Agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (payer-data-readiness reference data loader)"}


def download(url: str, path: Path, refresh: bool = False) -> str | None:
    """Download url to path unless cached. Returns the Last-Modified header, if downloaded."""
    if path.exists() and not refresh:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=300) as resp:
        data = resp.read()
        modified = resp.headers.get("Last-Modified")
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    if modified:
        path.with_suffix(".modified").write_text(modified)
    return modified


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def icd10cm_rows(fiscal_year: int, zip_path: Path) -> list[tuple]:
    """Parse the fixed-width order file: order number (5), code (7), billable flag (1),
    short description (60), long description."""
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if re.fullmatch(r"icd10cm_order_\d{4}\.txt", Path(n).name))
        text = z.read(name).decode("latin-1")
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        rows.append((fiscal_year, int(line[0:5]), line[6:13].strip(), line[14] == "1",
                     line[16:76].strip(), line[77:].strip()))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-ndc", action="store_true", help="download the NDC directory again")
    args = ap.parse_args()

    releases = []
    icd_rows = []
    for fy, (url, expected) in ICD10CM_RELEASES.items():
        path = CACHE_DIR / f"icd10cm_fy{fy}.zip"
        download(url, path)
        digest = sha256(path)
        if expected and digest != expected:
            raise SystemExit(f"SHA-256 mismatch for {path}: {digest} (expected {expected})")
        icd_rows += icd10cm_rows(fy, path)
        releases.append(("icd-10-cm", f"FY{fy}", url, digest, None))

    ndc_zip = CACHE_DIR / "ndctext.zip"
    download(NDC_URL, ndc_zip, refresh=args.refresh_ndc)
    modified_file = ndc_zip.with_suffix(".modified")
    modified = (parsedate_to_datetime(modified_file.read_text()) if modified_file.exists()
                else datetime.fromtimestamp(ndc_zip.stat().st_mtime, timezone.utc))
    releases.append(("ndc", modified.strftime("%Y-%m-%d"), NDC_URL, sha256(ndc_zip),
                     modified.replace(tzinfo=None)))
    with zipfile.ZipFile(ndc_zip) as z:
        package_txt = z.read("package.txt")
    package_path = CACHE_DIR / "ndc_package.txt"
    package_path.write_bytes(package_txt)

    con = connect()
    run_id = new_run_id()
    try:
        with logged_load(con, run_id, "terminology.releases", "bronze.terminology_releases"):
            con.execute("""
                CREATE OR REPLACE TABLE bronze.terminology_releases (
                    code_system VARCHAR, version VARCHAR, url VARCHAR, sha256 VARCHAR,
                    published_at TIMESTAMP, _loaded_at TIMESTAMP DEFAULT current_timestamp,
                    _run_id VARCHAR)
            """)
            con.executemany("INSERT INTO bronze.terminology_releases VALUES (?, ?, ?, ?, ?, current_timestamp, ?)",
                            [(*r, run_id) for r in releases])
        with logged_load(con, run_id, "terminology.icd10cm", "bronze.terminology_icd10cm"):
            icd = pa.Table.from_arrays(
                [pa.array(col) for col in zip(*icd_rows)],
                names=["fiscal_year", "order_number", "code", "is_billable",
                       "short_description", "long_description"])
            con.register("icd_arrow", icd)
            con.execute("""
                CREATE OR REPLACE TABLE bronze.terminology_icd10cm AS
                SELECT fiscal_year::INTEGER AS fiscal_year, order_number::INTEGER AS order_number,
                       code, is_billable, short_description, long_description,
                       current_timestamp AS _loaded_at, ? AS _run_id
                FROM icd_arrow
            """, [run_id])
            con.unregister("icd_arrow")
        with logged_load(con, run_id, "terminology.ndc", "bronze.terminology_ndc_package"):
            con.execute(f"""
                CREATE OR REPLACE TABLE bronze.terminology_ndc_package AS
                SELECT *, current_timestamp AS _loaded_at, '{run_id}' AS _run_id
                FROM read_csv('{package_path.as_posix()}', delim = '\t', header = true,
                              all_varchar = true, encoding = 'latin-1', quote = '')
            """)
    finally:
        con.close()


if __name__ == "__main__":
    main()
