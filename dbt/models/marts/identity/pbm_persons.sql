-- PBM cardholders grouped into persons: the connected components of the pairs
-- gold.pbm_duplicate_pairs decided are duplicates. pbm_person_id is the lowest
-- cardholder ID of the person; a cardholder without a duplicate is a person on
-- its own.
with recursive edges as (
    select cardholder_id as a, other_cardholder_id as b
    from {{ ref('pbm_duplicate_pairs') }}
    where status = 'duplicate'
    union all
    select other_cardholder_id, cardholder_id
    from {{ ref('pbm_duplicate_pairs') }}
    where status = 'duplicate'
),

reach(cardholder_id, reached) as (
    select cardholder_id, cardholder_id
    from {{ ref('stg_pbm__members') }}
    union
    select r.cardholder_id, e.b
    from reach r
    join edges e on e.a = r.reached
),

persons as (
    select cardholder_id, min(reached) as pbm_person_id
    from reach
    group by cardholder_id
)

select
    cardholder_id,
    pbm_person_id,
    count(*) over (partition by pbm_person_id) as cardholders
from persons
