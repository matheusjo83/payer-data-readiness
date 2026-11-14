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
    end                       as decision,
    req_amt::decimal(12, 2)   as requested_amount,
    dx_cd                     as diagnosis_code,
    pos_cd                    as place_of_service_code,
    strptime(svc_dt, '%Y%m%d')::date as service_date,
    svc_qty                   as service_quantity,
    case src_cd
        when 'L' then 'legacy'
        when 'F' then 'pas'
    end                       as channel,
    sbmsn_id                  as submission_id,
    rqstr_npi                 as requester_npi,
    upd_ts                    as updated_at
from {{ ref('cdc_current__pa_req') }} where _op <> 'D'  -- tombstones of deleted keys
