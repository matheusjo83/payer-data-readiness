-- FDA NDC Directory packages, keyed by the 11-digit NDC (5-4-2) that claims and
-- FHIR use. The FDA publishes 10-digit codes in three layouts (4-4-2, 5-3-2,
-- 5-4-1); each segment is padded with a leading zero to its 11-digit width.
-- The package file holds the listed products only (excluded ones are in a
-- separate FDA file, not loaded).
with packages as (
    select
        string_split(NDCPACKAGECODE, '-') as parts,
        NDCPACKAGECODE                    as package_code,
        PACKAGEDESCRIPTION                as description,
        try_strptime(STARTMARKETINGDATE, '%Y%m%d')::date as marketing_start_date,
        try_strptime(ENDMARKETINGDATE, '%Y%m%d')::date   as marketing_end_date
    from {{ source('terminology', 'ndc_package') }}
)

select
    lpad(parts[1], 5, '0') || lpad(parts[2], 4, '0') || lpad(parts[3], 2, '0') as ndc,
    package_code,
    description,
    marketing_start_date,
    marketing_end_date
from packages
-- A package code appears once per listing; keep the longest marketed listing.
qualify row_number() over (
    partition by ndc
    order by marketing_end_date desc nulls first, marketing_start_date
) = 1
