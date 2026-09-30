{# A member identifier (type MB, the CARIN Patient memberid slice) for FHIR JSON. #}
{% macro member_identifier(system, value) -%}
json_object(
    'type', json_object('coding', json_array(json_object(
        'system', 'http://terminology.hl7.org/CodeSystem/v2-0203',
        'code', 'MB'))),
    'system', {{ system }},
    'value', {{ value }})
{%- endmacro %}
