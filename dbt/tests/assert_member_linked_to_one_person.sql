-- A legacy member may be linked to several cardholder IDs only when they are one
-- PBM person (gold.pbm_persons). Two people linked to one member is a wrong merge.
select member_id, count(distinct pbm_person_id) as pbm_persons
from {{ ref('member_xref') }}
where member_id is not null
group by member_id
having count(distinct pbm_person_id) > 1
