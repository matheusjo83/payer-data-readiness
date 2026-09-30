"""Generate synthetic legacy payer data with deliberate data-quality issues.

All member, provider and claim data is fictitious (the drug codes are real NDCs,
see claim_details.py). Issues are injected at known rates so that the
lakehouse quality checks can later measure (and prove) that they were caught.

Dates are anchored to a reference date (--as-of) instead of the day the script
runs, so the same seed and reference date always produce the same data. The
default is the date of the figures published in docs/results.md.

Usage:
    python legacy_db/seed/generate_legacy_data.py --members 5000 --claims 25000
    python legacy_db/seed/generate_legacy_data.py --as-of 2027-01-15
"""

import argparse
import os
import random
import time
from datetime import date, datetime, timedelta

import psycopg

from claim_details import claim_details

# Override with LEGACY_PG_DSN (e.g. inside the Airflow container).
PG_DSN = os.environ.get(
    "LEGACY_PG_DSN", "host=localhost port=5433 dbname=payer_legacy user=legacy password=legacy"
)

# Injected issue rates (documented so results can be compared with what the
# quality layer detects).
RATE_DUPLICATE_MEMBER = 0.015
RATE_INVALID_GENDER = 0.010
RATE_INVALID_DOB = 0.010
RATE_CLAIM_UNKNOWN_MEMBER = 0.010
RATE_PAID_GT_CHARGED = 0.020
RATE_ORPHAN_LINE = 0.005
RATE_CLAIM_OUTSIDE_ELIGIBILITY = 0.010

# Reference date of the figures published in docs/results.md.
DEFAULT_AS_OF = date(2026, 9, 28)

FIRST = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
         "Linda", "David", "Elizabeth", "Maria", "Jose", "Wei", "Aisha", "Carlos"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis",
        "Rodriguez", "Martinez", "Hernandez", "Lopez", "Nguyen", "Lee", "Kim"]
STATES = ["TX", "TX", "TX", "OK", "LA", "NM", "AR"]
PLANS = [
    ("PLN001", "Commercial PPO", "COM"),
    ("PLN002", "Commercial HMO", "COM"),
    ("PLN003", "Medicare Advantage Plus", "MCR"),
    ("PLN004", "Medicaid Managed Care", "MCD"),
    ("PLN005", "CHIP Kids", "CHP"),
]
SPECIALTIES = ["FM", "IM", "PED", "CARD", "ORTH", "RAD", "ER", "OBGY"]
PROC_CODES = ["99213", "99214", "99203", "80053", "85025", "71046", "93000",
              "97110", "36415", "99285"]
DX_CODES = ["E11.9", "I10", "J06.9", "M54.5", "Z00.00", "E78.5", "F41.1",
            "K21.9", "J45.909", "N39.0"]
# Column order of the dicts returned by claim_details().
CLAIM_DETAIL_COLS = ["ntwk_cd", "bill_typ_cd", "pos_cd", "days_sply", "daw_cd", "rfl_nbr", "rfl_auth"]
LINE_DETAIL_COLS = ["ndc_cd", "rev_cd"]


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def npi_with_check_digit(base9: str) -> str:
    """Complete a 9-digit NPI base with its Luhn check digit (computed over the 80840 prefix)."""
    total = 0
    for i, ch in enumerate(reversed("80840" + base9)):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return base9 + str((10 - total % 10) % 10)


def rand_date(start: date, end: date) -> date:
    return start + timedelta(days=random.randint(0, (end - start).days))


def connect_with_retry(dsn: str, attempts: int = 20) -> psycopg.Connection:
    for i in range(attempts):
        try:
            return psycopg.connect(dsn)
        except psycopg.OperationalError:
            print(f"Waiting for Postgres... ({i + 1}/{attempts})")
            time.sleep(2)
    raise SystemExit("Could not connect to the legacy database. Is it running? (make up)")


def copy_rows(cur, table: str, cols: list[str], rows: list[tuple]) -> None:
    with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        for row in rows:
            cp.write_row(row)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--members", type=int, default=5000)
    ap.add_argument("--providers", type=int, default=300)
    ap.add_argument("--claims", type=int, default=25000)
    ap.add_argument("--prior-auths", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--as-of", type=date.fromisoformat, default=DEFAULT_AS_OF,
                    help="reference date (YYYY-MM-DD) that all generated dates are relative to "
                         f"(default: {DEFAULT_AS_OF})")
    args = ap.parse_args()
    random.seed(args.seed)
    # Claim details (claim_details.py) come from their own generator, so the
    # sequence of the original columns does not depend on them.
    detail_rng = random.Random(args.seed + 1)

    # "Now" is midnight at the start of the reference date.
    today = args.as_of
    now = datetime.combine(today, datetime.min.time())

    # Plans
    plans = [(pid, nm, lob, "20200101", "99991231") for pid, nm, lob in PLANS]

    # Members (+ duplicates, invalid gender, invalid DOB)
    members, member_ids = [], []
    for i in range(args.members):
        mid = f"M{i + 1:09d}"
        member_ids.append(mid)
        gender = random.choice(["M", "F", "F", "M", "U"])
        if random.random() < RATE_INVALID_GENDER:
            gender = random.choice([" ", "X", "9"])
        dob = ymd(rand_date(date(1940, 1, 1), date(2022, 12, 31)))
        if random.random() < RATE_INVALID_DOB:
            dob = random.choice(["", "19000000", "00000000", "2020131"])
        row = (mid, random.choice(FIRST), random.choice(LAST), dob, gender,
               f"{random.randint(73301, 79999)}", random.choice(STATES),
               now - timedelta(days=random.randint(30, 900)))
        members.append(row)
        if random.random() < RATE_DUPLICATE_MEMBER:
            dup = list(row)
            dup[5] = f"{random.randint(73301, 79999)}"   # address changed
            dup[7] = row[7] + timedelta(days=random.randint(1, 20))
            members.append(tuple(dup))

    # Eligibility spans
    elig, coverage = [], {}
    for mid in member_ids:
        pid = random.choice(PLANS)[0]
        eff = rand_date(date(2021, 1, 1), date(2025, 12, 31))
        term = "99991231" if random.random() < 0.8 else ymd(rand_date(eff, today))
        elig.append((mid, pid, ymd(eff), term, now))
        coverage[mid] = (eff, None if term == "99991231" else datetime.strptime(term, "%Y%m%d").date())

    # Providers
    providers = []
    for i in range(args.providers):
        # NPIs get a valid check digit (the draw is unchanged; its last digit is replaced).
        npi = npi_with_check_digit(f"{random.randint(10**9, 2 * 10**9 - 1)}"[:9])
        providers.append((f"P{i + 1:07d}", npi, f"{random.choice(LAST)} Clinic {i + 1}",
                          random.choice(SPECIALTIES), random.choice(STATES), now))
    provider_ids = [p[0] for p in providers]

    # Claims + lines
    # Service dates fall inside the member's coverage, except for the injected
    # claims outside eligibility (1-60 days before coverage starts).
    claim_window = (date(2024, 1, 1), today - timedelta(days=5))
    covered_ids = [mid for mid, (eff, term) in coverage.items()
                   if eff <= claim_window[1] and (term is None or term >= claim_window[0])]
    claims, lines = [], []
    for i in range(args.claims):
        cid = f"C{i + 1:012d}"
        mid = random.choice(covered_ids)
        eff, term = coverage[mid]
        svc = rand_date(max(eff, claim_window[0]), min(term or claim_window[1], claim_window[1]))
        if random.random() < RATE_CLAIM_OUTSIDE_ELIGIBILITY:
            svc = eff - timedelta(days=random.randint(1, 60))
        if random.random() < RATE_CLAIM_UNKNOWN_MEMBER:
            mid = f"M{random.randint(900000000, 999999999)}"
        rcvd = svc + timedelta(days=random.randint(1, 30))
        status = random.choices(["PD", "DN", "PN", "VD"], [80, 10, 7, 3])[0]
        adj = "" if status == "PN" else ymd(rcvd + timedelta(days=random.randint(1, 20)))
        n_lines = random.randint(1, 4)
        tot_chrg = tot_pd = 0.0
        claim_lines = []
        for ln in range(1, n_lines + 1):
            chrg = round(random.uniform(40, 1500), 2)
            pd = 0.0 if status in ("DN", "PN", "VD") else round(chrg * random.uniform(0.3, 0.9), 2)
            tot_chrg += chrg
            tot_pd += pd
            claim_lines.append([cid, ln, random.choice(PROC_CODES), random.choice(DX_CODES),
                                random.randint(1, 3), chrg, pd])
        if status == "PD" and random.random() < RATE_PAID_GT_CHARGED:
            tot_pd = tot_chrg * random.uniform(1.05, 1.5)
        prv_id = random.choice(provider_ids)
        claim_type = random.choices(["P", "I", "R"], [70, 20, 10])[0]
        svc_to = svc + timedelta(days=random.randint(0, 3))
        header, line_details = claim_details(detail_rng, claim_type, svc, svc_to, n_lines)
        claims.append((cid, mid, prv_id, claim_type, ymd(svc), ymd(svc_to),
                       ymd(rcvd), adj, status, round(tot_chrg, 2), round(tot_pd, 2),
                       *(header[c] for c in CLAIM_DETAIL_COLS), now))
        for line, details in zip(claim_lines, line_details):
            if claim_type == "R":
                line[2] = None   # pharmacy lines carry an NDC instead of a procedure code
            lines.append((*line, *(details[c] for c in LINE_DETAIL_COLS), now))

    # Orphan claim lines (header does not exist)
    n_orphans = int(len(lines) * RATE_ORPHAN_LINE)
    for i in range(n_orphans):
        lines.append((f"C9{i:011d}", 1, random.choice(PROC_CODES), random.choice(DX_CODES),
                      1, 100.0, 0.0, None, None, now))

    # Prior authorizations (decision time feeds the CMS-0057-F timeliness metric)
    pas = []
    for i in range(args.prior_auths):
        urgent = "Y" if random.random() < 0.2 else "N"
        req = now - timedelta(days=random.randint(1, 365), hours=random.randint(0, 23))
        if random.random() < 0.05:
            dcsn_ts, dcsn = None, "P"
        else:
            hours = random.expovariate(1 / (30 if urgent == "Y" else 90))
            dcsn_ts = req + timedelta(hours=hours)
            dcsn = random.choices(["A", "D"], [85, 15])[0]
        pas.append((f"PA{i + 1:012d}", random.choice(member_ids), random.choice(provider_ids),
                    random.choice(PROC_CODES), urgent, req, dcsn_ts, dcsn, now))

    with connect_with_retry(PG_DSN) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE pln, mbr_mstr, elig_span, prv, clm_hdr, clm_ln, pa_req RESTART IDENTITY")
        copy_rows(cur, "pln", ["pln_id", "pln_nm", "lob_cd", "eff_dt", "term_dt"], plans)
        copy_rows(cur, "mbr_mstr", ["mbr_id", "fst_nm", "lst_nm", "dob", "gndr_cd",
                                    "zip_cd", "st_cd", "upd_ts"], members)
        copy_rows(cur, "elig_span", ["mbr_id", "pln_id", "eff_dt", "term_dt", "upd_ts"], elig)
        copy_rows(cur, "prv", ["prv_id", "npi", "prv_nm", "spclty_cd", "st_cd", "upd_ts"], providers)
        copy_rows(cur, "clm_hdr", ["clm_id", "mbr_id", "prv_id", "clm_typ_cd", "svc_from_dt",
                                   "svc_to_dt", "rcvd_dt", "adj_dt", "clm_stat_cd",
                                   "tot_chrg_amt", "tot_pd_amt", *CLAIM_DETAIL_COLS, "upd_ts"], claims)
        copy_rows(cur, "clm_ln", ["clm_id", "ln_nbr", "proc_cd", "dx_cd", "units",
                                  "chrg_amt", "pd_amt", *LINE_DETAIL_COLS, "upd_ts"], lines)
        copy_rows(cur, "pa_req", ["pa_id", "mbr_id", "prv_id", "svc_cd", "urgnt_flg",
                                  "req_ts", "dcsn_ts", "dcsn_cd", "upd_ts"], pas)
        conn.commit()

    print(f"Loaded: {len(members)} member rows ({len(members) - len(member_ids)} duplicates), "
          f"{len(providers)} providers, {len(claims)} claims, {len(lines)} claim lines "
          f"({n_orphans} orphans), {len(pas)} prior auths, as of {today}.")


if __name__ == "__main__":
    main()
