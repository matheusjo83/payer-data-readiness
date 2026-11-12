-- Provider Access API opt-outs as FHIR Consent resources conforming to the Da
-- Vinci PDex Provider Access Consent profile (STU 2.1.0): provision deny, action
-- disclose, from the member (performer) to the payer's providers. Every opt-out
-- is exported, including ones that ended when the member opted back in; the
-- period says when it was in force (the end is the last day, FHIR periods are
-- inclusive). The API checks these at the time it runs an export, so an opt-out
-- recorded after the attribution list was built still applies. The profile fixes
-- status (active) and policyRule (cric).
select
    'optout-' || c.consent_id as id,
    c.member_id,
    c.effective_date,
    c.end_date,
    json_merge_patch('{}', json_object(
        'resourceType', 'Consent',
        'id', 'optout-' || c.consent_id,
        'meta', json_object(
            'lastUpdated', strftime(c.updated_at, '%Y-%m-%dT%H:%M:%SZ'),
            'profile', json_array('http://hl7.org/fhir/us/davinci-pdex/StructureDefinition/pdex-provider-consent')),
        'text', {{ fhir_narrative("concat_ws(', ', 'Member ' || c.member_id || ' opted out of the Provider Access API',
            'from ' || strftime(c.effective_date, '%Y-%m-%d'),
            'until ' || strftime(c.end_date - interval 1 day, '%Y-%m-%d'))") }},
        'status', 'active',
        'scope', json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/consentscope', 'code', 'patient-privacy'))),
        'category', json_array(json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/v3-ActCode', 'code', 'IDSCL')))),
        'patient', json_object('reference', 'Patient/' || c.member_id),
        'dateTime', strftime(c.updated_at, '%Y-%m-%dT%H:%M:%SZ'),
        'performer', json_array(json_object('reference', 'Patient/' || c.member_id)),
        'organization', json_array(json_object('reference', 'Organization/payer')),
        'policyRule', json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/consentpolicycodes',
            'code', 'cric',
            'display', 'Common Rule Informed Consent'))),
        'provision', json_object(
            'type', 'deny',
            'period', json_merge_patch('{}', json_object(
                'start', strftime(c.effective_date, '%Y-%m-%d'),
                'end', strftime(c.end_date - interval 1 day, '%Y-%m-%d'))),
            'actor', json_array(json_object(
                'role', json_object('coding', json_array(json_object(
                    'system', 'http://terminology.hl7.org/CodeSystem/provenance-participant-type',
                    'code', 'performer'))),
                'reference', json_object('reference', 'Organization/payer'))),
            'action', json_array(json_object('coding', json_array(json_object(
                'system', 'http://terminology.hl7.org/CodeSystem/consentaction', 'code', 'disclose')))))
    )) as resource
from {{ ref('stg_legacy__consents') }} c
join {{ ref('dim_member') }} m using (member_id)
where c.consent_type = 'provider_access_opt_out'
