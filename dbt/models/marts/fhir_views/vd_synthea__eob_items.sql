-- ViewDefinition eob_items (fhir/view_definitions/eob_items.json) over synthea FHIR resources.
select * from {{ vd_eob_items(source('fhir', 'explanation_of_benefit')) }}
