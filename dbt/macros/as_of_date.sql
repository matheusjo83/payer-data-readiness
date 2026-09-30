-- The reference date used wherever a model needs "today" (see dbt_project.yml).
{% macro as_of_date() -%}
    date '{{ var("as_of_date") }}'
{%- endmacro %}
