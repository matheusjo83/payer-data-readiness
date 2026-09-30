-- Quality gate for identity resolution on the synthetic data, where the truth is
-- known: the automatic links must be all correct (no person linked to someone
-- else) and must find at least 95% of the members the PBM shares with the payer.
select *
from {{ ref('identity_match_quality') }}
where method = 'splink'
  and scope = 'all'
  and (linked_wrong > 0 or recall < 0.95)
