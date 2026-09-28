-- Every captured change has a unique position in the log: (_lsn, _seq).
-- Rows written by one bulk WAL record (e.g. COPY) share an _lsn and are told
-- apart by _seq. A duplicate means a batch was applied twice.
{% set tables = ['pln', 'mbr_mstr', 'elig_span', 'prv', 'clm_hdr', 'clm_ln', 'pa_req'] %}

with changes as (
    {% for t in tables %}
    select '{{ t }}' as source_table, _lsn, _seq
    from {{ source('legacy', t) }}
    where _op <> 'S'
    {% if not loop.last %}union all{% endif %}
    {% endfor %}
)

select source_table, _lsn, _seq, count(*) as occurrences
from changes
group by source_table, _lsn, _seq
having count(*) > 1
