-- ViewDefinition patient_demographics (fhir/view_definitions/patient_demographics.json) over legacy FHIR resources.
select * from {{ vd_patient_demographics(ref('fhir_patient')) }}
