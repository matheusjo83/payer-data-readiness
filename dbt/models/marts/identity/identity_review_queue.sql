-- Likely matches that need a person to decide, with both records side by side.
select
    x.cardholder_id,
    x.candidate_member_id,
    x.reason,
    round(x.match_probability, 4)   as match_probability,
    round(x.second_probability, 4)  as second_probability,
    p.first_name                    as pbm_first_name,
    l.first_name                    as legacy_first_name,
    p.last_name                     as pbm_last_name,
    l.last_name                     as legacy_last_name,
    p.birth_date                    as pbm_birth_date,
    l.birth_date                    as legacy_birth_date,
    p.gender                        as pbm_gender,
    l.gender                        as legacy_gender,
    p.zip_code                      as pbm_zip_code,
    l.zip_code                      as legacy_zip_code
from {{ ref('member_xref') }} x
join {{ ref('identity_people') }} p on p.source_dataset = 'pbm' and p.unique_id = x.cardholder_id
join {{ ref('identity_people') }} l on l.source_dataset = 'legacy' and l.unique_id = x.candidate_member_id
where x.status = 'review'
