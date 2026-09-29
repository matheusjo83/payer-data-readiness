-- ViewDefinition eob_summary (fhir/view_definitions/eob_summary.json) over legacy FHIR resources.
select * from {{ vd_eob_summary(ref('fhir_explanation_of_benefit')) }}
