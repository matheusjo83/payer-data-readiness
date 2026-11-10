-- Likely matches that still need a person to decide, with both records side by
-- side. Two kinds of review: a PBM cardholder and its best legacy candidate
-- (member_link, from gold.member_xref), and two PBM cardholders that may be the
-- same person (pbm_duplicate, from gold.pbm_duplicate_pairs). A case leaves the
-- queue once identity_review_decisions holds a decision for it.
with pending as (
    select 'member_link' as review_type, cardholder_id, candidate_member_id as candidate_id,
           'legacy' as candidate_source, reason,
           match_probability, second_probability
    from {{ ref('member_xref') }}
    where status = 'review'
    union all
    select 'pbm_duplicate', cardholder_id, other_cardholder_id, 'pbm',
           review_reason,
           match_probability, null
    from {{ ref('pbm_duplicate_pairs') }}
    where status = 'review'
)

select
    q.review_type,
    q.cardholder_id,
    q.candidate_id,
    q.reason,
    round(q.match_probability, 4)   as match_probability,
    round(q.second_probability, 4)  as second_probability,
    p.first_name                    as pbm_first_name,
    c.first_name                    as candidate_first_name,
    p.last_name                     as pbm_last_name,
    c.last_name                     as candidate_last_name,
    p.birth_date                    as pbm_birth_date,
    c.birth_date                    as candidate_birth_date,
    p.gender                        as pbm_gender,
    c.gender                        as candidate_gender,
    p.zip_code                      as pbm_zip_code,
    c.zip_code                      as candidate_zip_code
from pending q
join {{ ref('identity_people') }} p on p.source_dataset = 'pbm' and p.unique_id = q.cardholder_id
join {{ ref('identity_people') }} c on c.source_dataset = q.candidate_source and c.unique_id = q.candidate_id
