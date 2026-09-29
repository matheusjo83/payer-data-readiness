-- Legacy coverage spans as FHIR R4 Coverage resources, aligned with the CARIN
-- Blue Button Coverage profile (beneficiary, relationship, period, payor, plan
-- class). Nulls are stripped as described in fhir_patient.sql.
select
    'cov-' || e.eligibility_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'Coverage',
        'id', 'cov-' || e.eligibility_id,
        'status', 'active',
        'type', json_object('text', p.line_of_business),
        'subscriberId', e.member_id,
        'beneficiary', json_object('reference', 'Patient/' || e.member_id),
        'relationship', json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/subscriber-relationship',
            'code', 'self'))),
        'period', json_object(
            'start', strftime(e.coverage_start_date, '%Y-%m-%d'),
            'end', strftime(e.coverage_end_date, '%Y-%m-%d')),
        'payor', json_array(json_object('display', 'Payer Data Readiness (synthetic)')),
        'class', json_array(json_merge_patch('{}', json_object(
            'type', json_object('coding', json_array(json_object(
                'system', 'http://terminology.hl7.org/CodeSystem/coverage-class',
                'code', 'plan'))),
            'value', e.plan_id,
            'name', p.plan_name)))
    )) as resource
from {{ ref('stg_legacy__eligibility') }} e
left join {{ ref('stg_legacy__plans') }} p using (plan_id)
