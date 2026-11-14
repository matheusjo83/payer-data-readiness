"""Simulated clinical reviewer of the prior authorization requests pended by Da Vinci PAS.

The PAS service (fhir/gateway/prior_auth.py) approves a request whose
documentation meets the service's criteria and pends the others (X12 306 A4).
This script plays the payer's reviewer on the utilization management system:
it decides every pended request that came through PAS (pa_req channel F),
from the answers stored with it (pa_doc), with each service's review rule
(prior_auth.RULES): physical therapy is approved when measurable functional
goals are documented, chest imaging when there was no chest imaging in the
last 30 days; otherwise the request is denied (X12 306 A3). The decision is
written to pa_req, as a reviewer's would be: CDC carries it to the lakehouse,
and the PAS service notifies the provider's subscriptions.

Usage (from the repository root, with the legacy database up):
    python fhir/prior_auth_review.py            # decide every pended PAS request
    python fhir/prior_auth_review.py PA000000003001 [...]   # only these
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "gateway"))
import prior_auth  # noqa: E402


def review(pa_ids: list[str] | None = None) -> dict[str, str]:
    decided = {}
    with prior_auth.connect() as conn, conn.cursor() as cur:
        where = "src_cd = 'F' AND dcsn_cd = 'P'" + (" AND pa_id = ANY(%s)" if pa_ids else "")
        pended = cur.execute(f"SELECT pa_id, svc_cd FROM pa_req WHERE {where} ORDER BY pa_id",
                             [pa_ids] if pa_ids else []).fetchall()
        for pa_id, svc_cd in pended:
            answers = {k: v for k, (v, _) in prior_auth.load_answers(cur, pa_id).items()}
            decision = prior_auth.RULES[svc_cd].review(answers)
            t = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
            cur.execute("UPDATE pa_req SET dcsn_cd = %s, dcsn_ts = %s, upd_ts = %s WHERE pa_id = %s",
                        [decision, t, t, pa_id])
            decided[pa_id] = decision
        conn.commit()
    return decided


def main() -> None:
    decided = review(sys.argv[1:] or None)
    approved = sum(d == "A" for d in decided.values())
    print(f"Reviewed {len(decided)} pended PAS requests: {approved} approved, {len(decided) - approved} denied")


if __name__ == "__main__":
    main()
