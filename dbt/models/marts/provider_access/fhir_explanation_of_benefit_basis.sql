-- The claims of fhir_explanation_of_benefit without financial data, for the
-- Provider Access and Payer-to-Payer APIs (CMS-0057-F: claims "without provider
-- remittances and enrollee cost-sharing information"). Each one conforms to the
-- CARIN Blue Button (STU 2.1.0) Basis profile of its claim type, which the IG
-- describes as the full profile with only the financial requirements removed,
-- and which Da Vinci PDex names for these APIs. Derived from the full resource
-- rather than mapped again, so the two cannot drift apart:
--
--   removed    total, payment, item.net, and every adjudication that carries an
--              amount (submitted, benefit, paid by patient), in the items and in
--              the header; an item left without adjudication loses the element.
--   kept       everything else, including the benefit payment status (in or out
--              of network), which is not an amount and which the Professional
--              Basis profile requires on every item.
--   changed    meta.profile (the Basis profile) and the narrative, which no
--              longer states amounts.
--
-- Same IDs as the full resources: they are published to a separate FHIR server
-- (the Provider Access one), so the Patient Access server keeps the full ones.
with eob as (
    select id, resource::json as r
    from {{ ref('fhir_explanation_of_benefit') }}
),

projected as (
    select
        id,
        r,
        list_transform(
            cast(json_extract(r, '$.item[*]') as json[]),
            i -> json_merge_patch(i, json_object(
                'net', null,
                'adjudication', case when len(list_filter(
                        cast(json_extract(i, '$.adjudication[*]') as json[]),
                        a -> not json_exists(a, '$.amount'))) > 0
                    then to_json(list_filter(
                        cast(json_extract(i, '$.adjudication[*]') as json[]),
                        a -> not json_exists(a, '$.amount'))) end))
        ) as items,
        list_filter(
            coalesce(cast(json_extract(r, '$.adjudication[*]') as json[]), []),
            a -> not json_exists(a, '$.amount')) as adjudication
    from eob
)

select
    id,
    json_merge_patch(r, json_object(
        'meta', json_object(
            'profile', json_array(replace(r->>'$.meta.profile[0]', '|2.1.0', '-Basis|2.1.0'))),
        'text', {{ fhir_narrative("concat_ws(', ',
            upper((r->>'$.type.coding[0].code')[1]) || (r->>'$.type.coding[0].code')[2:] || ' claim ' || id,
            'member ' || replace(r->>'$.patient.reference', 'Patient/', ''),
            'service ' || (r->>'$.billablePeriod.start') || ' to ' || (r->>'$.billablePeriod.end'),
            'no financial data')") }},
        'total', null,
        'payment', null,
        'item', to_json(items),
        'adjudication', case when len(adjudication) > 0 then to_json(adjudication) end
    )) as resource
from projected
