-- Every reviewer decision in the seed identity_review_decisions and whether it
-- still applies. The seed is the audit trail: who decided what and when, kept
-- under version control. A decision applies while the model still proposes the
-- case it decided: a match on any candidate the model scored for the cardholder,
-- a no_match on its best candidate, or a likely duplicate pair. After the data
-- changes, a decision on a case the model no longer proposes is stale and has no
-- effect.
with decisions as (
    select * from {{ ref('identity_review_decisions') }}
),

member_cases as (
    select s.cardholder_id, s.member_id as candidate_id, s.match_probability,
           s.member_id = x.candidate_member_id as is_best
    from {{ ref('identity_matches_splink') }} s
    join {{ ref('member_xref') }} x using (cardholder_id)
),

duplicate_cases as (
    select cardholder_id, other_cardholder_id as candidate_id, match_probability
    from {{ ref('pbm_duplicate_pairs') }}
)

select
    d.review_type,
    d.cardholder_id,
    d.candidate_id,
    d.decision,
    d.reviewer,
    d.decided_at,
    d.note,
    coalesce(m.match_probability, u.match_probability) as current_match_probability,
    coalesce(m.cardholder_id, u.cardholder_id) is not null as is_applied
from decisions d
left join member_cases m
    on d.review_type = 'member_link' and m.cardholder_id = d.cardholder_id and m.candidate_id = d.candidate_id
   and (d.decision = 'match' or m.is_best)
left join duplicate_cases u
    on d.review_type = 'pbm_duplicate' and u.cardholder_id = d.cardholder_id and u.candidate_id = d.candidate_id
