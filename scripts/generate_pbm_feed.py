"""Generate a synthetic PBM (pharmacy benefit manager) feed with known identity variations.

The PBM administers the pharmacy benefit for part of the payer's members and
issues its own cardholder IDs, so its files do not carry the payer's member ID.
The same person appears in both sources with the variations real feeds have:
nicknames, typos, a changed last name, transposed or mistyped birth dates, an
old address. The feed also holds people the payer does not know, including
deliberate look-alikes (same name and address, different birth date; same last
name, birth date and address, different first name).

Every variation is injected at a documented rate and recorded in a ground-truth
file (_truth.txt), so the identity resolution can be measured, as the legacy
data-quality checks are. The pipeline never reads the truth file except to
evaluate the matching (gold.identity_match_quality).

Members and coverage come from the legacy database (make seed must run first),
read in a fixed order and varied with a fixed seed, so the feed is reproducible.
Files are pipe-delimited, as PBM files often are, written to data/pbm/:

    pbm_members.txt     cardholder_id|first_name|last_name|birth_date|gender|zip_code|state
    pbm_pharmacies.txt  pharmacy_id|npi|pharmacy_name|state|updated_at
    pbm_claims.txt      one row per pharmacy claim, keyed by cardholder_id
    _truth.txt          cardholder_id|member_id|case|variations

Usage:
    python scripts/generate_pbm_feed.py
"""

import argparse
import csv
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "legacy_db" / "seed"))
from claim_details import NDC_CODES  # noqa: E402
from generate_legacy_data import (  # noqa: E402
    DEFAULT_AS_OF, FIRST, LAST, PG_DSN, STATES, connect_with_retry, npi_with_check_digit,
)

OUT_DIR = Path("data/pbm")

# Share of covered legacy members whose pharmacy benefit the PBM administers.
RATE_PBM_MEMBER = 0.60
# Variations applied independently to each PBM record of a legacy member.
RATE_NICKNAME = 0.08          # first name replaced by a common nickname (typo if none)
RATE_FIRST_TYPO = 0.04
RATE_LAST_TYPO = 0.04
RATE_LAST_CHANGED = 0.02      # different last name (e.g. after marriage)
RATE_DOB_SWAPPED = 0.02       # day and month transposed (when both are 12 or less)
RATE_DOB_TYPO = 0.02          # one digit of the day or of the year's last digit
RATE_DOB_MISSING = 0.01
RATE_ZIP_MOVED = 0.15         # an older or newer address
RATE_GENDER_MISSING = 0.03
# People the payer does not know, relative to the number of PBM records of members.
RATE_PBM_ONLY = 0.08          # unrelated people (e.g. dependents not yet enrolled)
RATE_SAME_NAME = 0.01         # a member's name, zip and gender, different birth date
RATE_TWIN = 0.005             # a member's last name, birth date and zip, other first name
# Claims outside the member's coverage (held back by the FHIR integrity gate).
RATE_FILL_OUTSIDE_COVERAGE = 0.01

NICKNAMES = {"James": "Jim", "Mary": "Molly", "Robert": "Bob", "Patricia": "Pat", "John": "Jack",
             "Jennifer": "Jen", "Michael": "Mike", "Linda": "Lindy", "David": "Dave",
             "Elizabeth": "Liz", "Maria": "Mia", "Jose": "Pepe", "Carlos": "Charlie"}


def typo(rng: random.Random, s: str) -> str:
    """One keying error: substitution, adjacent transposition, deletion or doubled letter."""
    i = rng.randrange(len(s))
    op = rng.choice(["sub", "swap", "del", "dup"])
    if op == "swap" and i < len(s) - 1:
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if op == "del" and len(s) > 3:
        return s[:i] + s[i + 1:]
    if op == "dup":
        return s[:i] + s[i] + s[i:]
    letter = rng.choice([c for c in "abcdefghijklmnopqrstuvwxyz" if c != s[i].lower()])
    return s[:i] + (letter.upper() if s[i].isupper() else letter) + s[i + 1:]


def parse_ymd(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def dob_typo(rng: random.Random, dob: date) -> date:
    """Mistype the day or the year's last digit, keeping a valid date that differs."""
    for _ in range(20):
        if rng.random() < 0.5:
            candidate = dob.replace(day=1) + timedelta(days=rng.randint(0, 27))
        else:
            try:
                candidate = dob.replace(year=dob.year - dob.year % 10 + rng.randint(0, 9))
            except ValueError:   # 29 February
                continue
        if candidate != dob:
            return candidate
    return dob + timedelta(days=1)


def load_legacy(as_of: date):
    with connect_with_retry(PG_DSN) as conn, conn.cursor() as cur:
        members = cur.execute(
            "SELECT DISTINCT ON (mbr_id) mbr_id, fst_nm, lst_nm, dob, gndr_cd, zip_cd, st_cd "
            "FROM mbr_mstr ORDER BY mbr_id, upd_ts DESC, mbr_sk DESC"
        ).fetchall()
        spans = cur.execute("SELECT mbr_id, eff_dt, term_dt FROM elig_span ORDER BY elig_sk").fetchall()
    coverage = {}
    for mbr_id, eff, term in spans:
        start, end = parse_ymd(eff), (None if term == "99991231" else parse_ymd(term))
        if start and start <= as_of:
            coverage.setdefault(mbr_id, []).append((start, min(end or as_of, as_of)))
    return members, coverage


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--as-of", type=date.fromisoformat, default=DEFAULT_AS_OF)
    args = ap.parse_args()
    rng = random.Random(args.seed + 2)   # its own stream, independent of the legacy generator
    members, coverage = load_legacy(args.as_of)
    claim_window = (date(2024, 1, 1), args.as_of - timedelta(days=1))

    people = []   # (record, truth) before cardholder IDs are assigned
    covered = [m for m in members if m[0] in coverage]
    for mbr_id, first, last, dob_raw, gender, zip_code, state in covered:
        if rng.random() >= RATE_PBM_MEMBER:
            continue
        variations = []
        first, last = first.strip(), last.strip()
        dob = parse_ymd(dob_raw or "")
        if dob is None:
            dob = rand_date(rng, date(1940, 1, 1), date(2022, 12, 31))
            variations.append("legacy_dob_invalid")
        gender = gender.strip().upper() if gender and gender.strip().upper() in ("M", "F", "U") else None
        if gender is None:
            gender = rng.choice(["M", "F"])
            variations.append("legacy_gender_invalid")
        if rng.random() < RATE_NICKNAME:
            if first in NICKNAMES:
                first = NICKNAMES[first]
                variations.append("nickname")
            else:
                first = typo(rng, first)
                variations.append("first_typo")
        if rng.random() < RATE_FIRST_TYPO:
            first = typo(rng, first)
            variations.append("first_typo")
        if rng.random() < RATE_LAST_TYPO:
            last = typo(rng, last)
            variations.append("last_typo")
        if rng.random() < RATE_LAST_CHANGED:
            last = rng.choice([n for n in LAST if n != last])
            variations.append("last_changed")
        if rng.random() < RATE_DOB_SWAPPED and dob.day <= 12 and dob.day != dob.month:
            dob = dob.replace(month=dob.day, day=dob.month)
            variations.append("dob_swapped")
        if rng.random() < RATE_DOB_TYPO:
            dob = dob_typo(rng, dob)
            variations.append("dob_typo")
        missing_dob = rng.random() < RATE_DOB_MISSING
        if missing_dob:
            variations.append("dob_missing")
        if rng.random() < RATE_ZIP_MOVED:
            zip_code = f"{rng.randint(73301, 79999)}"
            variations.append("zip_moved")
        if rng.random() < RATE_GENDER_MISSING:
            gender = None
            variations.append("gender_missing")
        record = [first, last, None if missing_dob else dob, gender, zip_code.strip(), state]
        people.append((record, [mbr_id, "member", ";".join(variations)], coverage[mbr_id]))

    n_members = len(people)
    for _ in range(round(n_members * RATE_PBM_ONLY)):
        record = [rng.choice(FIRST), rng.choice(LAST), rand_date(rng, date(1940, 1, 1), date(2022, 12, 31)),
                  rng.choice(["M", "F", "F", "M", "U"]), f"{rng.randint(73301, 79999)}", rng.choice(STATES)]
        people.append((record, [None, "pbm_only", ""], None))
    for case, rate in (("same_name", RATE_SAME_NAME), ("twin", RATE_TWIN)):
        for _ in range(round(n_members * rate)):
            _, first, last, dob_raw, gender, zip_code, state = rng.choice(covered)
            dob = parse_ymd(dob_raw or "") or rand_date(rng, date(1940, 1, 1), date(2022, 12, 31))
            if case == "same_name":
                other = dob
                while abs((other - dob).days) < 366:
                    other = rand_date(rng, date(1940, 1, 1), date(2022, 12, 31))
                gender = (gender or "").strip().upper()
                record = [first.strip(), last.strip(), other, gender if gender in ("M", "F", "U") else None,
                          zip_code.strip(), state]
            else:
                record = [rng.choice([n for n in FIRST if n != first.strip()]), last.strip(), dob,
                          rng.choice(["M", "F"]), zip_code.strip(), state]
            people.append((record, [None, case, ""], None))

    # Cardholder IDs are assigned in shuffled order, so they reveal nothing about the legacy order.
    rng.shuffle(people)
    as_of_midnight = datetime.combine(args.as_of, datetime.min.time())
    pharmacies = [(f"PH{i + 1:04d}", npi_with_check_digit(f"{rng.randint(10**8, 2 * 10**8 - 1)}"),
                   f"{rng.choice(LAST)} Pharmacy {i + 1}", rng.choice(STATES),
                   (as_of_midnight - timedelta(days=rng.randint(30, 900))).strftime("%Y-%m-%d %H:%M:%S"))
                  for i in range(25)]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    members_rows, truth_rows, claim_rows = [], [], []
    for n, (record, truth, spans) in enumerate(people, start=1):
        cardholder_id = f"RX{n:09d}"
        first, last, dob, gender, zip_code, state = record
        members_rows.append([cardholder_id, first.upper(), last.upper(),
                             dob.strftime("%m/%d/%Y") if dob else "", gender or "", zip_code, state])
        truth_rows.append([cardholder_id, truth[0] or "", truth[1], truth[2]])
        for _ in range(rng.choices([0, 1, 2, 3, 4], [15, 35, 25, 15, 10])[0]):
            if spans:
                start, end = rng.choice(spans)
                start, end = max(start, claim_window[0]), min(end, claim_window[1])
                if start > end:
                    continue
                fill = rand_date(rng, start, end)
                if rng.random() < RATE_FILL_OUTSIDE_COVERAGE:
                    fill = min(spans)[0] - timedelta(days=rng.randint(1, 60))
            else:
                fill = rand_date(rng, *claim_window)
            quantity = rng.choice([30, 30, 60, 90])
            refills_authorized = rng.randint(0, 5)
            cost = round(rng.uniform(5, 400), 2)
            patient_pay = min(cost, rng.choice([0, 5, 10, 15, 25, 40]))
            processed = datetime.combine(fill, datetime.min.time()) + timedelta(minutes=rng.randint(30, 36 * 60))
            claim_rows.append([
                f"PBM{len(claim_rows) + 1:010d}", cardholder_id, fill.isoformat(), rng.choice(NDC_CODES),
                quantity, 90 if quantity == 90 else 30, rng.choices(["0", "1", "2", "5"], [80, 10, 5, 5])[0],
                rng.randint(0, refills_authorized), refills_authorized, rng.choice(pharmacies)[0],
                rng.choices(["I", "O"], [92, 8])[0], f"{cost:.2f}", f"{patient_pay:.2f}",
                f"{cost - patient_pay:.2f}", rng.choices(["P", "R"], [96, 4])[0],
                processed.strftime("%Y-%m-%d %H:%M:%S"),
            ])

    def write(name: str, header: list[str], rows: list[list]) -> None:
        with (OUT_DIR / name).open("w", newline="") as f:
            w = csv.writer(f, delimiter="|", lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)

    write("pbm_members.txt", ["cardholder_id", "first_name", "last_name", "birth_date", "gender",
                              "zip_code", "state"], members_rows)
    write("pbm_pharmacies.txt", ["pharmacy_id", "npi", "pharmacy_name", "state", "updated_at"], pharmacies)
    write("pbm_claims.txt", ["rx_claim_id", "cardholder_id", "fill_date", "ndc", "quantity",
                             "days_supply", "daw_code", "refill_number", "refills_authorized",
                             "pharmacy_id", "network_code", "ingredient_cost", "patient_pay",
                             "plan_paid", "claim_status", "processed_at"], claim_rows)
    write("_truth.txt", ["cardholder_id", "member_id", "case", "variations"], truth_rows)
    cases = {}
    for _, t, _ in people:
        cases[t[1]] = cases.get(t[1], 0) + 1
    print(f"PBM feed: {len(members_rows)} cardholders ({cases}), {len(pharmacies)} pharmacies, "
          f"{len(claim_rows)} claims, as of {args.as_of}. Written to {OUT_DIR}/")


if __name__ == "__main__":
    main()
