-- Cross-reference of PBM cardholders to legacy members: one row per cardholder,
-- from the Splink scores. A cardholder is linked automatically only when all of
-- these hold; otherwise a likely match goes to human review, not to a merge:
--   * the best candidate's match probability reaches identity_auto_link_probability;
--   * the first names do not disagree completely (guard against relatives who
--     share last name, birth date and address, e.g. twins);
--   * no second candidate reaches identity_review_probability;
--   * no other PBM person is linked to the same member. Cardholders of one
--     person (gold.pbm_persons: duplicate cardholder IDs) may share a member.
-- A reviewer's decision (seed identity_review_decisions) takes precedence over
-- the model: match links the cardholder to the member the reviewer chose, which
-- may be any candidate the model scored, not only the best; no_match on the best
-- candidate leaves the cardholder unmatched. model_status is what the model
-- alone decided, before any review.
{% set auto = var('identity_auto_link_probability') %}
{% set review = var('identity_review_probability') %}
with ranked as (
    select
        *,
        row_number() over (partition by cardholder_id order by match_probability desc, member_id) as candidate_rank
    from {{ ref('identity_matches_splink') }}
),

best as (
    select b.*, s.match_probability as second_probability
    from ranked b
    left join ranked s on s.cardholder_id = b.cardholder_id and s.candidate_rank = 2
    where b.candidate_rank = 1
),

proposed as (
    select
        m.cardholder_id,
        p.pbm_person_id,
        b.member_id            as candidate_member_id,
        b.match_probability,
        b.match_weight,
        b.second_probability,
        b.gamma_first_name = 0 as first_name_disagrees,
        case
            when b.match_probability is null or b.match_probability < {{ review }} then 'unmatched'
            when b.match_probability < {{ auto }} then 'review'
            when b.gamma_first_name = 0 then 'review'
            when coalesce(b.second_probability, 0) >= {{ review }} then 'review'
            else 'linked'
        end                    as proposed_status
    from {{ ref('stg_pbm__members') }} m
    join {{ ref('pbm_persons') }} p using (cardholder_id)
    left join best b using (cardholder_id)
),

-- Decisions on a pair the model scored; a match decision wins over a no_match.
decisions as (
    select d.cardholder_id, d.candidate_id, d.decision, d.reviewer, d.decided_at
    from {{ ref('identity_review_decisions') }} d
    join {{ ref('identity_matches_splink') }} s
        on s.cardholder_id = d.cardholder_id and s.member_id = d.candidate_id
    where d.review_type = 'member_link'
),

decided as (
    select
        p.*,
        coalesce(m.decision, n.decision)        as decision,
        m.candidate_id                          as reviewed_member_id,
        coalesce(m.reviewer, n.reviewer)        as reviewer,
        coalesce(m.decided_at, n.decided_at)    as decided_at
    from proposed p
    left join decisions m
        on m.cardholder_id = p.cardholder_id and m.decision = 'match'
    left join decisions n
        on n.cardholder_id = p.cardholder_id and n.decision = 'no_match'
       and n.candidate_id = p.candidate_member_id
),

-- Members that more than one PBM person would be linked to: by the model alone,
-- and once the review decisions apply.
claimed_twice_by_model as (
    select candidate_member_id
    from decided
    where proposed_status = 'linked'
    group by candidate_member_id
    having count(distinct pbm_person_id) > 1
),

claimed_twice as (
    select coalesce(reviewed_member_id, candidate_member_id) as candidate_member_id
    from decided
    where (proposed_status = 'linked' and decision is null) or decision = 'match'
    group by 1
    having count(distinct pbm_person_id) > 1
),

statuses as (
    select
        d.*,
        case when d.proposed_status = 'linked' and cm.candidate_member_id is not null
             then 'review' else d.proposed_status end                       as model_status,
        case
            when d.decision = 'match' then 'linked'
            when d.decision = 'no_match' then 'unmatched'
            when d.proposed_status = 'linked' and c.candidate_member_id is not null then 'review'
            else d.proposed_status
        end                                                                 as status,
        cm.candidate_member_id is not null or c.candidate_member_id is not null as member_claimed_twice
    from decided d
    left join claimed_twice_by_model cm using (candidate_member_id)
    left join claimed_twice c using (candidate_member_id)
)

select
    cardholder_id,
    pbm_person_id,
    case when status = 'linked' then coalesce(reviewed_member_id, candidate_member_id) end as member_id,
    candidate_member_id,
    match_probability,
    match_weight,
    second_probability,
    model_status,
    status,
    case
        when decision = 'match' then 'confirmed_in_review'
        when decision = 'no_match' then 'rejected_in_review'
        when status = 'linked' then null
        when proposed_status = 'linked' and member_claimed_twice then 'member_claimed_twice'
        when proposed_status = 'unmatched' then 'no_likely_candidate'
        when match_probability < {{ auto }} then 'probability_below_auto_link'
        when first_name_disagrees then 'first_name_disagrees'
        else 'second_candidate'
    end                                                                  as reason,
    case when decision is not null then 'review' else 'model' end      as decided_by,
    reviewer                                                             as reviewed_by,
    decided_at                                                           as reviewed_at
from statuses
