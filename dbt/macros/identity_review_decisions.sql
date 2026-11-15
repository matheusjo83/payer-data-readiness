{#
  The reviewer decisions (seed identity_review_decisions), or none when the var
  identity_apply_review_decisions is false. The decisions name cardholders and
  members of the base data; on data generated at another volume (the scale
  benchmark) the same IDs are other people, so the benchmark measures the model
  without them.
#}
{% macro identity_review_decisions() -%}
    (select * from {{ ref('identity_review_decisions') }}
     {%- if not var('identity_apply_review_decisions', true) %} where false{% endif %})
{%- endmacro %}
