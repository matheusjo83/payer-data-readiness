-- Data-quality scorecard: how many known legacy issues the lakehouse detects.
-- Compare these counts with the injection rates in generate_legacy_data.py.
select 'duplicate_member_ids' as check_name,
       count(*) filter (where had_duplicates) as issue_count,
       count(*) as population
from {{ ref('stg_legacy__members') }}
union all
select 'invalid_gender', count(*) filter (where gender = 'invalid'), count(*)
from {{ ref('stg_legacy__members') }}
union all
select 'unparseable_birth_date', count(*) filter (where birth_date is null), count(*)
from {{ ref('stg_legacy__members') }}
union all
select 'claims_unknown_member', count(*) filter (where not has_known_member), count(*)
from {{ ref('fct_claims') }}
union all
select 'claims_paid_exceeds_charged', count(*) filter (where paid_exceeds_charged), count(*)
from {{ ref('fct_claims') }}
union all
select 'orphan_claim_lines',
       count(*) filter (where c.claim_id is null),
       count(*)
from {{ ref('stg_legacy__claim_lines') }} l
left join {{ ref('stg_legacy__claims') }} c on l.claim_id = c.claim_id
