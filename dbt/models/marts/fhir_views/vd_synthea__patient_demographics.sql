-- ViewDefinition patient_demographics (fhir/view_definitions/patient_demographics.json) over synthea FHIR resources.
select * from {{ vd_patient_demographics(source('fhir', 'patient')) }}
