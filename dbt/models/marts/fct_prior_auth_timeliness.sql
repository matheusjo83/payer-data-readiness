-- Prior authorization decision time against the CMS-0057-F timeframes
-- (72 hours for expedited requests, 7 calendar days for standard requests).
-- Decision time is the elapsed time between the two timestamps; date_diff
-- would count minute boundaries crossed instead.
select
    prior_auth_id,
    member_id,
    is_expedited,
    requested_at,
    decided_at,
    decision,
    case when is_expedited then 72 else 168 end               as timeframe_hours,
    epoch(decided_at - requested_at) / 3600.0                  as decision_hours,
    case
        when decided_at is null then null
        else epoch(decided_at - requested_at) / 3600.0
             <= case when is_expedited then 72 else 168 end
    end                                                        as within_timeframe
from {{ ref('stg_legacy__prior_auths') }}
