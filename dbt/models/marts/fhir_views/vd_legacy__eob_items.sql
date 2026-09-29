-- ViewDefinition eob_items (fhir/view_definitions/eob_items.json) over legacy FHIR resources.
select * from {{ vd_eob_items(ref('fhir_explanation_of_benefit')) }}
