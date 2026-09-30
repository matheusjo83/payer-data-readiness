{#
  True when a 10-digit NPI has a valid check digit: the Luhn algorithm applied
  to the '80840' prefix plus the first 9 digits must produce the 10th digit.
  Null when the value is not 10 digits.
#}
{% macro npi_check_digit_valid(npi) -%}
case when regexp_full_match(coalesce({{ npi }}, ''), '[0-9]{10}') then
    try_cast({{ npi }}[10] as integer) = (10 - list_sum(list_transform(
        -- digits of '80840' + first 9 NPI digits, right to left
        list_transform(string_split(reverse('80840' || {{ npi }}[1:9]), ''), d -> try_cast(d as integer)),
        -- every other digit, starting with the rightmost, is doubled (digit sum)
        (d, i) -> case when i % 2 = 1 then (case when d * 2 > 9 then d * 2 - 9 else d * 2 end) else d end
    )) % 10) % 10
end
{%- endmacro %}
