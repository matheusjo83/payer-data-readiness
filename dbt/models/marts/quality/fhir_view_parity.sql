-- FHIR view parity: each comparison lines up a ViewDefinition output with the
-- table it should reproduce and counts the rows found on only one side
-- (EXCEPT ALL in both directions, so duplicates count too).
--
--   synthea_patients  ViewDefinition over Synthea Patient vs. the hand-written stg_fhir__patients
--   legacy_*          round trip: legacy gold table -> FHIR resource -> ViewDefinition -> back
{% set comparisons = {
    'synthea_patients': {
        'view': "select patient_id, gender, try_cast(birth_date as date) as birth_date, state, postal_code
                 from " ~ ref('vd_synthea__patient_demographics'),
        'reference': "select patient_id, gender, birth_date, state, postal_code
                      from " ~ ref('stg_fhir__patients'),
    },
    'legacy_patients': {
        'view': "select patient_id, gender, try_cast(birth_date as date) as birth_date, state, postal_code
                 from " ~ ref('vd_legacy__patient_demographics'),
        'reference': "select member_id, case when gender in ('male', 'female', 'unknown') then gender end,
                             birth_date, state, zip_code
                      from " ~ ref('dim_member'),
    },
    'legacy_coverage': {
        'view': "select coverage_id, patient_id, plan_id, plan_name,
                        try_cast(period_start as date), try_cast(period_end as date)
                 from " ~ ref('vd_legacy__coverage_summary'),
        'reference': "select 'cov-' || e.eligibility_id, e.member_id, e.plan_id, p.plan_name,
                             e.coverage_start_date, e.coverage_end_date
                      from " ~ ref('stg_legacy__eligibility') ~ " e
                      left join " ~ ref('stg_legacy__plans') ~ " p using (plan_id)",
    },
    'legacy_claims': {
        'view': "select eob_id, patient_id, claim_type, item_count,
                        cast(submitted_amount as decimal(12, 2)), cast(paid_amount as decimal(12, 2))
                 from " ~ ref('vd_legacy__eob_summary'),
        'reference': "select claim_id, member_id, claim_type, line_count, total_charged, total_paid
                      from " ~ ref('fct_claims') ~ "
                      where has_known_member and is_within_eligibility",
    },
    'legacy_claim_lines': {
        'view': "select eob_id, item_sequence, product_code, try_cast(serviced_start as date),
                        cast(net_amount as decimal(12, 2))
                 from " ~ ref('vd_legacy__eob_items'),
        'reference': "select l.claim_id, l.line_number, l.procedure_code, c.service_from_date, l.charged_amount
                      from " ~ ref('stg_legacy__claim_lines') ~ " l
                      join " ~ ref('fct_claims') ~ " c using (claim_id)
                      where c.has_known_member and c.is_within_eligibility",
    },
} %}

{% for name, q in comparisons.items() %}
select
    '{{ name }}'                                                                   as comparison,
    (select count(*) from ({{ q.view }}))                                          as view_rows,
    (select count(*) from ({{ q.reference }}))                                     as reference_rows,
    (select count(*) from (({{ q.view }}) except all ({{ q.reference }})))         as only_in_view,
    (select count(*) from (({{ q.reference }}) except all ({{ q.view }})))         as only_in_reference
{% if not loop.last %}union all{% endif %}
{% endfor %}
