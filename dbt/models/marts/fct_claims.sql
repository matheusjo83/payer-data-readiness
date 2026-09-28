-- Gold claims fact: header + aggregated lines, with integrity flags.
with lines as (
    select
        claim_id,
        count(*)            as line_count,
        sum(charged_amount) as lines_charged,
        sum(paid_amount)    as lines_paid
    from {{ ref('stg_legacy__claim_lines') }}
    group by claim_id
)

select
    c.*,
    coalesce(l.line_count, 0)                    as line_count,
    l.lines_charged,
    l.lines_paid,
    m.member_id is not null                      as has_known_member,
    c.total_paid > c.total_charged               as paid_exceeds_charged,
    date_diff('day', c.received_date, c.adjudicated_date) as days_to_adjudicate
from {{ ref('stg_legacy__claims') }} c
left join lines l using (claim_id)
left join {{ ref('stg_legacy__members') }} m using (member_id)
