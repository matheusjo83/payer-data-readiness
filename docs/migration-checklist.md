# Legacy payer data migration checklist

A checklist for moving a legacy payer warehouse into a lakehouse whose data can back FHIR APIs,
analytics and AI. It comes from building this repository, so each item says why it matters and
where the repository shows it. Items marked **Not covered here** matter in practice but are outside
what this project demonstrates.

Use it as a starting point, not a standard. Adapt the items to your sources, regulations and
platform.

## 1. Baseline the source before moving anything

- [ ] **Inventory tables, keys and constraints actually enforced.** Legacy schemas often declare
  keys they never enforce, or enforce none. Your models have to handle what the data contains, not
  what the schema claims.
  *In this repo:* `legacy_db/init/01_schema.sql` (no foreign keys, dates as `VARCHAR(8)`).
- [ ] **List the known data issues and their expected rates.** Duplicates, invalid codes,
  unparseable dates, orphan records, broken references. Without a baseline you cannot tell whether
  the migration fixed, hid or introduced a problem.
  *In this repo:* the injection rates at the top of `legacy_db/seed/generate_legacy_data.py`.
- [ ] **Find records you can identify independently of the pipeline.** A set of known-bad records,
  found by a query or a manual review, lets you check detection against ground truth instead of a
  rate.
  *In this repo:* the orphan lines (`C9…`) and unknown members (`M9…`) traced in the [results](results.md#data-quality).
- [ ] **Check test data for artifacts of how it was produced.** Synthetic or masked data can create
  "issues" that are really side effects of generation. Investigate surprising rates before you
  report them.
  *In this repo:* the 25% outside-eligibility artifact, fixed in Phase 3 (see `docs/write-up.md`).

## 2. Ingest without losing changes

- [ ] **Choose the capture method on purpose.** Compare log-based CDC, triggers, timestamp
  watermarks and full snapshots against three questions: do you need deletes, can you change the
  source, and does every table have a reliable update timestamp?
  *In this repo:* log-based CDC with a Postgres replication slot (`ingestion/cdc_legacy.py`).
- [ ] **Start CDC before the initial snapshot.** Create the replication slot, or record the log
  position, before copying the tables, so changes committed during the copy are not lost.
  *In this repo:* `snapshot()` in `ingestion/cdc_legacy.py`.
- [ ] **Release source changes only after the target commits.** Read, write in one transaction
  together with the position you reached, and only then acknowledge. Store that position in the
  target so a crash between the two steps never applies a batch twice.
  *In this repo:* `peek` → DuckDB transaction with `bronze._cdc_state` → `pg_replication_slot_advance`.
- [ ] **Keep raw changes append-only and derive current state.** An append-only change log keeps
  history and lets you rebuild state after a bug; overwriting in place does not.
  *In this repo:* `bronze.legacy_<table>_changes` and the `legacy_current_state` macro.
- [ ] **Handle truncates, deletes and bulk writes.** Test a full reload (`TRUNCATE` + `COPY`), not
  only single-row changes. Bulk writes can share one log position across many rows.
  *In this repo:* `_op = 'T'` handling and the `_seq` tie-breaker.
- [ ] **Make both load paths agree on representation.** Snapshot and change paths can render the
  same value differently (for example, padded `CHAR(n)` values).
  *In this repo:* `normalize()` in `ingestion/cdc_legacy.py`.
- [ ] **Record every load.** Source, target, rows, start, end and status for each step. Reliability
  metrics can only be computed from what was logged.
  *In this repo:* `bronze._load_log` and `gold.load_reliability`.
- [ ] **Monitor replication lag and retained log.** A slot that nobody consumes keeps growing on
  the source database.
  **Not covered here** beyond the documented `drop-slot` command.

## 3. Reconcile continuously

- [ ] **Compare rebuilt state with the source, row by row.** Counts can match while values differ.
  Compare full rows in both directions (`EXCEPT ALL`).
  *In this repo:* the reconciliation run after every CDC test (reported in the [results](results.md#change-data-capture-latency-phase-2)).
- [ ] **Measure source-to-target latency from commit time.** Use the source commit timestamp and
  the arrival time, not the job schedule. Know whether latency comes from polling or from
  processing.
  *In this repo:* `gold.cdc_latency`.
- [ ] **Account for clock skew** when the source and the target run on different hosts.
  **Not covered here** (single machine).

## 4. Model in layers with explicit contracts

- [ ] **Separate raw, conformed and business layers.** Raw keeps what arrived; conformed
  deduplicates, types and decodes; business models answer questions.
  *In this repo:* bronze, silver (`dbt/models/staging`) and gold (`dbt/models/marts`).
- [ ] **Decide how to resolve duplicates, and record that it happened.** Pick a survivorship rule
  and keep a flag, so the deduplication stays visible downstream.
  *In this repo:* `stg_legacy__members` (latest version wins, `had_duplicates`).
- [ ] **Parse legacy encodings in one place.** Text dates, sentinel values such as `99991231` and
  single-character codes belong in the conformed layer, documented.
  *In this repo:* `stg_legacy__eligibility`, `stg_legacy__claims`.
- [ ] **Build the standard denominators.** For payers, member months are the base for utilization
  and cost metrics.
  *In this repo:* `gold.fct_member_months`.

## 5. Test data quality with intent

- [ ] **Split tests into errors and warnings on purpose.** Invariants that must hold (keys, allowed
  values) fail the build. Known source issues warn and are counted, so the pipeline keeps running
  and the issues stay visible.
  *In this repo:* `severity: warn` in `dbt/models/staging/legacy/_legacy__models.yml`.
- [ ] **Publish a scorecard of detected issues.** One table with each check, its count and its
  population.
  *In this repo:* `gold.dq_issue_summary`.
- [ ] **Keep failing records, flagged, instead of dropping them.** Downstream consumers can exclude
  them; auditors can still find them.
  *In this repo:* `has_known_member`, `is_within_eligibility` and `paid_exceeds_charged` in
  `gold.fct_claims`.
- [ ] **Measure documentation and test coverage against the real schema.** Compare documented and
  tested columns with the columns that exist in the database, not with the YAML.
  *In this repo:* `scripts/dbt_coverage.py` (`make coverage`).
- [ ] **Report coverage honestly.** Low column-level coverage is fine to publish if you can explain
  what is tested. Padding the metric with trivial tests makes it less useful.

## 6. Orchestrate and operate

- [ ] **Keep business logic out of the orchestrator.** Tasks should call the same commands you run
  by hand, so the pipeline can be run and debugged without the scheduler.
  *In this repo:* `airflow/dags/payer_lakehouse.py` with `BashOperator` calling scripts and
  `dbt build`.
- [ ] **Isolate pipeline dependencies from the orchestrator's.**
  *In this repo:* a separate virtualenv in `airflow/Dockerfile`.
- [ ] **Encode engine constraints in the scheduler.** If the storage allows a single writer, make
  writers queue (for example, with a one-slot pool) instead of failing.
  *In this repo:* the `lakehouse` pool.
- [ ] **Pin versions that share state.** Components that read and write the same files must agree
  on formats.
  *In this repo:* `requirements.txt`.
- [ ] **Define the failure policy.** Retries, which test severities fail a run, and who is alerted.
  *In this repo:* two retries per task, warnings do not fail `dbt_build`. Alerting is
  **not covered here**.
- [ ] **Production deployment, authentication and secrets management.**
  **Not covered here** (local `airflow standalone`, no login).

## 7. Map to FHIR without inventing data

- [ ] **Pick the target profiles early.** Know which implementation guide the API will follow (for
  payer data, often CARIN Blue Button, US Core and Da Vinci guides) and which elements it requires.
  *In this repo:* CARIN Blue Button STU 2.1.0 (the version CMS lists for the Patient Access API),
  declared in each resource's `meta.profile`. Measuring the gap first showed which missing elements
  were mapping work and which were data the legacy source did not have.
- [ ] **Never map an invalid source value to a plausible one.** Omit it, or use a data-absent
  reason or the standard's own "unknown" value when the profile requires the element, and keep the
  quality flag in the analytics layer.
  *In this repo:* invalid gender codes become `unknown` in `fhir_patient` (CARIN requires a gender)
  and stay counted in `gold.dq_issue_summary`.
- [ ] **Enforce FHIR JSON rules while building resources.** No null values, no empty arrays, valid
  date formats.
  *In this repo:* `json_merge_patch('{}', …)` in `dbt/models/marts/fhir/`.
- [ ] **Gate exports on required references.** If a resource requires a patient or a coverage, do
  not export records without them. Count what is held back and why.
  *In this repo:* the integrity filter in `fhir_explanation_of_benefit` (493 claims held back).
- [ ] **Use stable identifiers and consistent references** between resources.
  *In this repo:* `Patient/<member_id>`, `Coverage/cov-<eligibility_id>`, `Organization/payer` and
  `Organization/<provider_id>`.

## 8. Validate and prove parity

- [ ] **Round-trip the data through FHIR.** Flatten the generated resources back into tables and
  compare them with the source tables, row by row, in both directions.
  *In this repo:* `gold.fhir_view_parity` and `dbt/tests/assert_fhir_view_parity.sql`.
- [ ] **Use a standard to flatten FHIR.** SQL on FHIR ViewDefinitions make the flattening portable
  and reviewable, instead of ad hoc JSON queries.
  *In this repo:* `fhir/view_definitions/` compiled by `fhir/sof_duckdb/`.
- [ ] **Test your tools against the specification's own tests.**
  *In this repo:* `fhir/conformance.py` against the SQL on FHIR shared test suite.
- [ ] **Run the official validator on a reproducible sample.**
  *In this repo:* `fhir/validate.py` with the HL7 FHIR validator, against base FHIR R4 and against
  CARIN Blue Button 2.1.0, in CI.
- [ ] **Prove that each check can fail.** Run parity checks and validators against deliberately
  broken input and confirm they report the expected number of failures.
  *In this repo:* the sabotage runs reported in the [results](results.md#fhir-phases-4-and-6).
- [ ] **Validate against the target profiles, with references resolved.** Validate each resource
  together with the resources it references, so the referenced resources are checked against their
  target profiles too, and check separately that every reference resolves: the HL7 validator does
  not report an unresolved reference inside a Bundle as an error.
  *In this repo:* `make fhir-validate-carin` (Bundles) and `dbt/tests/assert_fhir_references_resolve.sql`.
- [ ] **Validate codes with a terminology server.**
  **Not covered here** (`-tx n/a`). Without a server, clear the validator's terminology cache, or
  the result depends on what earlier runs cached.

## 9. Report results people can trust

- [ ] **Tie every published number to a query or a command** that reproduces it.
  *In this repo:* the [results](results.md) page names the model or `make` target for each figure.
- [ ] **State what the numbers do not show.** Synthetic data, single machine, validation scope.
- [ ] **Update published numbers when the code changes them**, and say why they changed.
  *In this repo:* the Phase 3 note in the [results](results.md).
- [ ] **Make runs reproducible.** Fixed seeds, pinned tool versions and a single command sequence.
  *In this repo:* seed 42, a fixed reference date, pinned Python packages, Synthea and HL7 validator
  versions, and the Synthea download checked by SHA-256.

## 10. Govern the data

- [ ] **Keep real PHI out of development and demos.** Use synthetic or properly de-identified data.
  *In this repo:* all data is synthetic (Synthea and the legacy generator).
- [ ] **Document lineage from source to consumption.**
  *In this repo:* dbt docs (`make docs`).
- [ ] **Access control, audit logging, retention and consent.**
  **Not covered here.**
