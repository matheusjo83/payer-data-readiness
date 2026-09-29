-- ViewDefinition eob_summary (fhir/view_definitions/eob_summary.json) over synthea FHIR resources.
select * from {{ vd_eob_summary(source('fhir', 'explanation_of_benefit')) }}
