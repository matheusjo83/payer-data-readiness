-- Cross-reference of PBM cardholders to legacy members: one row per cardholder,
-- from the Splink scores. A cardholder is linked automatically only when all of
-- these hold; otherwise a likely match goes to human review, not to a merge:
--   * the best candidate's match probability reaches identity_auto_link_probability;
--   * the first names do not disagree completely (guard against relatives who
--     share last name, birth date and address, e.g. twins);
--   * no second candidate reaches identity_review_probability;
--   * no other cardholder is linked automatically to the same member.
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
    left join best b using (cardholder_id)
),

claimed_twice as (
    select candidate_member_id
    from proposed
    where proposed_status = 'linked'
    group by candidate_member_id
    having count(*) > 1
)

select
    p.cardholder_id,
    case when p.proposed_status = 'linked' and c.candidate_member_id is null
         then p.candidate_member_id end                                  as member_id,
    p.candidate_member_id,
    p.match_probability,
    p.match_weight,
    p.second_probability,
    case when p.proposed_status = 'linked' and c.candidate_member_id is not null
         then 'review' else p.proposed_status end                        as status,
    case
        when p.proposed_status = 'linked' and c.candidate_member_id is not null then 'member_claimed_twice'
        when p.proposed_status = 'linked' then null
        when p.proposed_status = 'unmatched' then 'no_likely_candidate'
        when p.match_probability < {{ auto }} then 'probability_below_auto_link'
        when p.first_name_disagrees then 'first_name_disagrees'
        else 'second_candidate'
    end                                                                  as reason
from proposed p
left join claimed_twice c using (candidate_member_id)
