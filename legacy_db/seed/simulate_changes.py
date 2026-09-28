"""Simulate day-to-day activity in the legacy payer database.

Each event runs in its own transaction, like an OLTP application would, so
change data capture sees a realistic stream of inserts, updates and deletes:

    decide_prior_auth   pending prior authorization gets approved or denied
    new_prior_auth      new prior authorization request (pending)
    adjudicate_claim    pending claim becomes paid or denied (header + lines)
    new_claim           new claim with 1-4 lines
    move_member         member changes address (zip code)
    purge_void_claim    voided claim is deleted (header + lines)

Usage:
    python legacy_db/seed/simulate_changes.py --events 300 --duration 60
"""

import argparse
import random
import time
from datetime import datetime, timedelta

import psycopg

PG_DSN = "host=localhost port=5433 dbname=payer_legacy user=legacy password=legacy"

EVENT_WEIGHTS = {
    "decide_prior_auth": 25,
    "new_prior_auth": 15,
    "adjudicate_claim": 20,
    "new_claim": 25,
    "move_member": 10,
    "purge_void_claim": 5,
}
PROC_CODES = ["99213", "99214", "99203", "80053", "85025", "71046", "93000",
              "97110", "36415", "99285"]
DX_CODES = ["E11.9", "I10", "J06.9", "M54.5", "Z00.00", "E78.5", "F41.1",
            "K21.9", "J45.909", "N39.0"]


def ymd(d) -> str:
    return d.strftime("%Y%m%d")


class Simulator:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        cur = conn.cursor()
        self.member_ids = [r[0] for r in cur.execute("SELECT DISTINCT mbr_id FROM mbr_mstr")]
        self.provider_ids = [r[0] for r in cur.execute("SELECT prv_id FROM prv")]
        # New IDs continue the generator's sequences (orphan lines use the 'C9' range).
        self.next_claim = cur.execute(
            "SELECT coalesce(max(substr(clm_id, 2)::bigint), 0) + 1 FROM clm_hdr WHERE clm_id < 'C9'"
        ).fetchone()[0]
        self.next_pa = cur.execute(
            "SELECT coalesce(max(substr(pa_id, 3)::bigint), 0) + 1 FROM pa_req"
        ).fetchone()[0]

    def decide_prior_auth(self, cur, now) -> bool:
        row = cur.execute(
            "SELECT pa_id FROM pa_req WHERE dcsn_cd = 'P' ORDER BY random() LIMIT 1"
        ).fetchone()
        if not row:
            return False
        cur.execute(
            "UPDATE pa_req SET dcsn_ts = %s, dcsn_cd = %s, upd_ts = %s WHERE pa_id = %s",
            [now, random.choices(["A", "D"], [85, 15])[0], now, row[0]],
        )
        return True

    def new_prior_auth(self, cur, now) -> bool:
        pa_id = f"PA{self.next_pa:012d}"
        self.next_pa += 1
        cur.execute(
            "INSERT INTO pa_req (pa_id, mbr_id, prv_id, svc_cd, urgnt_flg, req_ts, dcsn_ts, dcsn_cd, upd_ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, NULL, 'P', %s)",
            [pa_id, random.choice(self.member_ids), random.choice(self.provider_ids),
             random.choice(PROC_CODES), "Y" if random.random() < 0.2 else "N", now, now],
        )
        return True

    def adjudicate_claim(self, cur, now) -> bool:
        row = cur.execute(
            "SELECT clm_id FROM clm_hdr WHERE clm_stat_cd = 'PN' ORDER BY random() LIMIT 1"
        ).fetchone()
        if not row:
            return False
        clm_id = row[0]
        paid = random.random() < 0.85
        if paid:
            cur.execute(
                "UPDATE clm_ln SET pd_amt = round(chrg_amt * (0.3 + random() * 0.6)::numeric, 2), upd_ts = %s "
                "WHERE clm_id = %s",
                [now, clm_id],
            )
        cur.execute(
            "UPDATE clm_hdr SET clm_stat_cd = %s, adj_dt = %s, upd_ts = %s, "
            "tot_pd_amt = coalesce((SELECT sum(pd_amt) FROM clm_ln WHERE clm_ln.clm_id = clm_hdr.clm_id), 0) "
            "WHERE clm_id = %s",
            ["PD" if paid else "DN", ymd(now), now, clm_id],
        )
        return True

    def new_claim(self, cur, now) -> bool:
        clm_id = f"C{self.next_claim:012d}"
        self.next_claim += 1
        svc = now.date() - timedelta(days=random.randint(1, 30))
        charges = [round(random.uniform(40, 1500), 2) for _ in range(random.randint(1, 4))]
        cur.execute(
            "INSERT INTO clm_hdr (clm_id, mbr_id, prv_id, clm_typ_cd, svc_from_dt, svc_to_dt, rcvd_dt, "
            "adj_dt, clm_stat_cd, tot_chrg_amt, tot_pd_amt, upd_ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, '', 'PN', %s, 0, %s)",
            [clm_id, random.choice(self.member_ids), random.choice(self.provider_ids),
             random.choices(["P", "I", "R"], [70, 20, 10])[0], ymd(svc), ymd(svc), ymd(now),
             round(sum(charges), 2), now],
        )
        for ln, chrg in enumerate(charges, start=1):
            cur.execute(
                "INSERT INTO clm_ln (clm_id, ln_nbr, proc_cd, dx_cd, units, chrg_amt, pd_amt, upd_ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, 0, %s)",
                [clm_id, ln, random.choice(PROC_CODES), random.choice(DX_CODES),
                 random.randint(1, 3), chrg, now],
            )
        return True

    def move_member(self, cur, now) -> bool:
        # Legacy duplicates share mbr_id; the application updates the latest row.
        cur.execute(
            "UPDATE mbr_mstr SET zip_cd = %s, upd_ts = %s WHERE mbr_sk = ("
            "  SELECT mbr_sk FROM mbr_mstr WHERE mbr_id = %s ORDER BY upd_ts DESC LIMIT 1)",
            [f"{random.randint(73301, 79999)}", now, random.choice(self.member_ids)],
        )
        return True

    def purge_void_claim(self, cur, now) -> bool:
        row = cur.execute(
            "SELECT clm_id FROM clm_hdr WHERE clm_stat_cd = 'VD' ORDER BY random() LIMIT 1"
        ).fetchone()
        if not row:
            return False
        cur.execute("DELETE FROM clm_ln WHERE clm_id = %s", [row[0]])
        cur.execute("DELETE FROM clm_hdr WHERE clm_id = %s", [row[0]])
        return True

    def run_event(self, name: str) -> bool:
        with self.conn.transaction(), self.conn.cursor() as cur:
            return getattr(self, name)(cur, datetime.now())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=300)
    ap.add_argument("--duration", type=float, default=60.0,
                    help="seconds to spread the events over (0 = as fast as possible)")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    names, weights = list(EVENT_WEIGHTS), list(EVENT_WEIGHTS.values())
    done = {n: 0 for n in names}
    pause = args.duration / args.events if args.events else 0
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        sim = Simulator(conn)
        for _ in range(args.events):
            name = random.choices(names, weights)[0]
            if sim.run_event(name):
                done[name] += 1
            if pause:
                time.sleep(random.uniform(0, 2 * pause))

    print("Events committed: " + ", ".join(f"{n}={c}" for n, c in done.items()))


if __name__ == "__main__":
    main()
