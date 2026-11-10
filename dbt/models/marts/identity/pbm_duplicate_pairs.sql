-- Pairs of PBM cardholders that may be the same person, from the Splink dedupe
-- scores, with the decision for each pair. The rules mirror member_xref: a pair
-- is merged automatically only when the match probability reaches
-- identity_auto_link_probability and the first names do not disagree completely
-- (relatives such as twins share last name, birth date and address); a likely
-- pair that fails a condition goes to human review. The payer's records weigh
-- in too, both ways, and never merge on their own: when each cardholder has a
-- strong legacy candidate and the candidates are different members, the payer
-- holds them as two people, so the pair is not merged automatically; when both
-- have the same member as their likely candidate (from
-- identity_review_probability), the pair goes to review even if its own score is
-- lower, since linking both cardholders to that member would make them one
-- person. A reviewer's decision (seed identity_review_decisions) takes
-- precedence over the model.
{% set auto = var('identity_auto_link_probability') %}
{% set review = var('identity_review_probability') %}
with strong_candidate as (
    select cardholder_id, member_id
    from {{ ref('identity_matches_splink') }}
    where match_probability >= {{ auto }}
    qualify row_number() over (partition by cardholder_id order by match_probability desc, member_id) = 1
),

likely_candidate as (
    select cardholder_id, member_id
    from {{ ref('identity_matches_splink') }}
    where match_probability >= {{ review }}
    qualify row_number() over (partition by cardholder_id order by match_probability desc, member_id) = 1
),

pairs as (
    select
        d.*,
        a.member_id is not null and b.member_id is not null and a.member_id <> b.member_id
            as different_members,
        la.member_id is not null and la.member_id = lb.member_id as same_member
    from {{ ref('identity_pbm_duplicates_splink') }} d
    left join strong_candidate a on a.cardholder_id = d.cardholder_id
    left join strong_candidate b on b.cardholder_id = d.other_cardholder_id
    left join likely_candidate la on la.cardholder_id = d.cardholder_id
    left join likely_candidate lb on lb.cardholder_id = d.other_cardholder_id
    where d.match_probability >= {{ review }} or (la.member_id is not null and la.member_id = lb.member_id)
),

scored as (
    select
        *,
        case
            when match_probability < {{ review }} then 'same_legacy_candidate'
            when match_probability < {{ auto }} then 'probability_below_auto_link'
            when gamma_first_name = 0 then 'first_name_disagrees'
            when different_members then 'different_legacy_members'
        end as review_reason,
        case
            when match_probability < {{ auto }} then 'review'
            when gamma_first_name = 0 then 'review'
            when different_members then 'review'
            else 'duplicate'
        end as model_status
    from pairs
),

decisions as (
    select cardholder_id, candidate_id as other_cardholder_id, decision, reviewer, decided_at
    from {{ ref('identity_review_decisions') }}
    where review_type = 'pbm_duplicate'
)

select
    s.cardholder_id,
    s.other_cardholder_id,
    round(s.match_probability, 6)                           as match_probability,
    s.gamma_first_name = 0                                  as first_name_disagrees,
    s.different_members                                     as linked_to_different_members,
    s.same_member                                           as same_legacy_candidate,
    s.model_status,
    s.review_reason,
    case d.decision
        when 'match' then 'duplicate'
        when 'no_match' then 'distinct'
        else s.model_status
    end                                                     as status,
    case when d.decision is not null then 'review' else 'model' end as decided_by,
    d.reviewer                                              as reviewed_by,
    d.decided_at                                            as reviewed_at
from scored s
left join decisions d using (cardholder_id, other_cardholder_id)
