-- Coverage spans: parses legacy VARCHAR dates; '99991231' means open-ended
-- coverage and becomes a null end date.
select
    elig_sk                                                     as eligibility_id,
    mbr_id                                                      as member_id,
    pln_id                                                      as plan_id,
    try_strptime(nullif(eff_dt, ''), '%Y%m%d')::date            as coverage_start_date,
    case
        when term_dt = '99991231' then null
        else try_strptime(nullif(term_dt, ''), '%Y%m%d')::date
    end                                                         as coverage_end_date,
    upd_ts                                                      as updated_at
from {{ legacy_current_state('elig_span', ['elig_sk']) }}
