select
    pa_id    as prior_auth_id,
    qnr_url  as questionnaire,
    item_id,
    ans_txt  as answer,
    case ans_src
        when 'A' then 'auto'
        when 'O' then 'override'
        when 'M' then 'manual'
    end      as answer_source,
    upd_ts   as updated_at
from {{ ref('cdc_current__pa_doc') }} where _op <> 'D'  -- tombstones of deleted keys
