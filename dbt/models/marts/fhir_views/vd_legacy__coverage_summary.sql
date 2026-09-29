-- ViewDefinition coverage_summary (fhir/view_definitions/coverage_summary.json) over legacy FHIR resources.
select * from {{ vd_coverage_summary(ref('fhir_coverage')) }}
