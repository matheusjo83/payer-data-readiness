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
  5-second polling.
- **SQL on FHIR on DuckDB.** Claude Code wrote a compiler from SQL on FHIR v2 ViewDefinitions to DuckDB
  SQL.
  It passes all 133 shareable tests of the specification's shared test suite.
- **Legacy data as FHIR, checked two ways.** The legacy data becomes 5,000 Patient, 5,000 Coverage
  and 24,507 ExplanationOfBenefit resources. Round trips through FHIR reproduce the source tables
  with zero differing rows, and the HL7 validator reports zero errors on a 600-resource sample
  (against base FHIR R4; terminology not checked).

These figures describe synthetic data generated with a fixed seed. They show that the pipeline
behaves as designed. They do not describe any real health plan.

## Architecture

```
Synthea (synthetic FHIR R4) ──── NDJSON load ──┐
                                               ├─► Bronze ─► Silver ─► Gold ─► SQL on FHIR views
Legacy payer DB (Postgres) ──── CDC (WAL) ─────┘   (raw)    (tested)  (models)  Quality & lineage
          └──────────── orchestrated by Airflow (CDC sync, FHIR load, dbt build, coverage) ─────┘
```

The stack is Postgres for the legacy source (with the wal2json plugin), DuckDB as the lakehouse
engine, dbt for transformations, tests and lineage, Synthea for synthetic FHIR data, Python for
ingestion, and Apache Airflow for orchestration. I chose it, with help from Claude Code, so that anyone
can run the whole project with `make` and Docker, at no cost. The trade-offs of that choice come up throughout, most
of all DuckDB's single writer.

It was built in five phases, each merged as its own pull request: foundation, change data capture,
modeling and orchestration, FHIR, and this write-up. I wrote the first phase; Claude Code implemented
phases 2 to 4 and drafted this write-up.

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
`gold.dq_issue_summary` reports the counts. The final build ran 128 nodes: 123 passed and 5 ended
in the expected warnings.

For coverage, a small script written by Claude Code combines the dbt manifest (descriptions and tests) with
the catalog (the columns that actually exist in the database). A column that exists but is missing
from the documentation counts against coverage. Every model and column is documented, and every
model has at least one test. Column-level test coverage is 41.8% in silver and 24.3% in gold. I
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
manual full refresh that also loads FHIR, checks the ViewDefinitions and reports coverage. Three
decisions shaped it:

- **Same commands as `make`.** Tasks call the same scripts and `dbt build` through `BashOperator`.
  The loaders and dbt live in their own virtualenv inside the image, so their dependencies never
  conflict with Airflow's.
- **One writer at a time.** DuckDB allows a single writer. Every task that writes the lakehouse
  runs in an Airflow pool with one slot, so tasks from both DAGs queue instead of failing on a
  locked file.
- **Pinned versions.** The host and the container write the same DuckDB file. A newer DuckDB in
  the container could write a storage format the host cannot read, so the versions are pinned.

The single-writer constraint is the clearest limit of this stack. It is fine for a reference
project. For concurrent pipelines at scale it would be the first thing to replace.

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

The last step turns the legacy warehouse into FHIR R4 resources aligned with the CARIN Blue Button
profiles: Patient, Coverage and ExplanationOfBenefit. "Aligned" is deliberate. The resources carry
the elements those profiles center on, but they were validated against base FHIR R4, not against the
CARIN profiles.

Three rules guided the mapping:

- **Don't invent data.** Legacy gender codes outside M/F/U are omitted rather than mapped to
  `unknown`, which would hide the fact that the source value was invalid. FHIR does not allow null
  values or empty arrays, so the SQL that builds each resource strips them.
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
a human-readable narrative, which these resources do not. As with the parity test, Claude Code checked
that the validator fails when it should. It rejected hand-made resources with the legacy problems the mapping avoids: a
null value, gender `invalid`, a `YYYYMMDD` date, an empty array and missing required elements.

## What this does not show, and what would change for production

- **Profile conformance and terminology.** Validation used base FHIR R4 without a terminology
  server, so it does not check the CARIN profiles or whether CPT and ICD-10-CM codes exist. A
  production Patient Access API would need both, plus narratives.
- **Scale and concurrency.** DuckDB's single writer shapes the orchestration. At scale, the next step
  would be to move to a table format with concurrent writers, such as Iceberg or Delta, and materialize the CDC
  current state incrementally instead of rebuilding it with a window function over all changes.
- **Operations.** A replication slot keeps the database log until it is consumed. The project
  documents how to drop the slot, but production needs monitoring and alerts on slot lag.
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

## Lessons

1. **Know what is in your test data.** Injected issues with documented rates, and records you can
   identify independently, turn "the checks ran" into "the checks found what was there". They also
   expose the artifacts of your generator.
2. **Reconcile against the source, in full.** Both CDC bugs showed up in row-by-row comparisons of
   rebuilt state against the source, not in unit-level checks.
3. **Make tests prove they can fail.** A parity check or a validator that always passes is only
   evidence once you have seen it catch a deliberate error.
4. **Publish the uncomfortable numbers.** Column test coverage of 24%, eight unimplemented
   experimental tests and a 14-second view are part of the result. Hiding them would make every
   other number less credible.
5. **Treat the data layer as the product.** An interoperability API can only be as reliable as the
   data behind it. Most of the effort, and most of the evidence, belongs there.

## Reproduce it

```bash
make up && make seed && make synthea && make ingest && make dbt
make coverage conformance fhir-validate
```

Requirements and the CDC streaming demo are in the repository's README, and the Airflow setup is
in [architecture](architecture.md#orchestration). A
companion [migration checklist](migration-checklist.md) turns the practices above into steps you can
apply to another legacy migration.
