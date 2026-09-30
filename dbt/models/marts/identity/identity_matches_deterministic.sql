-- Baseline: deterministic matching of PBM cardholders to legacy members, the
-- kind of exact rules a first master patient index often starts with. A pair is
-- a candidate when it agrees exactly on one of three field sets (first names
-- compared after nickname normalization, as in the Splink model); a cardholder
-- is linked only when all its candidates point to a single member.
{% set rules = {
    'first_last_dob': ['first_name_canonical', 'last_name', 'birth_date'],
    'last_dob_zip':   ['last_name', 'birth_date', 'zip_code'],
    'first_dob_zip':  ['first_name_canonical', 'birth_date', 'zip_code'],
} %}
with legacy as (
    select * from {{ ref('identity_people') }} where source_dataset = 'legacy'
),

pbm as (
    select * from {{ ref('identity_people') }} where source_dataset = 'pbm'
),

candidates as (
    {% for name, cols in rules.items() %}
    select p.unique_id as cardholder_id, l.unique_id as member_id, '{{ name }}' as rule
    from pbm p
    join legacy l on {% for c in cols %}p.{{ c }} = l.{{ c }}{% if not loop.last %} and {% endif %}{% endfor %}
    {% if not loop.last %}union all{% endif %}
    {% endfor %}
)

select
    cardholder_id,
    case when count(distinct member_id) = 1 then min(member_id) end    as member_id,
    count(distinct member_id)                                           as candidate_members,
    list(distinct rule order by rule)                                   as rules,
    case when count(distinct member_id) = 1 then 'linked' else 'ambiguous' end as status
from candidates
group by cardholder_id
