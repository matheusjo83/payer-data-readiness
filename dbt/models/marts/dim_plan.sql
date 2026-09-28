-- Plan dimension (gold).
select * from {{ ref('stg_legacy__plans') }}
