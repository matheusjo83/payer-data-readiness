-- Quality gate for the deduplication within the PBM on the synthetic data, where
-- the truth is known: no automatic merge of two different people.
select *
from {{ ref('identity_dedupe_quality') }}
where merged_wrong > 0
