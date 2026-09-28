select
    clm_id                                             as claim_id,
    mbr_id                                             as member_id,
    prv_id                                             as provider_id,
    case clm_typ_cd
        when 'P' then 'professional'
        when 'I' then 'institutional'
        when 'R' then 'pharmacy'
    end                                                as claim_type,
    try_strptime(nullif(svc_from_dt, ''), '%Y%m%d')::date as service_from_date,
    try_strptime(nullif(svc_to_dt, ''), '%Y%m%d')::date   as service_to_date,
    try_strptime(nullif(rcvd_dt, ''), '%Y%m%d')::date     as received_date,
    try_strptime(nullif(adj_dt, ''), '%Y%m%d')::date      as adjudicated_date,
    case clm_stat_cd
        when 'PD' then 'paid'
        when 'DN' then 'denied'
        when 'PN' then 'pending'
        when 'VD' then 'void'
    end                                                as claim_status,
    tot_chrg_amt                                       as total_charged,
    tot_pd_amt                                         as total_paid,
    upd_ts                                             as updated_at
from {{ source('legacy', 'clm_hdr') }}
