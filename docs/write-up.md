# Making legacy payer data API-ready: a reproducible lakehouse from CDC to SQL on FHIR

*Matheus Julio de Oliveira, with Claude Code (AI-assisted development; see "How this was built")*

Interoperability rules such as CMS-0057-F push health plans to expose claims, coverage and prior
authorization data through FHIR APIs. Most of the work behind those APIs is not the API itself. It
is the data underneath: legacy warehouses with duplicated members, relationships nobody enforces,
dates stored as text, and no record of what changed or when.

This project, which I conceived and developed with AI assistance, is a small, fully local
reference implementation of that data layer. It takes a simulated
legacy payer warehouse, captures its changes from the database log, models it into a tested
lakehouse, and turns it into FHIR resources that can be checked with the specification's own tools.
Everything runs on a laptop with open-source software, and all data is synthetic.

This write-up covers the design decisions, the numbers the project produces, the mistakes found
along the way, and what would need to change before using any of it in production. The code, and the
commands to reproduce every number here, are in the repository.

## How this was built

This project was developed with AI assistance, using Claude Code (Anthropic's coding assistant).

- **Idea, architecture and stack.** Matheus Julio de Oliveira came up with the project idea and
  defined its layered architecture (bronze, silver, gold), designed to run locally with Docker. He
  chose the tools (Postgres, DuckDB, dbt, Synthea) with help from Claude Code, which also helped
  narrow his initial, broader idea to payer data, CMS-0057-F and SQL on FHIR.
- **Phase 1** (legacy source, synthetic data generator, bronze ingestion, first silver and gold
  models) was written by Matheus.
- **Phases 2 to 4** (change data capture, modeling and orchestration, FHIR) were implemented by
  Claude Code under Matheus's supervision. For each main design decision, Claude Code presented
  alternatives with a recommendation; Matheus evaluated them and chose the recommended option each
  time. Claude Code wrote the code; ran the tests, reconciliations and validations; and found and
  diagnosed the problems described in the write-up. Matheus decided how to handle the eligibility
  artifact. He supervised by reading Claude Code's explanations and summaries at each step.
- **After Phase 5** (reproducibility pinning, metric fixes, continuous integration, Phase 6,
  profile conformance, and Phase 7, identity resolution) the work was done the same way, by Claude
  Code under Matheus's supervision, including changes to the legacy generator Matheus wrote in
  Phase 1. In Phase 6, Matheus chose how to handle the fields the legacy source lacked, the invalid
  gender codes and the references; in Phase 7, the second source (a PBM feed), the matching method
  (Splink compared with a deterministic baseline) and the scope (through FHIR).
- **After v2.0.0** (terminology, and the review queue and deduplication in identity resolution)
  the work continued the same way. Matheus chose how to check codes (tx.fhir.org in the CARIN
  validation, plus the public CMS and FDA code lists checked in dbt against every resource, with
  CPT left unverified because it is licensed) and how to finish identity resolution (reviewer
  decisions as a versioned seed applied over the model, with an audit trail and a simulated
  reviewer, and a second Splink model for duplicates within the PBM). For the FHIR API he chose a
  HAPI FHIR server loaded from gold and prior authorizations as Da Vinci PDex resources, and,
  when the prior authorization data fell short of the profile, how to complete it.
- **After v3.0.0** Matheus chose the next direction (the Provider Access and Payer-to-Payer APIs,
  with the other options kept in a backlog) and, for it, a gateway in front of HAPI for the PDex
  operations, the claims without financial data as a gold model on a second server, the opt-outs
  as a legacy table captured by CDC, and PDex 2.1.0.
- **Documentation.** Claude Code drafted the Results section (now in `docs/results.md`) and the
  README sections added from Phase 2 on (now in `docs/architecture.md`), the technical
  write-up and the migration checklist, in the language, format and voice Matheus chose. Matheus
  reviewed the drafts and checked facts independently, including whether CMS requires or
  recommends the CARIN Blue Button guide.
- **Git history.** Matheus made the commits and merged the pull requests, so git lists him as the
  author of every commit. Commit authorship records who committed a change, not who wrote the code.

This write-up is written in Matheus's voice: "I" refers to his decisions, his supervision and the
work he did himself. Work done by Claude Code is attributed to it.

## Summary

- **Data quality with ground truth.** The legacy generator injects seven kinds of known issues at
  documented rates. The pipeline detects them, and for two of them detection can be checked against
  records identified independently of the pipeline: 312 of 312 orphan claim lines and 242 of 242
  claims with an unknown member.
- **Change data capture from the database log.** A Postgres logical replication slot feeds
  append-only change tables. After a streaming test of 640 changes, the current state rebuilt in
  the lakehouse matched the source row for row in all seven tables. Median latency was 2.16 s with
  5-second polling. On DuckLake, the current state is maintained incrementally and checked
  against a full rebuild and against Postgres in CI, and the loader keeps writing while dbt
  builds (0.42 s median latency during a full build).
- **SQL on FHIR on DuckDB.** Claude Code wrote a compiler from SQL on FHIR v2 ViewDefinitions to DuckDB
  SQL.
  It passes all 133 shareable tests of the specification's shared test suite.
- **Legacy data as FHIR, checked two ways.** The legacy data becomes 5,000 Patient, 5,000 Coverage,
  24,507 ExplanationOfBenefit and 301 Organization resources. Round trips through FHIR reproduce the
  source tables with zero differing rows, and the HL7 validator reports zero errors on a
  1,600-resource sample against the CARIN Blue Button 2.1.0 profiles, with references resolved
  and codes checked by tx.fhir.org. Before remediation, every sampled resource failed those
  profiles. Every ICD-10-CM, NDC and place of service code in every exported claim is valid on its
  service date against the CMS and FDA releases, after one diagnosis code the generator used turned
  out to have been retired in 2021.
- **A FHIR API, checked against the source.** A HAPI FHIR server serves 42,550 resources loaded
  from gold, including 3,000 prior authorizations as Da Vinci PDex resources (zero errors against
  PDex 2.1.0). 143 checks computed from gold pass through the API: counts, the searches CARIN
  and PDex require, and a Bulk `$export` whose every resource equals its gold version.
- **Provider Access and Payer-to-Payer.** 300 attribution lists built from claims and prior
  authorizations, opt-outs captured from the legacy database and checked when an export runs, the
  claims without financial data (CARIN Basis profiles), and a gateway that implements the PDex
  export and member match operations HAPI lacks. 129 checks computed from gold pass through it,
  including an opt-out added after the list was built and a match request with a case for every
  rule.
- **Identity resolution with ground truth.** A PBM feed sends 2,900 of the members under its own
  IDs, with nicknames, typos, changed names, mistyped birth dates and old addresses, plus 275 people
  the payer does not know and 94 people with a second cardholder ID. A Splink model links 2,928 of
  2,991 member cardholder IDs correctly and none wrongly (recall 97.9%) and sends 74 uncertain
  cases to review; exact rules link 2,794 correctly and 14 wrongly (recall 93.4%). With the queue
  worked by a simulated reviewer, recall reaches 98.9%, still with no wrong link, and 93 of the 94
  duplicate IDs are merged, none wrongly.

These figures describe synthetic data generated with a fixed seed. They show that the pipeline
behaves as designed. They do not describe any real health plan.

## Architecture

```
Synthea (synthetic FHIR R4) ──── NDJSON load ──┐
                                               ├─► Bronze ─► Silver ─► Gold ─► SQL on FHIR views
Legacy payer DB (Postgres) ──── CDC (WAL) ─────┤   (raw)    (tested)  (models)  Quality & lineage
                                               │                      Identity resolution
PBM files (pipe-delimited) ──── file load ─────┘
          └──── orchestrated by Airflow (CDC sync, FHIR and PBM loads, dbt build, coverage) ────┘
```

The stack is Postgres for the legacy source (with the wal2json plugin), DuckDB as the lakehouse
engine (since the last step, on DuckLake: a Postgres catalog and Parquet files), dbt for transformations, tests and lineage, Synthea for synthetic FHIR data, Splink for
record linkage, Python for ingestion, and Apache Airflow for orchestration. I chose it, with help from Claude Code, so that anyone
can run the whole project with `make` and Docker, at no cost. The trade-offs of that choice come up throughout, most
of all DuckDB's single writer, which the move to DuckLake at the end removed.

It was built in five phases, each merged as its own pull request: foundation, change data capture,
modeling and orchestration, FHIR, and this write-up. I wrote the first phase; Claude Code implemented
phases 2 to 4 and drafted this write-up. Later pull requests pinned the generators, added continuous
integration, and added two phases: conformance to the CARIN Blue Button profiles (Phase 6) and
identity resolution across two sources (Phase 7).

## A legacy source you can measure against

A data quality pipeline is hard to evaluate on data whose problems you don't know. So I started
by generating the legacy warehouse myself, with traits common in real legacy systems: cryptic
column names, dates stored as `VARCHAR(8)`, single-character codes and no foreign keys. The
generator then injects known issues at documented rates:

| Check                        | Detected | Population    | Injection rate      |
|------------------------------|---------:|---------------|---------------------|
| Duplicate member IDs         |       94 | 5,000 members | 1.5% of members     |
| Invalid gender code          |       51 | 5,000 members | 1.0% of members     |
| Unparseable birth date       |       44 | 5,000 members | 1.0% of members     |
| Claims with unknown member   |      242 | 25,000 claims | 1.0% of claims      |
| Paid amount exceeds charged  |      405 | 25,000 claims | 2.0% of paid claims |
| Claims outside eligibility   |      251 | 24,758 claims | 1.0% of claims      |
| Orphan claim lines           |      312 | 62,795 lines  | 0.5% of lines       |

Counts match the rates up to random variation, but matching a rate is weak evidence. For two
checks the generator gives the injected records IDs outside the valid ranges, so they can be found in
the raw layer without going through the models. The pipeline flagged exactly those records and no
others: 312 of 312 orphan lines and 242 of 242 unknown-member claims. The other checks have no
independent marker in the data, so for those only counts are reported.

Generating the data this way also produced something I did not plan for. When Claude Code added
the eligibility check in Phase 3, it flagged 25% of claims as outside the member's coverage. That was
not a finding. The generator drew claim dates and coverage dates independently, so a quarter of the
claims fell before coverage started. Publishing that number as "issues detected" would have been
misleading. I decided to fix the generator: Claude Code changed it so that claims fall inside coverage and
injected 1% outside it at a documented rate. The detected rate is now 1.01%. The fix changed the random sequence, so
other published figures changed too, and they were updated rather than keeping numbers the code no
longer produces.

**Lesson:** synthetic data is only useful as evidence if you know what is in it, and that includes
the artifacts of how you generated it.

## Change data capture from the database log

The first version reloaded full snapshots of every table. That is simple, but it cannot see
deletes, it puts load on the source, and it says nothing about how fresh the data is. For Phase 2,
Claude Code laid out four options, with a recommendation: log-based CDC with a replication slot,
Debezium with Kafka, triggers writing to an audit table, and a watermark on update timestamps. I
evaluated them and chose log-based CDC, the recommended option. It captures deletes
and gives each change its commit time without touching the legacy tables. Triggers would change the
source system, which legacy owners often refuse. Watermarks miss deletes, and two of the tables
have no timestamp column at all. Debezium solves the same problem with much more infrastructure
than a local reference project needs.

The loader reads the slot through `pg_logical_slot_peek_changes` and wal2json. The design comes
down to three decisions:

1. **Create the slot before the snapshot.** Nothing committed during the initial copy is lost.
   Changes that are both in the copy and in the slot get replayed on top of the copied rows.
   Because every change carries the full new row, the replay ends in the same state.
2. **Peek, store, then advance.** The loader reads changes without consuming them, writes them to
   DuckDB in one transaction together with the batch's commit position, and only then lets
   Postgres release that part of the log. If the process dies between the write and the release,
   the next run sees the stored position and skips the batch. Claude Code tested exactly that failure: after
   the simulated crash, the restart re-applied none of the stored changes.
3. **Keep changes, derive state.** The raw layer holds append-only change tables, one row per
   insert, update, delete or truncate. A dbt macro rebuilds the current state: the latest version
   of each key after the last truncate, without deleted keys.

To measure latency, Claude Code ran the loader with 5-second polling while a simulator committed 300 events
over 60 seconds:

| Table      | Changes | p50 latency | p95 latency | Max latency |
|------------|--------:|------------:|------------:|------------:|
| `clm_ln`   |     340 |      2.06 s |      4.86 s |      5.05 s |
| `clm_hdr`  |     143 |      2.13 s |      4.86 s |      5.05 s |
| `pa_req`   |     121 |      2.50 s |      4.68 s |      4.97 s |
| `mbr_mstr` |      36 |      2.86 s |      4.53 s |      4.86 s |

The polling interval sets these numbers, not processing time: each batch took about 0.03 s to
write. Both processes ran on one machine, so there was no clock skew between them. Across machines,
skew would add to the measured latency.

Two bugs in this phase are worth describing, because both passed the first round of tests:

- **Padding in fixed-width columns.** Postgres stores `CHAR(1)` values with their blank padding.
  The snapshot path read them as text, which drops trailing spaces, while wal2json emitted the raw
  padded value. The same row ended up with `''` in one path and `' '` in the other. Downstream
  models trimmed the value, so no metric changed, but the raw layer did not agree with itself. A
  full comparison of the rebuilt state against the source found it. Claude Code found it and fixed
  it by applying the same text semantics to change values.
- **Rows that share a log position.** The CDC design assumed every change has its own log sequence
  number, and Claude Code wrote a test for it. That holds for single-row writes. It does not hold for `COPY`: Postgres
  writes bulk inserts as multi-row log records, and every row in one record shares the same
  position. The test failed only in Phase 3, the first time the data was reloaded with CDC running.
  The rebuilt state had been correct all along, because rows in one multi-insert always have
  different keys. Still, the ordering had no formal tie-breaker. Claude Code added each change's
  position within its transaction as a second key and renamed the test to check the pair.

**Lesson:** compare the rebuilt state with the source row by row, and test the bulk paths
(reloads, `COPY`) as well as the row-by-row ones. The tests had covered the expected case, not every
case the database produces.

## Modeling, tests and what "covered" means

The silver layer rebuilds each source's current state and makes it usable: deduplicated, typed,
decoded and tested. The gold layer has dimensions for members, plans and providers, facts for
claims, member months and prior authorization timeliness, and the quality and reliability models.

Tests fall into two groups on purpose. Tests on keys, relationships and allowed values that should
always hold are errors, and they fail the build. Tests that flag issues known to exist in the
legacy source are warnings: they count those issues without stopping the pipeline, and
`gold.dq_issue_summary` reports the counts. At the end of Phase 3 the build ran 136 nodes: 131
passed and 5 ended in the expected warnings. It now runs 247: 241 pass, and 6 end in expected
warnings (the same five, plus the replication slot the drill drops on purpose).

For coverage, a small script written by Claude Code combines the dbt manifest (descriptions and tests) with
the catalog (the columns that actually exist in the database). A column that exists but is missing
from the documentation counts against coverage. Every model and column is documented, and every
model has at least one test. Column-level test coverage was 38.0% in silver and 23.5% in gold when
this was first written, in Phase 3; now it is 28.5% and 22.9% (the incremental current state
added 114 silver columns, the raw legacy columns, whose tests cover the key and the CDC metadata). I
publish those numbers as they are. The tests cover keys, relationships, allowed values, parity and
the quality checks, and adding tests to descriptive columns only to raise the percentage would make
the metric less honest, not the data more reliable.

For prior authorization, `gold.fct_prior_auth_timeliness` measures decision time against the
CMS-0057-F timeframes of 72 hours for expedited and 7 calendar days for standard requests. On this
data, 91.0% of decided expedited requests and 86.0% of decided standard ones met their timeframe.
The generator draws decision times from exponential distributions, so these figures show that the
model computes the metric correctly. They say nothing about how any organization performs.

## Orchestration: Airflow orchestrates, it doesn't compute

Airflow 3 runs two DAGs in Docker: a CDC refresh every 30 minutes (`cdc_sync` → `dbt_build`) and a
manual full refresh that also loads FHIR, checks the ViewDefinitions and reports coverage. (A third
DAG, added after Phase 7, monitors the replication slot; see the limitations below.) Three
decisions shaped it:

- **Same commands as `make`.** Tasks call the same scripts and `dbt build` through `BashOperator`.
  The loaders and dbt live in their own virtualenv inside the image, so their dependencies never
  conflict with Airflow's.
- **One writer at a time.** DuckDB allows a single writer. Every task that writes the lakehouse
  runs in an Airflow pool with one slot, so tasks from both DAGs queue instead of failing on a
  locked file.
- **Pinned versions.** The host and the container write the same DuckDB file. A newer DuckDB in
  the container could write a storage format the host cannot read, so the versions are pinned.

The single-writer constraint was the clearest limit of this stack, and the first thing I would
replace for concurrent pipelines; the last step of the project did (see DuckLake below). Since
then only the dbt builds queue in the pool.

## SQL on FHIR, compiled to DuckDB

SQL on FHIR v2 defines ViewDefinitions: portable, declarative projections of FHIR resources into
tables, written with FHIRPath expressions. The goal was to run them inside the lakehouse, on the raw
FHIR JSON, as part of the dbt build. Claude Code presented three options: running the JavaScript
reference implementation, running Pathling on Spark, or writing a compiler to DuckDB SQL; I chose
the third, which it recommended. The first two run outside the database. Claude Code wrote the
compiler.

The central constraint is that DuckDB does not allow subqueries inside lambda functions. So the
compiler turns everything into expressions over a single resource:

- Every FHIRPath expression becomes a DuckDB expression of type `JSON[]`, which stands for a
  FHIRPath collection. `name.given` flattens across arrays, `where()` becomes `list_filter`, and
  `first()` becomes a list slice.
- Every `select` becomes a list of JSON rows. `forEach` maps over a collection, `unionAll`
  concatenates lists, and nested selects combine through a cartesian product of lists.
- The final query unnests each resource's rows and projects the columns.

To check it, Claude Code ran the specification's shared test suite, pinned to a specific commit, and compared
results the way the reference runner does: rows in any order, exactly the view's columns, and
tests that expect an error pass only if compilation or execution fails. The first run passed 131
of the 133 shareable tests. The two failures were about `%rowIndex`: the null row of an empty
`forEachOrNull` must have index 0, and `repeat` must walk the tree depth-first, not breadth-first.
After fixing both, it passes 133 of 133 shareable tests and 3 of 11 experimental ones. The eight
experimental failures are `lowBoundary()` and `highBoundary()`, which the compiler does not implement. `repeat`
is unrolled to 10 levels rather than truly recursive.

A build script compiles each ViewDefinition into a dbt macro that takes the table holding the
resources as a parameter. The same view then runs over any source:

```sql
select * from {{ vd_eob_summary(ref('fhir_explanation_of_benefit')) }}
```

The generated macros are committed, and an Airflow task fails if they are out of date with the
ViewDefinitions. The main cost of this approach is speed: over Synthea's 53,875
ExplanationOfBenefit resources, the view is the slowest step of the build, at about 14 seconds.

## Legacy data as FHIR, and how it was checked

The last step of Phase 4 turned the legacy warehouse into FHIR R4 resources aligned with the CARIN
Blue Button profiles: Patient, Coverage and ExplanationOfBenefit. "Aligned" was deliberate. The
resources carried the elements those profiles center on, but they were validated against base FHIR
R4, not against the CARIN profiles. Phase 6, described in the next section, closed that gap.

Three rules guided the mapping:

- **Don't invent data.** In Phase 4, legacy gender codes outside M/F/U were omitted rather than
  mapped to `unknown`, which I thought would hide the fact that the source value was invalid. Phase 6
  changed this, as explained below. FHIR does not allow null values or empty arrays, so the SQL that
  builds each resource strips them.
- **Gate on integrity.** An ExplanationOfBenefit requires a patient and a coverage. Claims with an
  unknown member or outside the member's coverage are not exported: 493 of 25,000, exactly the 242
  and 251 counted by the quality checks. They stay in the gold claims table with their flags, so
  the gap is visible instead of being quietly filled.
- **Prove the round trip.** The ViewDefinitions run over the generated resources, and the result
  is compared row by row, in both directions, with the gold tables the resources came from.

| Comparison                                   | Rows   | Only in view | Only in reference |
|----------------------------------------------|-------:|-------------:|------------------:|
| Synthea patients: view vs. hand-written model |    558 |            0 |                 0 |
| Legacy patients (round trip)                 |  5,000 |            0 |                 0 |
| Legacy coverage (round trip)                 |  5,000 |            0 |                 0 |
| Legacy claims (round trip)                   | 24,507 |            0 |                 0 |
| Legacy claim lines (round trip)              | 61,228 |            0 |                 0 |

A comparison that always returns zero proves nothing unless it can fail, so Claude Code ran it
against deliberately altered references. Swapping paid for charged amounts produced 24,507 mismatched rows,
adding the claims outside coverage produced 251, and mapping invalid gender codes to `unknown`
produced 51. Each number is the one the change should cause.

Finally, Claude Code ran the official HL7 FHIR validator on a reproducible sample of 200 resources per type.
It reported zero errors. All 600 warnings are the best-practice recommendation that resources carry
a human-readable narrative, which these resources did not have then (they do now; see Cleaning up
the warnings). As with the parity test, Claude Code checked
that the validator fails when it should. It rejected hand-made resources with the legacy problems the mapping avoids: a
null value, gender `invalid`, a `YYYYMMDD` date, an empty array and missing required elements.

## Profile conformance

A comment on the project asked whether the validation covered payer profiles or only the base
specification. It covered only the base specification, so Phase 6 measured the distance to the
CARIN Blue Button profiles and then closed it. Claude Code checked which version CMS lists for the
Patient Access API: STU 2.1.0, which builds on US Core 6.1.0. CMS lists 2.0.0 too, but marks it as
derived from standards that expired on January 1, 2026.

The baseline ran the HL7 validator against CARIN on the Phase 4 resources. Every sampled resource
failed: 1,000 of 1,000, with 24 distinct errors. About half were mapping gaps, where the data
existed but the resource did not carry it (`meta.lastUpdated`, identifier types, diagnosis types).
The other half were fields the legacy source did not have at all: the type of bill that separates
inpatient from outpatient claims, the place of service, the network status and the pharmacy
details (NDC, days supply, dispense as written code, refills).

Claude Code presented three decisions, and I chose its recommendation each time:

- **Missing fields.** Extend the legacy generator with them, rather than documenting the gap or
  exporting only what could conform. Real payer warehouses have these fields. To keep every
  published figure, the new fields come from a separate random generator, so the original columns
  kept their values; the rebuild confirmed that nothing on the results page changed.
- **Gender.** Map invalid codes to `unknown`. The profile requires a gender, and FHIR's `unknown`
  says exactly what is true: the value is not known. The invalid source codes stay counted in the
  quality table, so the problem is still visible where it belongs.
- **References.** Validate each sampled resource in a Bundle with the resources it references, and
  add Organization resources for the payer and the providers, instead of leaving the references for
  later.

The references paid off at once. With them resolved, the validator found three problems that
validating resources one by one had hidden: the Organizations had no `meta.lastUpdated`, about 90% of
the synthetic NPIs had an invalid check digit, and the payer referenced by every claim failed its
profile, which the validator reported on each claim and coverage that pointed to it. After Claude
Code fixed them, the validator reports zero errors on 1,400 sampled resources: 200 per resource type
and per ExplanationOfBenefit profile (1,600 since Phase 7 added a sample of the PBM pharmacy claims).

Checking that the checks could fail found two gaps in the tooling itself. First, the validator does
not report a reference it cannot resolve inside a Bundle: a Bundle with the provider removed
passed. A dbt test now checks that every reference in all exported resources resolves. Second, the
validator without a terminology server still reused codes cached by an earlier run against one, so
the offline result depended on the machine. The validation now clears that cache. The same test
against tx.fhir.org showed that the fictitious NDCs the generator first produced would be rejected,
so it now uses real NDCs of common generic drugs.

A third gap was in the project's own claim of reproducibility. The first CI run of this phase reported the
same zero errors as the local run but different warning counts. The "reproducible" sample, a
reservoir sample with a fixed seed, depended on the physical order of the rows, and dbt writes the
rows in a different order on each build. The sample is now chosen by a hash of the resource ID.

## Identity resolution

A comment on the project noted that validation can say a Patient is well formed, but not that it
is the same person as the one arriving through another feed. That is identity resolution, and it
belongs in the data layer, before anything reaches the API. The project had no such case: the
legacy members and the Synthea patients are separate populations. So Phase 7 created one, the
same way Phase 1 created the data-quality issues: on purpose, at documented rates, with a record
of the truth.

Claude Code presented three decisions, and I chose its recommendation each time:

- **The second source.** A pharmacy benefit manager (PBM) that issues its own cardholder IDs and
  sends members, pharmacies and pharmacy claims in pipe-delimited files, a common arrangement for
  payers. Its generator takes 60% of the covered members and varies their records (nicknames,
  typos, a changed last name, transposed or mistyped birth dates, old addresses, missing fields),
  then adds unrelated people and deliberate look-alikes. It has its own random generator and only
  reads the legacy data, so no published figure changed.
- **The method.** Splink, a probabilistic (Fellegi-Sunter) record linkage library that runs on
  DuckDB, compared with a deterministic baseline, so the gain is measured rather than assumed. The
  model runs as a dbt Python model, inside the lineage, and is trained without labels; the ground
  truth is read only by the model that scores the results.
- **The scope.** Through FHIR: a linked member's Patient carries the PBM cardholder ID, and the PBM
  claims become ExplanationOfBenefit resources that reference that Patient, validated by the same
  CARIN gate as the rest.

The first model made the error master patient indexes are known for. It linked nine "twins" (same
last name, birth date and ZIP code, different first name) with match probabilities above 0.99.
Nicknames and typos had taught it that differing first names are not rare among true matches, so
the disagreement cost little. The exact rules did the same: all 14 of their false positives were
the twins. Two changes followed. A nickname level in the first-name comparison, through a small
dictionary, which raised the correct links. And a guard: no automatic link when the first names
disagree completely; the case goes to a review queue with both records side by side. The guard
costs ten true matches that now wait for review, and it removed every wrong link. In Phase 7,
before the PBM had duplicate cardholder IDs (current figures in the next sections):

| Method        | Linked correctly | Linked wrongly | Not linked | In review | Precision | Recall  |
|---------------|-----------------:|---------------:|-----------:|----------:|----------:|--------:|
| Deterministic |            2,719 |             14 |        181 |         — |    99.49% |  93.76% |
| Splink        |            2,847 |              0 |         53 |        69 |   100.00% |  98.17% |

The breakdown by variation shows where the gain comes from. The exact rules find none of the 142
members whose birth date is transposed, mistyped, missing or invalid in the legacy source, because
every rule requires the birth date; Splink finds 77% to 90% of them. A test now fails the build if
an automatic link is wrong or recall falls below 95%, and it fails when pointed at the baseline.

The PBM claims then follow the members: in Phase 7, 4,573 of 5,141 became ExplanationOfBenefit
resources (4,717 of 5,275 now, with the duplicates and the review). The other 568 waited,
flagged, because their cardholder was in review or unmatched (529) or the member had no coverage
on the fill date (39).

One more reproducibility detail surfaced here. Splink's scores differed in the 15th digit from run
to run, because DuckDB adds up parallel sums in varying order. No decision changed, but the
published scores should not depend on the thread scheduler, so the model runs on one thread.

## Codes, checked against their code systems

Validation without a terminology server checks the shape of a code, not whether it exists. Claude
Code presented two ways to close that gap, and I chose to do both, as it recommended: run the
CARIN validation against tx.fhir.org, the public HL7 terminology server, and check every code in
every exported claim inside the lakehouse, against the code lists the US government publishes for
free. The second matters because the validator sees 1,600 resources, and the API would serve
29,224 claims. The ICD-10-CM releases come from CMS, one per fiscal year and pinned by SHA-256;
the NDC Directory comes from the FDA; the place of service codes are a small seed. A dbt model
reads the codes through a ViewDefinition, so it checks what the API would serve, and checks each
one on its service date. CPT stays unverified: the AMA licenses it.

The check found a real error on its first run. The legacy generator used M54.5, low back pain,
which was a valid diagnosis code until September 30, 2021. On October 1 it became a category,
split into M54.50, M54.51 and M54.59, and a category cannot be billed. Every claim here dates from
2024 or later, so all 6,079 of those diagnoses were invalid, and the HL7 validator without a
server could not have said so. The generator now writes M54.50 from that date. A test fails the
build on any invalid code, and the check rejected each of four deliberately wrong codes.

The two checks disagreed on one point. tx.fhir.org reported all ten NDCs as inactive, while the
FDA directory lists every one of them as marketed, with no end date. Asked directly, the server
turned out to serve the NDC as of November 2021 and to mark every code in it inactive, including
drugs marketed since 1987; anything launched since then it does not know. The lakehouse follows
the FDA, which publishes the code system.

## Working the review queue, and duplicates within a source

Phase 7 ended with a review queue nobody worked and a PBM that was assumed to hold each person
once. Claude Code presented the options for both, and I chose its recommendations.

- **Decisions as data.** A reviewer's decision is a row in a versioned dbt seed (who, when, which
  pair, match or no match), and the cross-reference applies it over the model. The model's own
  decision stays next to the final one, and an audit model shows whether each decision still
  applies: if the data changes and the model no longer proposes the case that was decided, the
  decision stops applying and the case returns to the queue, with a warning.
- **A simulated reviewer.** There are no reviewers here, so a script decides each case from the
  ground truth and appends the decision to the seed. It is always right, so the results after
  review are an upper bound; what they show is that the decisions flow through to the links, the
  FHIR identifiers and the exported claims.
- **Duplicates.** The generator now gives 3% of the people a second cardholder ID, keyed again with
  a new address or a typo, as when a PBM reissues a card. A second Splink model deduplicates the
  PBM's own records, under the same rules as the linkage, plus the payer's view: when the payer's
  records say the two cardholders are different members, they are not merged automatically, and
  when both point to the same member, the pair goes to review.

Duplicates made the linkage harder before they made it better. Two cardholder IDs of one person
both point to the same member, which looks like two people claiming one member, so until the pair
is merged both go to review; 50 of the 183 decisions are such cases. Once duplicates are grouped
into persons, the IDs of one person may share a member, and deciding one case can put another in
the queue, so the simulated reviewer runs in rounds until the queue is empty (on this data, the
first round decided all 183 cases and the second found none). Started from an empty seed, it
writes the committed seed again, byte for byte. The model links 2,928 of the 2,991 member
cardholder IDs with no wrong link; review adds 30, all of them correct, and rejects 44 look-alikes
and twins. The 33 still missing scored below the review threshold, so no reviewer ever sees them.
That is the cost of the threshold, and it is the number to watch if this were real.
Deduplication merges 62 of the 94 duplicate pairs on its own and 93 after review, none wrongly. A
test now fails the build if a reviewed link is wrong; I checked that it fails by flipping one
decision on a look-alike to a match.

## Cleaning up the warnings

After the merge, the CI run passed but showed warnings, and I asked for the project to be
completely clean. Claude Code sorted every warning in the run by cause before changing anything,
and I chose to treat all of them.

- **The CI itself.** Two actions still targeted Node.js 20 and the runner label was about to move
  to a new Ubuntu release. The actions were updated and the runner pinned to `ubuntu-24.04`, which
  also suits a project that pins everything else.
- **Synthea.** It warned that its records referenced organizations and practitioners it did not
  export. Turning off its transaction bundles would have silenced the warning but rewritten the
  references in most files; exporting the providers silenced it and left every existing file
  unchanged, which Claude Code confirmed file by file.
- **The deduplication model.** Splink warned that three comparison levels were never trained and
  fell back to defaults: the PBM's 94 true duplicate pairs are too few. The model now takes its m
  probabilities from the PBM-to-payer linkage, which has thousands of true pairs. It merges fewer
  pairs by itself, all correctly. Regenerating the review decisions for the new model then made a
  test fail: two duplicate cardholder IDs, each confirmed as the right member, remained two PBM
  persons, because their pair scored too low for review. The rule was missing one direction of the
  payer's evidence; a pair whose cardholders share a likely member now goes to review. The old
  model had hidden the gap by scoring those pairs higher.
- **The validator.** 10,101 warnings on the CARIN sample, of five kinds. Three were ours: no
  narrative, no Payer Type code on Coverage, and an NPI type outside the base value set. All three
  are fixed. Two cannot be fixed in the data: CARIN requires an identifier type for claims that the
  base value set lacks (CARIN's own examples get the same warning), and tx.fhir.org's NDC is a 2021
  snapshot. Those are counted apart, with their reason, rather than hidden or worked around with a
  code that would be wrong.

What remains is deliberate or outside the project: the six dbt warnings that count the issues
injected into the legacy data and the slot the drill drops (they are the evidence, not noise), and
one line from the Java runtime inside the pinned Synthea release.

## A FHIR API, and prior authorizations

CMS-0057-F asks for APIs, and until here the project produced resources but served none. Claude
Code presented the options, and I chose its recommendations: a HAPI FHIR server in Docker, loaded
from the gold models by a publish step, with search and Bulk `$export`; and the prior
authorizations, which the Patient Access API must include, as Da Vinci PDex Prior Authorization
resources, an ExplanationOfBenefit with `use = preauthorization`. PDex rather than PAS, because
PAS is the exchange between provider and payer for asking for an authorization; what a member's
app reads is the payer's record of it. CMS lists PDex 2.0.0 and 2.1.0 for the Patient Access API
and marks 2.0.0 as derived from expired standards, so this uses 2.1.0, as it uses CARIN 2.1.0.

A prototype validated against PDex before any model was written found four gaps. Two were
mechanical: the profile has to be declared without a version, and the payer needed an identifier,
which Da Vinci HRex requires of the organization a Coverage points to. Two were in the data, and
Claude Code brought them to me as decisions:

- **Requests outside coverage.** 483 of the 3,000 requests were dated on a day the member had no
  coverage, and an ExplanationOfBenefit needs one. It was the artifact the claims had in Phase 3:
  the generator draws the member and the date independently. I chose to fix it the same way. The
  date moves into the coverage, at the same time of day and with the same delay to the decision,
  from a separate random generator, so the timeliness figures did not change, and every other
  legacy table came out identical, value by value.
- **An amount.** PDex puts the decision on an adjudication that must carry an amount, and the
  legacy table had none. The generator now records a requested amount, as Phase 6 added the
  fields CARIN required.

The decision itself is an X12 code, and X12 code lists are licensed. The PDex and PAS examples
show A1 (Certified in total) and A4 (Pending); no open source shows the code for a denial. I chose
A3 (Not Certified), the X12 code for it, and the documentation says that nothing here can verify
it. HRex also requires a birth date, which the 44 members with an invalid legacy birth date do
not have; as with gender in Phase 6, the value is not invented: the resource says it is unknown,
with FHIR's data-absent reason. With these, the validator reports zero errors and zero warnings on
547 prior authorizations, one sample per decision, and a round trip through a ViewDefinition gives
back every request with zero differing rows.

The server is checked the way the rest of the project is: against gold, not against itself. A
script computes every expected figure from the lakehouse and asks the API: counts by type and by
use, the searches the CARIN and PDex capability statements require (five of them are not in base
FHIR, so their definitions are loaded from the two guides first), and a Bulk export. All 143
checks pass, and all 42,550 exported resources equal gold apart from `meta`. Proving that the
checks could fail found a gap in them: with one resource deleted on the server, the counts still
matched, because HAPI reuses the result of an identical search for a minute. The checks now ask
for fresh results, and the same test fails as it should.

## DuckLake, and a current state that is not rebuilt every time

Two limits were left in the list of what this project did not show. The lakehouse was a DuckDB
file, which takes one writer at a time, so the CDC loader and dbt took turns. And every build
recomputed the current state of each legacy table with a window function over its whole change
history, which is fine for a hundred thousand changes and not for a production log. Claude Code
presented the alternatives, and I chose its recommendation: DuckLake, with its catalog in Postgres
and its data in Parquet, and an incremental current state merged by key from an `_lsn` watermark,
with deletes applied.

DuckLake is DuckDB's own lakehouse format, and the documentation says what it is not: not Iceberg
and not Delta Lake. The tables are Parquet files, but their metadata lives in DuckLake's catalog
schema in Postgres, so Spark or Trino cannot read them through an Iceberg or Delta catalog. What
it gives this project is several writers. With the loader polling every two seconds and the
activity simulator committing changes, a full `dbt build` ran at the same time: the loader landed
626 changes in 133 batches while dbt rebuilt every model, without a single conflict, with a
median latency of 0.42 s. On the DuckDB file the loader had to back off whenever dbt held it.

The move surfaced three things DuckLake does differently, and one of them was a reproducibility
bug of mine. It has no primary key constraints, so the loader's state table lost one it did not
need. It allows one `UPDATE` or `DELETE` per `MERGE`, so the incremental models use
delete+insert. And it does not keep a table's rows in a fixed order: the Splink scores changed
from build to build, because Splink's random samples, seeded as they are, depend on the order of
the rows they sample. The DuckDB file had kept that order stable by accident. The models now
sort their input, and three builds in a row gave identical scores again, with every link, merge
and review decision unchanged.

The incremental current state reads only the changes past its watermark and keeps the latest per
key. A deleted key stays as a tombstone that the silver views hide, so the watermark is always the
last change applied. Two events break the chain, and the model rebuilds itself on either: a
TRUNCATE past the watermark, and a new snapshot, which the loader takes when the replication slot
is lost. Each row records the snapshot it came from, which also catches a recreated source
database, whose log positions start again from a lower number.

The checks follow the project's habit. A dbt test compares every incremental table with a full
rebuild from all its changes, and a script compares it with the Postgres tables, row by row. Both
passed after simulated changes (725 of them, 56 deletes), after a new snapshot, after a TRUNCATE
and reload of 101,201 rows, and after a run with nothing new. Both failed when I corrupted two
rows of the incremental tables. The concurrent run found one more gap, in the test itself: it
compared the incremental table with a rebuild from a change table the loader had already moved
past. It now stops at the incremental table's watermark, which is why deleted keys stay as
tombstones instead of being removed.

## Provider Access and Payer-to-Payer, and what a provider may not see

The FHIR API served the members' own data. CMS-0057-F also asks a payer to share it with the
providers who treat them (the Provider Access API) and with the member's next health plan (the
Payer-to-Payer API), both without remittances or cost-sharing, both by January 2027. Claude Code
read the Da Vinci PDex 2.1.0 pages for both, presented the alternatives, and I chose its
recommendations each time: a small gateway in front of HAPI for the operations HAPI does not
have; the claims without financial data as a gold model, served from a second HAPI server; the
opt-outs as a legacy table captured by CDC; and PDex 2.1.0, the version the prior authorizations
already use, with the changes in 2.2.0 documented rather than followed.

The claims without financial data are not mapped again. CARIN 2.1.0 has a *Basis* version of
each claim profile, the full one with only the financial requirements removed, and the model
derives each claim from the full resource: it removes the totals, the payment, the line amounts
and every adjudication that carries an amount, and keeps the rest, including whether the claim
was paid in or out of network. A test checks that no amount is left and that the lines,
diagnoses and references are those of the full claim.

Attribution comes from the claims, which PDex allows: a member belongs to the list of a provider
who billed an in-network claim or asked for a prior authorization for them in the last 24 months.
That gives 300 lists. Of the 3,911 attributed members, 89 had opted out, so they are left out of
the lists and counted in each one. CMS lets members opt out, and PDex says the consent status
must be checked when an export runs, not only when the list is built. The legacy database now
records opt-outs (132, 17 of them ended when the member opted back in), and the gateway checks
them against the server on every export, so an opt-out recorded an hour after the list was built
still keeps that member's data from leaving.

The gateway is the part I would look at first in a review, since HAPI does neither operation.
For an export it runs HAPI's Group export and filters the result, because HAPI ignores the
`patient` parameter on a Group export and adds the Group itself: only the members asked for,
without the ones opted out at that moment, and, for another payer, only claims from the last five
years and prior authorizations that are pending or were decided in the last twelve months. A
download records on the Group what each member was sent, as PDex requires, and a later delta
sends only what changed since. For the Payer-to-Payer match it takes what the new plan knows
(the member's demographics, the old member ID, the member's consent) and answers with three
groups: matched, not matched, and matched but not shareable because of the consent. A changed
family name still matches; two members with the same demographics do not. This payer has no way
to tell sensitive data apart, so a consent for non-sensitive data only cannot be complied with,
and the member goes to the third group instead of receiving everything.

The checks work the same way as before: a script plays the provider and the new payer, and gold
decides what the answers should be. It exports three attribution lists, puts an opt-out on the
server after the lists were built, runs a download and then a delta, sends a match request with a
case for every rule, exports the matched members and calls the one-member match. All 129 checks
pass, and they fail when they should: with one full claim, amounts included, put back on the
Provider Access server and one opt-out deleted, five of them failed. The validator found the gaps in the request my check sends, as it should have: HRex
requires a member ID on the old coverage, so the case I had written as "no member ID" became "a
member ID this payer does not know". It also found one error that no request can avoid. HRex's
two consent policy codes are links to anchors on a web page, which the validator cannot resolve,
and the guide's own example fails with the same error. It is counted apart, with the reason, like
the three warnings that come from conflicts between PDex and the guides it builds on.

## Prior authorization, from the order to the decision

Until here the prior authorizations were the payer's records, published for the member; how a
provider asks for one was outside the project. CMS-0057-F also requires a Prior Authorization
API, and Da Vinci splits the exchange into three guides: CRD tells the provider, while the order is
being written, whether the payer needs an authorization and what documentation; DTR hands over
the questionnaire; PAS carries the request and the decision. I chose the payer's side of all
three, in version 2.1.0, the one CMS lists, with a script playing the provider's EHR. The requests
are written to the legacy source, as a utilization management system would hold them, so that the
pipeline that already exists carries each one to the lakehouse and to the member's app. Decisions
come from fixed rules: a request whose documentation meets the criteria is approved on arrival,
any other is pended with the reason, and a simulated reviewer decides it later. The provider
learns of that decision through a PAS Subscription, or by asking with `$inquire`.

A prototype of one full cycle, built by hand from a real request in gold and validated before any
code was written, found what the data lacked. DTR requires the clinician who answered to be named
as a US Core Practitioner, and CRD's patient carries the provider's medical record number; the
project has neither, and both are the provider's data, so I kept them in the simulated EHR. PAS
requires a place of service, a diagnosis and a service date on each request, which `pa_req` did not
have; I chose to add them, with the channel the request came through and a table for the
questionnaire answers, and to give the 3,000 generated requests their values from a separate
random generator, so that no published figure changed. Two services need an authorization,
physical therapy and a chest X-ray, each with its questionnaire.

The check states its own expected decisions for nine requests from two providers and follows
them through every step: the coverage requirements, the questionnaire, the decision on arrival,
the rows written to the legacy source, the reviewer's decisions and the notifications (to the
provider that subscribed, and only for its requests), the inquiry, and at the end CDC, dbt, the
PDex resource in gold and the member reading their own decision. All 91 checks pass, and with
three faults put into the service, 15 of them failed. Validating the service's real answers found
one defect in CRD itself: an invariant that fails whenever an optional element is present, so the
service leaves the element out.

## What this does not show, and what would change for production

- **Terminology.** CPT and NUBC revenue codes are licensed, so nothing here checks them, and the
  NCPDP dispense as written code is published only as a stub; all three are reported as
  unverified. The NDC check depends on the FDA's current directory, which cannot be pinned, and
  tx.fhir.org is a public test server whose content changes (its NDC is from 2021). Nothing checks
  the Payer Type codes either: their code system is published without content. A production
  Patient Access API would need a terminology server of its own with the licensed code systems.
  The narratives here are generated one-line summaries, enough for `dom-6` but not a designed
  human-readable view. The validator covers a sample; the code check in dbt covers every
  resource, but only the code systems it has.
- **The API.** The authorization is a demonstration: a SMART authorization server written for
  this project (clients in a file, keys in memory, a simulated member login, no TLS) in front of
  gateways that decide what each client sees. It shows the rules (a member's app sees one patient;
  a provider only the members on its list who have not opted out, one at a time; another payer
  only the members whose consent names it), not a production identity provider. The Payer-to-Payer consent is checked when the match is made, not
  again at export. The legacy source has no clinical data, so the US Core data both APIs share is
  absent. The servers
  run on HAPI's embedded H2 database and are rebuilt on every publish, so their `lastUpdated` is
  the publish time; a production server would sit on a real database and receive only what
  changed. The prior authorization exchange (Da Vinci CRD, DTR and PAS) is the payer's side only,
  against a simulated EHR: FHIR without the X12 278, one service per request, no updates or
  cancellations, rules and a reviewer that demonstrate the flow rather than a medical policy, and
  subscriptions kept in memory.
- **Scale and concurrency.** DuckLake takes several writers and the current state is incremental,
  but everything still runs on one machine, the gold models are rebuilt in full on every build,
  and the tables are readable only through DuckDB. A deployment that other engines must read
  would need Iceberg or Delta, and a larger one incremental gold models and object storage for
  the Parquet files.
- **Operations.** A replication slot keeps the database log until it is consumed. The legacy
  database caps that retention at 1 GB, a check outside the loader alerts at 512 MB every 5
  minutes, and a drill in CI proves the alert, the loss of the slot past the cap and the recovery
  by a new snapshot. In production the alert would page someone instead of failing an Airflow run,
  the cap would be sized from disk and expected downtime, and a new snapshot of large tables would
  need planning, since it replaces the change history.
- **Reproducibility.** Both generators are pinned: the legacy generator anchors its dates to a
  reference date (`--as-of`) instead of the day it runs, and the Synthea script downloads a fixed
  release (v4.0.0, checked by SHA-256) and runs it with fixed seeds, a fixed reference date and a
  fixed end date (without the end date, Synthea simulates up to the moment it runs). The gold
  models use a pinned reference date (the dbt variable `as_of_date`) instead of `current_date`. What still
  varies is timing: load times, CDC latency and the activity simulator, which runs in real time
  without a fixed seed. Synthea
  leaves a few allergy details (CarePlan activities, a reaction severity) unstable between runs,
  in resource types no model reads.
- **The compiler.** It covers the FHIRPath features the shareable tests exercise, without the
  boundary functions. Real-world ViewDefinitions may use features beyond that set.
- **Identity resolution.** The variations are generated, so they are cleaner than real ones: one
  kind of error at a time per field, from short name lists. The nickname dictionary covers the
  generator's nicknames, so its benefit here is an upper bound. The reviewer is simulated from the
  ground truth, so the figures after review are an upper bound too: real reviewers make mistakes
  and take time. A production master patient index would add more sources and fields (phone,
  address lines, SSN fragments), an unmerge workflow, and people who review the uncertain cases.

## Lessons

1. **Know what is in your test data.** Injected issues with documented rates, and records you can
   identify independently, turn "the checks ran" into "the checks found what was there". They also
   expose the artifacts of your generator.
2. **Reconcile against the source, in full.** Both CDC bugs showed up in row-by-row comparisons of
   rebuilt state against the source, not in unit-level checks.
3. **Make tests prove they can fail.** A parity check or a validator that always passes is only
   evidence once you have seen it catch a deliberate error.
4. **Publish the uncomfortable numbers.** Column test coverage of 25.1% in gold, eight unimplemented
   experimental tests and a 14-second view are part of the result. Hiding them would make every
   other number less credible.
5. **Treat the data layer as the product.** An interoperability API can only be as reliable as the
   data behind it. Most of the effort, and most of the evidence, belongs there.

## Reproduce it

```bash
make up && make seed && make pbm && make synthea && make ingest && make dbt
make coverage conformance fhir-validate-carin fhir-validate-pdex
make fhir-up fhir-publish fhir-api-check
make review   # optional: rewrites the review decisions seed from the ground truth
```

Requirements and the CDC streaming demo are in the repository's README, and the Airflow setup is
in [architecture](architecture.md#orchestration). A
companion [migration checklist](migration-checklist.md) turns the practices above into steps you can
apply to another legacy migration.
