-- PBM pharmacy claims, typed.
select
    rx_claim_id,
    cardholder_id,
    try_cast(fill_date as date)                 as fill_date,
    ndc                                         as ndc_code,
    try_cast(quantity as integer)               as quantity,
    try_cast(days_supply as integer)            as days_supply,
    daw_code                                    as dispense_as_written_code,
    try_cast(refill_number as integer)          as refill_number,
    try_cast(refills_authorized as integer)     as refills_authorized,
    pharmacy_id,
    case network_code
        when 'I' then 'in_network'
        when 'O' then 'out_of_network'
    end                                         as network_status,
    try_cast(ingredient_cost as decimal(12, 2)) as ingredient_cost,
    try_cast(patient_pay as decimal(12, 2))     as patient_pay,
    try_cast(plan_paid as decimal(12, 2))       as plan_paid,
    case claim_status
        when 'P' then 'paid'
        when 'R' then 'reversed'
    end                                         as claim_status,
    try_cast(processed_at as timestamp)         as processed_at
from {{ source('pbm', 'claims') }}
