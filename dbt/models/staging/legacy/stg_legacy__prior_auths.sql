select
    pa_id                     as prior_auth_id,
    mbr_id                    as member_id,
    prv_id                    as provider_id,
    svc_cd                    as service_code,
    urgnt_flg = 'Y'           as is_expedited,
    req_ts                    as requested_at,
    dcsn_ts                   as decided_at,
    case dcsn_cd
        when 'A' then 'approved'
        when 'D' then 'denied'
        when 'P' then 'pending'
    end                       as decision
from {{ legacy_current_state('pa_req', ['pa_id']) }}
