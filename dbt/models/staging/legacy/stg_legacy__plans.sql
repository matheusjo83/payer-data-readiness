-- Health plans with decoded line of business.
select
    pln_id                                                  as plan_id,
    trim(pln_nm)                                            as plan_name,
    case lob_cd
        when 'COM' then 'commercial'
        when 'MCR' then 'medicare_advantage'
        when 'MCD' then 'medicaid'
        when 'CHP' then 'chip'
    end                                                     as line_of_business,
    try_strptime(nullif(eff_dt, ''), '%Y%m%d')::date        as effective_date,
    case
        when term_dt = '99991231' then null
        else try_strptime(nullif(term_dt, ''), '%Y%m%d')::date
    end                                                     as termination_date
from {{ legacy_current_state('pln', ['pln_id']) }}
