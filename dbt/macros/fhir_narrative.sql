{#
  FHIR narrative (Resource.text) generated from the resource's own data, so the
  dom-6 best practice ("a resource should have narrative") holds, as in the
  CARIN Blue Button examples. `summary` is a SQL expression for one plain-text
  sentence; XHTML special characters are escaped. Nothing in the narrative is
  absent from the structured data (status `generated`).
#}
{% macro fhir_narrative(summary) -%}
json_object(
    'status', 'generated',
    'div', '<div xmlns="http://www.w3.org/1999/xhtml"><p>'
        || replace(replace(replace({{ summary }}, '&', '&amp;'), '<', '&lt;'), '>', '&gt;')
        || '</p></div>')
{%- endmacro %}
