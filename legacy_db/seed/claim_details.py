"""Claim-type-specific details shared by the generator and the activity simulator.

These are the fields the CARIN Blue Button profiles require beyond the original
legacy schema: network status, type of bill and revenue codes (institutional),
place of service (professional), NDC and dispensing details (pharmacy).

They are drawn from their own random generator, so adding them did not change
the random sequence of the original columns: every other generated value, and
every figure derived from it, stays the same.
"""

import random
from datetime import date

POS_CODES = ["11", "11", "11", "22", "23", "02"]          # office, outpatient hospital, ER, telehealth
REVENUE_CODES = ["0250", "0300", "0320", "0450", "0510"]  # pharmacy, lab, radiology, ER, clinic
ROOM_AND_BOARD = "0120"                                   # semi-private room (inpatient first line)
# Real NDCs (11-digit 5-4-2 form) of common generic drugs, taken from the FDA NDC
# directory (openFDA) and confirmed to exist in the NDC version tx.fhir.org uses.
NDC_CODES = [
    "00615800605",  # atorvastatin calcium tablets
    "68071503601",  # lisinopril tablets
    "68012000213",  # metformin extended-release tablets (Glumetza)
    "69097012705",  # amlodipine besylate tablets
    "62175011832",  # omeprazole delayed-release capsules
    "00074706911",  # levothyroxine tablets (Synthroid)
    "63187020425",  # albuterol sulfate inhalation solution
    "71610043545",  # sertraline tablets
    "68071433109",  # simvastatin tablets
    "63187055230",  # losartan potassium and hydrochlorothiazide tablets
]


def claim_details(rng: random.Random, claim_type: str, svc_from: date, svc_to: date,
                  n_lines: int) -> tuple[dict, list[dict]]:
    """Header fields and per-line fields for one claim (legacy column names).

    claim_type is the legacy code: P professional, I institutional, R pharmacy.
    Institutional claims spanning two or more days are billed as inpatient.
    """
    header = {"ntwk_cd": rng.choices(["I", "O"], [90, 10])[0], "bill_typ_cd": None,
              "pos_cd": None, "days_sply": None, "daw_cd": None, "rfl_nbr": None, "rfl_auth": None}
    lines = [{"ndc_cd": None, "rev_cd": None} for _ in range(n_lines)]
    if claim_type == "P":
        header["pos_cd"] = rng.choice(POS_CODES)
    elif claim_type == "I":
        inpatient = (svc_to - svc_from).days >= 2
        header["bill_typ_cd"] = "0111" if inpatient else "0131"
        for i, line in enumerate(lines):
            line["rev_cd"] = ROOM_AND_BOARD if inpatient and i == 0 else rng.choice(REVENUE_CODES)
    elif claim_type == "R":
        header["days_sply"] = rng.choice([30, 30, 30, 90, 7, 14])
        header["daw_cd"] = rng.choices(["0", "1", "2", "5"], [80, 10, 5, 5])[0]
        header["rfl_auth"] = rng.randint(0, 5)
        header["rfl_nbr"] = rng.randint(0, header["rfl_auth"])
        for line in lines:
            line["ndc_cd"] = rng.choice(NDC_CODES)
    return header, lines
