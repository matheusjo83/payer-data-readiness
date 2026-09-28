{# Shared column descriptions, reused with {{ doc('...') }} across models. #}

{% docs member_id %}
Legacy member identifier (`mbr_id`). Not unique in the source member master, which contains duplicates.
{% enddocs %}

{% docs provider_id %}
Legacy provider identifier (`prv_id`).
{% enddocs %}

{% docs plan_id %}
Legacy health plan identifier (`pln_id`).
{% enddocs %}

{% docs claim_id %}
Legacy claim identifier (`clm_id`).
{% enddocs %}

{% docs prior_auth_id %}
Legacy prior authorization identifier (`pa_id`).
{% enddocs %}

{% docs line_of_business %}
Decoded line of business: commercial, medicare_advantage, medicaid or chip.
{% enddocs %}

{% docs updated_at %}
Last update timestamp in the legacy source (`upd_ts`).
{% enddocs %}

{% docs coverage_start_date %}
First day of coverage, parsed from the legacy `YYYYMMDD` string.
{% enddocs %}

{% docs coverage_end_date %}
Last day of coverage. Null when coverage is open-ended (`99991231` in the legacy source).
{% enddocs %}

{% docs fhir_loaded_at %}
Time the FHIR resource was loaded into bronze.
{% enddocs %}
