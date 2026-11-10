-- ICD-10-CM, one row per code and fiscal year. The FHIR code is the CMS code with
-- a dot after the third character. A fiscal year runs from October 1 of the
-- previous year to September 30; only billable codes are valid on a claim.
select
    fiscal_year,
    case when length(code) > 3 then substr(code, 1, 3) || '.' || substr(code, 4) else code end as code,
    is_billable,
    make_date(fiscal_year - 1, 10, 1) as valid_from,
    make_date(fiscal_year, 9, 30)     as valid_to,
    long_description                  as description
from {{ source('terminology', 'icd10cm') }}
