-- The Basis ExplanationOfBenefit resources (Provider Access and Payer-to-Payer
-- APIs) carry no financial data: no amount anywhere in the resource, and no
-- total or payment. Each one also has a full resource with the same ID, and
-- apart from the removed elements, the profile and the narrative it is equal to it
-- (checked on the number of items, diagnoses and the claim's identifiers).
with basis as (
    select b.id, b.resource::json as b, f.resource::json as f
    from {{ ref('fhir_explanation_of_benefit_basis') }} b
    left join {{ ref('fhir_explanation_of_benefit') }} f using (id)
)

select id, 'missing full resource' as problem from basis where f is null
union all
select id, 'amount in the Basis resource' from basis where contains(b::varchar, '"amount"')
union all
select id, 'total or payment' from basis where json_exists(b, '$.total') or json_exists(b, '$.payment')
union all
select id, 'net on an item' from basis where contains(json_extract(b, '$.item')::varchar, '"net"')
union all
select id, 'not a Basis profile' from basis where not (b->>'$.meta.profile[0]') like '%-Basis|2.1.0'
union all
select id, 'items or diagnoses differ' from basis
where json_array_length(b, '$.item') <> json_array_length(f, '$.item')
   or coalesce(json_array_length(b, '$.diagnosis'), 0) <> coalesce(json_array_length(f, '$.diagnosis'), 0)
   or json_extract(b, '$.identifier') <> json_extract(f, '$.identifier')
   or json_extract(b, '$.patient') <> json_extract(f, '$.patient')
   or json_extract(b, '$.insurance') <> json_extract(f, '$.insurance')
