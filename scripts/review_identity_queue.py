"""Work the identity review queue as a simulated reviewer, from the ground truth.

In production, people review the uncertain cases in gold.identity_review_queue
with both records side by side and record their decisions. This project has no
reviewers, so this script stands in for them: it decides each pending case from
the generator's ground truth (bronze.pbm_truth) and appends the decisions to the
seed dbt/seeds/identity_review_decisions.csv, the audit trail dbt applies over
the model. A simulated reviewer who reads the truth is always right, so the
results after review are an upper bound on what people would achieve.

  member_link     match with the true member when the model scored that member
                  as a candidate (a reviewer looks at every candidate, not only
                  the best); otherwise no_match on the best candidate.
  pbm_duplicate   match when the two cardholders are one person, else no_match.

Deciding cases can put new ones in the queue (merging two cardholder IDs of one
person, for example, changes which member links compete), so run dbt and this
script again until the queue is empty; `make review` does that.

Usage (from the repository root, after make dbt):
    python scripts/review_identity_queue.py        # exit 3 when it added decisions
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
import lakehouse  # noqa: E402

SEED = Path("dbt/seeds/identity_review_decisions.csv")
COLUMNS = ["review_type", "cardholder_id", "candidate_id", "decision", "reviewer", "decided_at", "note"]
REVIEWER = "simulated-reviewer"
# The decisions are dated on the reference date of the data (dbt var as_of_date),
# so that the same data always produces the same seed.
DECIDED_AT = "2026-09-28"


def main() -> None:
    con = lakehouse.connect(read_only=True)
    try:
        rows = con.execute("""
            with truth as (
                select cardholder_id, nullif(member_id, '') as member_id,
                       coalesce(nullif(member_id, ''), nullif(duplicate_of, ''), cardholder_id) as person_key
                from bronze.pbm_truth
            )
            select
                q.review_type,
                q.cardholder_id,
                case
                    when q.review_type = 'member_link' and s.member_id is not null then s.member_id
                    else q.candidate_id
                end as candidate_id,
                case
                    when q.review_type = 'member_link' and s.member_id is not null then 'match'
                    when q.review_type = 'pbm_duplicate' and a.person_key = b.person_key then 'match'
                    else 'no_match'
                end as decision,
                q.reason
            from gold.identity_review_queue q
            join truth a on a.cardholder_id = q.cardholder_id
            left join truth b on q.review_type = 'pbm_duplicate' and b.cardholder_id = q.candidate_id
            left join gold.identity_matches_splink s
                on q.review_type = 'member_link' and s.cardholder_id = q.cardholder_id
               and s.member_id = a.member_id
            order by q.review_type, q.cardholder_id, q.candidate_id
        """).fetchall()
    finally:
        con.close()

    with SEED.open(newline="") as f:
        existing = list(csv.DictReader(f))
    decided = {(r["review_type"], r["cardholder_id"], r["candidate_id"]) for r in existing}
    new = []
    for review_type, cardholder_id, candidate_id, decision, reason in rows:
        if (review_type, cardholder_id, candidate_id) in decided:
            continue
        decided.add((review_type, cardholder_id, candidate_id))
        new.append({"review_type": review_type, "cardholder_id": cardholder_id, "candidate_id": candidate_id,
                    "decision": decision, "reviewer": REVIEWER, "decided_at": DECIDED_AT,
                    "note": f"queued as {reason}; decided from the ground truth"})

    with SEED.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(existing + new, key=lambda r: (r["review_type"], r["cardholder_id"], r["candidate_id"])))
    counts = {}
    for r in new:
        counts[(r["review_type"], r["decision"])] = counts.get((r["review_type"], r["decision"]), 0) + 1
    print(f"{len(rows)} cases in the queue, {len(new)} new decisions {counts or ''}; "
          f"{len(existing) + len(new)} decisions in {SEED}")
    sys.exit(3 if new else 0)


if __name__ == "__main__":
    main()
