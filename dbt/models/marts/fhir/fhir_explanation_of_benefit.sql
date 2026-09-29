-- Legacy claims as FHIR R4 ExplanationOfBenefit resources, aligned with the
-- CARIN Blue Button EOB profiles. Only claims that pass the integrity checks
-- are exported: a known member (EOB.patient) and a coverage span on the service
-- date (EOB.insurance is required). The rest stay in gold.fct_claims, flagged.
-- Nulls are stripped as described in fhir_patient.sql.
with exported as (
    select *
    from {{ ref('fct_claims') }}
    where has_known_member
      and is_within_eligibility
),

lines as (
    select
        l.claim_id,
        list(json_merge_patch('{}', json_object(
            'sequence', l.line_number,
            'diagnosisSequence', json_array(l.line_number),
            'productOrService', json_object('coding', json_array(json_merge_patch('{}', json_object(
                'system', 'http://www.ama-assn.org/go/cpt',
                'code', l.procedure_code)))),
            'servicedDate', strftime(c.service_from_date, '%Y-%m-%d'),
            'quantity', json_object('value', l.units),
            'net', json_object('value', l.charged_amount, 'currency', 'USD'),
            'adjudication', json_array(
                json_object(
                    'category', json_object('coding', json_array(json_object(
                        'system', 'http://terminology.hl7.org/CodeSystem/adjudication',
                        'code', 'submitted'))),
                    'amount', json_object('value', l.charged_amount, 'currency', 'USD')),
                json_object(
                    'category', json_object('coding', json_array(json_object(
                        'system', 'http://terminology.hl7.org/CodeSystem/adjudication',
                        'code', 'benefit'))),
                    'amount', json_object('value', coalesce(l.paid_amount, 0), 'currency', 'USD')))
        )) order by l.line_number) as items,
        list(json_merge_patch('{}', json_object(
            'sequence', l.line_number,
            'diagnosisCodeableConcept', json_object('coding', json_array(json_merge_patch('{}', json_object(
                'system', 'http://hl7.org/fhir/sid/icd-10-cm',
                'code', l.diagnosis_code)))))
        ) order by l.line_number) as diagnoses
    from {{ ref('stg_legacy__claim_lines') }} l
    join exported c using (claim_id)
    group by l.claim_id
)

select
    c.claim_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'ExplanationOfBenefit',
        'id', c.claim_id,
        'identifier', json_array(json_object(
            'system', 'https://payer-data-readiness.example/claim-id',
            'value', c.claim_id)),
        'status', case when c.claim_status = 'void' then 'cancelled' else 'active' end,
        'type', json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/claim-type',
            'code', c.claim_type))),
        'use', 'claim',
        'patient', json_object('reference', 'Patient/' || c.member_id),
        'billablePeriod', json_object(
            'start', strftime(c.service_from_date, '%Y-%m-%d'),
            'end', strftime(c.service_to_date, '%Y-%m-%d')),
        'created', strftime(coalesce(c.adjudicated_date, c.received_date), '%Y-%m-%d'),
        'insurer', json_object('display', 'Payer Data Readiness (synthetic)'),
        'provider', json_object('reference', 'Organization/' || c.provider_id),
        'outcome', case when c.claim_status = 'pending' then 'queued' else 'complete' end,
        'insurance', json_array(json_object(
            'focal', true,
            'coverage', json_object('reference', 'Coverage/cov-' || c.eligibility_id))),
        'diagnosis', l.diagnoses,
        'item', l.items,
        'total', json_array(json_object(
            'category', json_object('coding', json_array(json_object(
                'system', 'http://terminology.hl7.org/CodeSystem/adjudication',
                'code', 'submitted'))),
            'amount', json_object('value', c.total_charged, 'currency', 'USD'))),
        'payment', json_object('amount', json_object('value', c.total_paid, 'currency', 'USD'))
    )) as resource
from exported c
left join lines l using (claim_id)
