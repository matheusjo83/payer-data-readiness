-- Member data-sharing choices. PAO: the member opted out of the Provider Access
-- API (CMS-0057-F lets members opt out of having their data available to their
-- providers). A term date of '99991231' (still in force) becomes a null end
-- date; otherwise the end date is the day the member opted back in.
select
    cnsnt_id                                                    as consent_id,
    mbr_id                                                      as member_id,
    case cnsnt_typ when 'PAO' then 'provider_access_opt_out' end as consent_type,
    try_strptime(nullif(eff_dt, ''), '%Y%m%d')::date            as effective_date,
    case
        when term_dt = '99991231' then null
        else try_strptime(nullif(term_dt, ''), '%Y%m%d')::date
    end                                                         as end_date,
    upd_ts                                                      as updated_at
from {{ ref('cdc_current__mbr_cnsnt') }} where _op <> 'D'  -- tombstones of deleted keys
