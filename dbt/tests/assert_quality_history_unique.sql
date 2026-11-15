-- One value per run, area, metric and dimension in the data quality history.
select run_key, area, metric, dimension, count(*) as values
from {{ ref('quality_history') }}
group by all
having count(*) > 1
