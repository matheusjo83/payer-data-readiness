-- Deduplication within the PBM measured against the generator's ground truth
-- (source pbm.truth): pairs of cardholder IDs that belong to the same person.
-- Two cardholders are truly the same person when the truth gives them the same
-- legacy member, or when one is a duplicate of the other. A pair is merged when
-- gold.pbm_duplicate_pairs decided it is a duplicate, by the model alone
-- (method splink) or after the review decisions (splink_reviewed).
with truth as (
    select cardholder_id,
           coalesce(nullif(member_id, ''), nullif(duplicate_of, ''), cardholder_id) as person_key
    from {{ source('pbm', 'truth') }}
),

true_pairs as (
    select a.cardholder_id, b.cardholder_id as other_cardholder_id
    from truth a
    join truth b on a.person_key = b.person_key and a.cardholder_id < b.cardholder_id
),

methods as (
    select 'splink' as method, cardholder_id, other_cardholder_id, model_status as status
    from {{ ref('pbm_duplicate_pairs') }}
    union all
    select 'splink_reviewed', cardholder_id, other_cardholder_id, status
    from {{ ref('pbm_duplicate_pairs') }}
),

scored as (
    select m.method, t.cardholder_id is not null as is_true, m.status
    from methods m
    left join true_pairs t using (cardholder_id, other_cardholder_id)
    union all
    -- True pairs the model did not score as likely: missed by both methods.
    select method, true, 'distinct'
    from true_pairs t
    cross join (values ('splink'), ('splink_reviewed')) as v(method)
    where not exists (
        select 1 from {{ ref('pbm_duplicate_pairs') }} p
        where p.cardholder_id = t.cardholder_id and p.other_cardholder_id = t.other_cardholder_id)
)

select
    method,
    count(*) filter (where is_true)                              as true_pairs,
    count(*) filter (where status = 'duplicate' and is_true)     as merged_correct,
    count(*) filter (where status = 'duplicate' and not is_true) as merged_wrong,
    count(*) filter (where status <> 'duplicate' and is_true)    as not_merged,
    count(*) filter (where status = 'review')                    as in_review,
    round(count(*) filter (where status = 'duplicate' and is_true)
          / nullif(count(*) filter (where status = 'duplicate'), 0), 4) as precision,
    round(count(*) filter (where status = 'duplicate' and is_true)
          / nullif(count(*) filter (where is_true), 0), 4)              as recall
from scored
group by method
order by method
