-- Fails if an exported ExplanationOfBenefit carries a code that its code system
-- does not have, or that was not usable on the service date (see
-- gold.terminology_code_check). Unverified codes do not fail it.
select *
from {{ ref('terminology_code_check') }}
where status = 'invalid'
