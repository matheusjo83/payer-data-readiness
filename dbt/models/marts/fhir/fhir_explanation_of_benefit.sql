-- Legacy claims as FHIR R4 ExplanationOfBenefit resources conforming to the
-- CARIN Blue Button (STU 2.1.0) EOB profile for their claim type: Professional
-- and NonClinician, Inpatient or Outpatient Institutional (from the type of
-- bill) and Pharmacy. Only claims that pass the integrity checks are exported:
-- a known member (EOB.patient) and a coverage span on the service date
-- (EOB.insurance is required). The rest stay in gold.fct_claims, flagged.
-- Nulls are stripped as described in fhir_patient.sql.
{% set c4bb = 'http://hl7.org/fhir/us/carin-bb/CodeSystem/' %}
with exported as (
    select
        *,
        case
            when claim_type = 'professional' then {{ carin_profile('C4BB-ExplanationOfBenefit-Professional-NonClinician') }}
            when claim_type = 'pharmacy' then {{ carin_profile('C4BB-ExplanationOfBenefit-Pharmacy') }}
            when institutional_setting = 'inpatient' then {{ carin_profile('C4BB-ExplanationOfBenefit-Inpatient-Institutional') }}
            else {{ carin_profile('C4BB-ExplanationOfBenefit-Outpatient-Institutional') }}
        end as profile,
        -- Payer benefit payment status: the network status the claim was paid under.
        json_object(
            'category', json_object('coding', json_array(json_object(
                'system', '{{ c4bb }}C4BBAdjudicationDiscriminator',
                'code', 'benefitpaymentstatus'))),
            'reason', json_object('coding', json_array(json_object(
                'system', '{{ c4bb }}C4BBPayerAdjudicationStatus',
                'code', case network_status when 'in_network' then 'innetwork' else 'outofnetwork' end)))
        ) as benefit_payment_status
    from {{ ref('fct_claims') }}
    where has_known_member
      and is_within_eligibility
),

line_rows as (
    select
        l.*,
        c.claim_type,
        c.place_of_service_code,
        c.service_from_date,
        c.benefit_payment_status,
        -- The first line's diagnosis is the principal one.
        row_number() over (partition by l.claim_id order by l.line_number) = 1 as is_first_line,
        json_object(
            'category', json_object('coding', json_array(json_object(
                'system', 'http://terminology.hl7.org/CodeSystem/adjudication',
                'code', 'submitted'))),
            'amount', json_object('value', l.charged_amount, 'currency', 'USD')) as submitted,
        json_object(
            'category', json_object('coding', json_array(json_object(
                'system', 'http://terminology.hl7.org/CodeSystem/adjudication',
                'code', 'benefit'))),
            'amount', json_object('value', coalesce(l.paid_amount, 0), 'currency', 'USD')) as benefit
    from {{ ref('stg_legacy__claim_lines') }} l
    join exported c using (claim_id)
),

lines as (
    select
        claim_id,
        list(json_merge_patch('{}', json_object(
            'sequence', line_number,
            'diagnosisSequence', json_array(line_number),
            'revenue', case when revenue_code is not null then json_object('coding', json_array(json_object(
                'system', 'https://www.nubc.org/CodeSystem/RevenueCodes',
                'code', revenue_code))) end,
            'productOrService', json_object('coding', json_array(json_merge_patch('{}',
                case when claim_type = 'pharmacy'
                     then json_object('system', 'http://hl7.org/fhir/sid/ndc', 'code', ndc_code)
                     else json_object('system', 'http://www.ama-assn.org/go/cpt', 'code', procedure_code)
                end))),
            'servicedDate', strftime(service_from_date, '%Y-%m-%d'),
            'locationCodeableConcept', case when place_of_service_code is not null then
                json_object('coding', json_array(json_object(
                    'system', 'https://www.cms.gov/Medicare/Coding/place-of-service-codes/Place_of_Service_Code_Set',
                    'code', place_of_service_code))) end,
            'quantity', json_object('value', units),
            'net', json_object('value', charged_amount, 'currency', 'USD'),
            -- Professional claims carry the payment status per line, the others per claim.
            'adjudication', case when claim_type = 'professional'
                then json_array(submitted, benefit, benefit_payment_status)
                else json_array(submitted, benefit) end
        )) order by line_number) as items,
        list(json_merge_patch('{}', json_object(
            'sequence', line_number,
            'diagnosisCodeableConcept', json_object('coding', json_array(json_merge_patch('{}', json_object(
                'system', 'http://hl7.org/fhir/sid/icd-10-cm',
                'code', diagnosis_code)))),
            'type', case when claim_type <> 'pharmacy' then json_array(json_object('coding', json_array(
                case
                    when is_first_line then json_object(
                        'system', 'http://terminology.hl7.org/CodeSystem/ex-diagnosistype', 'code', 'principal')
                    else json_object(
                        'system', '{{ c4bb }}C4BBClaimDiagnosisType',
                        'code', case when claim_type = 'professional' then 'secondary' else 'other' end)
                end))) end)
        ) order by line_number) as diagnoses
    from line_rows
    group by claim_id
),

supporting_info as (
    select
        claim_id,
        case
            when claim_type = 'pharmacy' then json_array(
                json_object('sequence', 1, 'category', json_object('coding', json_array(json_object(
                        'system', '{{ c4bb }}C4BBSupportingInfoType', 'code', 'dayssupply'))),
                    'valueQuantity', json_object('value', days_supply)),
                json_object('sequence', 2, 'category', json_object('coding', json_array(json_object(
                        'system', '{{ c4bb }}C4BBSupportingInfoType', 'code', 'dawcode'))),
                    'code', json_object('coding', json_array(json_object(
                        'system', 'http://terminology.hl7.org/CodeSystem/NCPDPDispensedAsWrittenOrProductSelectionCode',
                        'code', dispense_as_written_code)))),
                json_object('sequence', 3, 'category', json_object('coding', json_array(json_object(
                        'system', '{{ c4bb }}C4BBSupportingInfoType', 'code', 'refillnum'))),
                    'valueQuantity', json_object('value', refill_number)),
                json_object('sequence', 4, 'category', json_object('coding', json_array(json_object(
                        'system', '{{ c4bb }}C4BBSupportingInfoType', 'code', 'refillsauthorized'))),
                    'valueQuantity', json_object('value', refills_authorized)))
            when institutional_setting = 'inpatient' then json_array(
                json_object('sequence', 1, 'category', json_object('coding', json_array(json_object(
                        'system', '{{ c4bb }}C4BBSupportingInfoType', 'code', 'admissionperiod'))),
                    'timingPeriod', json_object(
                        'start', strftime(service_from_date, '%Y-%m-%d'),
                        'end', strftime(service_to_date, '%Y-%m-%d'))))
        end as supporting_info
    from exported
)

select
    c.claim_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'ExplanationOfBenefit',
        'id', c.claim_id,
        'meta', json_object(
            'lastUpdated', strftime(c.updated_at, '%Y-%m-%dT%H:%M:%SZ'),
            'profile', json_array(c.profile)),
        'identifier', json_array(json_object(
            'type', json_object('coding', json_array(json_object(
                'system', '{{ c4bb }}C4BBIdentifierType',
                'code', 'uc'))),
            'system', 'https://payer-data-readiness.example/claim-id',
            'value', c.claim_id)),
        'status', case when c.claim_status = 'void' then 'cancelled' else 'active' end,
        'type', json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/claim-type',
            'code', c.claim_type))),
        'subType', case when c.institutional_setting is not null then json_object('coding', json_array(json_object(
            'system', '{{ c4bb }}C4BBInstitutionalClaimSubType',
            'code', c.institutional_setting))) end,
        'use', 'claim',
        'patient', json_object('reference', 'Patient/' || c.member_id),
        'billablePeriod', json_object(
            'start', strftime(c.service_from_date, '%Y-%m-%d'),
            'end', strftime(c.service_to_date, '%Y-%m-%d')),
        'created', strftime(coalesce(c.adjudicated_date, c.received_date), '%Y-%m-%d'),
        'insurer', json_object('reference', 'Organization/payer'),
        'provider', json_object('reference', 'Organization/' || c.provider_id),
        'outcome', case when c.claim_status = 'pending' then 'queued' else 'complete' end,
        'supportingInfo', s.supporting_info,
        'insurance', json_array(json_object(
            'focal', true,
            'coverage', json_object('reference', 'Coverage/cov-' || c.eligibility_id))),
        'diagnosis', l.diagnoses,
        'item', l.items,
        'adjudication', case when c.claim_type <> 'professional' then json_array(c.benefit_payment_status) end,
        'total', json_array(json_object(
            'category', json_object('coding', json_array(json_object(
                'system', 'http://terminology.hl7.org/CodeSystem/adjudication',
                'code', 'submitted'))),
            'amount', json_object('value', c.total_charged, 'currency', 'USD'))),
        'payment', json_object('amount', json_object('value', c.total_paid, 'currency', 'USD'))
    )) as resource
from exported c
left join lines l using (claim_id)
left join supporting_info s using (claim_id)
