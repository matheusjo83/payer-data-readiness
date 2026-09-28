-- Prior authorization decision time against the CMS-0057-F timeframes
-- (72 hours for expedited requests, 7 calendar days for standard requests).
select
    prior_auth_id,
    member_id,
    is_expedited,
    requested_at,
    decided_at,
    decision,
    case when is_expedited then 72 else 168 end               as timeframe_hours,
    date_diff('minute', requested_at, decided_at) / 60.0       as decision_hours,
    case
        when decided_at is null then null
        else date_diff('minute', requested_at, decided_at) / 60.0
             <= case when is_expedited then 72 else 168 end
    end                                                        as within_timeframe
from {{ ref('stg_legacy__prior_auths') }}
