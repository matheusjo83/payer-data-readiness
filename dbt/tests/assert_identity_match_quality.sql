-- Quality gate for identity resolution on the synthetic data, where the truth is
-- known: the automatic links must be all correct (no person linked to someone
-- else) and must find at least 95% of the members the PBM shares with the payer;
-- after the review decisions, no link may be wrong either.
select *
from {{ ref('identity_match_quality') }}
where scope = 'all'
  and ((method = 'splink' and (linked_wrong > 0 or recall < 0.95))
       or (method = 'splink_reviewed' and linked_wrong > 0))
