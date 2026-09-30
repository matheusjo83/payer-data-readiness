-- Identity resolution measured against the generator's ground truth
-- (source pbm.truth), for the deterministic baseline and the Splink links in
-- gold.member_xref. The only model that reads the truth.
--
-- scope 'all' covers every cardholder; the other scopes break the results down
-- by injected variation (a cardholder with two variations counts in both) and by
-- kind of person the payer does not know (pbm_only, same_name, twin).
--   linked_correct  linked to the right member
--   linked_wrong    linked to a member that is not the person (false positive)
--   not_linked      a legacy member left unlinked (false negative; review or unmatched)
--   in_review       sent to human review (Splink only)
with truth as (
    select cardholder_id, nullif(member_id, '') as true_member_id, "case" as kind,
           string_split(nullif(variations, ''), ';') as variations
    from {{ source('pbm', 'truth') }}
),

decisions as (
    select 'deterministic' as method, t.cardholder_id,
           case when d.status = 'linked' then d.member_id end as member_id, false as in_review
    from truth t
    left join {{ ref('identity_matches_deterministic') }} d using (cardholder_id)
    union all
    select 'splink', x.cardholder_id, x.member_id, x.status = 'review'
    from {{ ref('member_xref') }} x
),

scored as (
    select
        d.method,
        t.kind,
        t.variations,
        d.member_id is not null and d.member_id = t.true_member_id                      as linked_correct,
        d.member_id is not null and d.member_id is distinct from t.true_member_id       as linked_wrong,
        d.member_id is null and t.true_member_id is not null                            as not_linked,
        d.in_review,
        t.true_member_id is not null                                                    as is_member
    from decisions d
    join truth t using (cardholder_id)
),

scoped as (
    select 'all' as scope, * from scored
    union all
    select unnest(variations), * from scored where variations is not null
    union all
    select kind, * from scored where kind <> 'member'
)

select
    method,
    scope,
    count(*)                                    as cardholders,
    count(*) filter (where is_member)           as legacy_members,
    count(*) filter (where linked_correct)      as linked_correct,
    count(*) filter (where linked_wrong)        as linked_wrong,
    count(*) filter (where not_linked)          as not_linked,
    count(*) filter (where in_review)           as in_review,
    round(count(*) filter (where linked_correct)
          / nullif(count(*) filter (where linked_correct or linked_wrong), 0), 4) as precision,
    round(count(*) filter (where linked_correct)
          / nullif(count(*) filter (where is_member), 0), 4)                      as recall
from scoped
group by method, scope
order by method, scope = 'all' desc, scope
