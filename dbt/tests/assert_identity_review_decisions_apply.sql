{{ config(severity='warn') }}
-- Warns about reviewer decisions that no longer apply: the model no longer proposes
-- the case they decided (the data changed), so they have no effect and the case,
-- if it is still uncertain, is back in the queue under its new candidate.
select * from {{ ref('identity_review_audit') }} where not is_applied
