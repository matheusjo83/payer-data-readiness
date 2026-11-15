-- Legacy claims as FHIR R4 ExplanationOfBenefit resources conforming to the
-- CARIN Blue Button (STU 2.1.0) EOB profile for their claim type: Professional
-- and NonClinician, Inpatient or Outpatient Institutional (from the type of
-- bill) and Pharmacy. Only claims that pass the integrity checks are exported:
-- a known member (EOB.patient) and a coverage span on the service date
-- (EOB.insurance is required). The rest stay in gold.fct_claims, flagged.
-- PBM pharmacy claims are exported too (Pharmacy profile) when identity
-- resolution linked the cardholder to a member and the member had coverage on
-- the fill date; the rest stay in gold.fct_pbm_claims, flagged.
-- Nulls are stripped as described in fhir_patient.sql.
-- Built in batches at large volumes (macros/fhir_explanation_of_benefit.sql):
-- the first one here, the others inserted by the post-hook.
{{ config(post_hook="{{ eob_insert_batches() }}") }}

{{ eob_resources(0, eob_batches()) }}
