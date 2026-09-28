select
    clm_id   as claim_id,
    ln_nbr   as line_number,
    proc_cd  as procedure_code,
    dx_cd    as diagnosis_code,
    units,
    chrg_amt as charged_amount,
    pd_amt   as paid_amount,
    upd_ts   as updated_at
from {{ legacy_current_state('clm_ln', ['clm_id', 'ln_nbr']) }}
