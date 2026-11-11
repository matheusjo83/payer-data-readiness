-- ViewDefinition prior_auth_summary (fhir/view_definitions/prior_auth_summary.json) over the PDex prior authorizations.
select * from {{ vd_prior_auth_summary(ref('fhir_prior_authorization')) }}
