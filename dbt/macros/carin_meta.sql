{#
  FHIR meta for the CARIN Blue Button (STU 2.1.0) resources: the profile the
  resource claims, with its major.minor version as the IG requires, and
  lastUpdated when the legacy row has a timestamp. Legacy timestamps carry no
  time zone; they are exported as UTC.
#}
{% macro carin_meta(profile, updated_at=none) -%}
json_merge_patch('{}', json_object(
    {%- if updated_at is not none %}
    'lastUpdated', strftime({{ updated_at }}, '%Y-%m-%dT%H:%M:%SZ'),
    {%- endif %}
    'profile', json_array({{ carin_profile(profile) }})))
{%- endmacro %}

{% macro carin_profile(profile) -%}
'http://hl7.org/fhir/us/carin-bb/StructureDefinition/{{ profile }}|2.1.0'
{%- endmacro %}
