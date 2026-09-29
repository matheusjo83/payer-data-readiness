-- Every ViewDefinition must reproduce its reference table exactly.
select *
from {{ ref('fhir_view_parity') }}
where only_in_view > 0 or only_in_reference > 0
