-- Every captured change has its own log position, so (table, _lsn) must be
-- unique outside the snapshot. A duplicate means a batch was applied twice.
{% set tables = ['pln', 'mbr_mstr', 'elig_span', 'prv', 'clm_hdr', 'clm_ln', 'pa_req'] %}

with changes as (
    {% for t in tables %}
    select '{{ t }}' as source_table, _lsn
    from {{ source('legacy', t) }}
    where _op <> 'S'
    {% if not loop.last %}union all{% endif %}
    {% endfor %}
)

select source_table, _lsn, count(*) as occurrences
from changes
group by source_table, _lsn
having count(*) > 1
