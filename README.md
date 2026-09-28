# Payer Data Readiness Lakehouse

A reproducible reference implementation of how a health plan (payer) can modernize a legacy
analytics warehouse into a governed lakehouse, so that the data behind interoperability APIs,
analytics and AI is **reliable, timely and traceable**.

> All data in this project is **synthetic**. No real patient, member or claim data is used.

## Why this matters

Federal interoperability rules, such as CMS-0057-F (Interoperability and Prior Authorization),
require many payers to expose claims, coverage and prior authorization data through FHIR APIs.
An API is only as trustworthy as the data behind it. In practice, that data often lives in legacy
warehouses with duplicated records, unenforced relationships and unparseable dates.

This project demonstrates the data layer underneath those APIs:

1. **Migrate** a legacy payer warehouse into a lakehouse (bronze, silver, gold).
2. **Measure and remediate** data quality with automated tests.
3. **Trace** every dataset from source to consumption (lineage).
4. **Bridge to FHIR**: flatten FHIR resources into analytics-ready tables using the
   [SQL on FHIR v2](https://build.fhir.org/ig/FHIR/sql-on-fhir-v2/) `ViewDefinition` specification.

## Architecture

```
Synthea (synthetic FHIR R4) ──── NDJSON load ──┐
                                               ├─► Bronze ─► Silver ─► Gold ─► SQL on FHIR views
Legacy payer DB (Postgres) ──── CDC (WAL) ─────┘   (raw)    (tested)  (models)  Quality & lineage
                                (logged)
          └──────────── orchestrated by Airflow (CDC sync, FHIR load, dbt build, coverage) ─────┘
```

| Layer  | Location (DuckDB)         | Contents                                                 |
|--------|---------------------------|----------------------------------------------------------|
| Bronze | `bronze.*`                | Legacy change tables (CDC), raw FHIR JSON, load log      |
| Silver | `silver.*` (dbt staging)  | Current state of each source: deduplicated, typed, tested |
| Gold   | `gold.*` (dbt marts)      | Dimensions, facts, quality scorecard, reliability metrics |

Gold models: `dim_member`, `dim_plan`, `dim_provider`, `fct_claims`, `fct_member_months`,
`fct_prior_auth_timeliness`, plus the quality and reliability models `dq_issue_summary`,
`load_reliability` and `cdc_latency`.

## Stack

Runs fully local, at zero cost: **Postgres** (legacy source, Docker, with the **wal2json** logical
decoding plugin for CDC), **DuckDB** (lakehouse engine), **dbt** (transformations, tests, lineage),
**Synthea** (synthetic FHIR data), **Python** (ingestion), **Apache Airflow** (orchestration, Docker).

## Quickstart

Requirements: Docker, Python 3.11+, Java 17+ (for Synthea), `make`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make up        # build and start the legacy Postgres database
make seed      # generate legacy data with deliberate quality issues
make synthea   # generate synthetic FHIR R4 data (bulk NDJSON)
make ingest    # CDC snapshot of the legacy tables + FHIR load into the bronze layer
make dbt       # build silver/gold layers and run data tests
make coverage  # documentation and test coverage of the dbt models
make docs      # browse models and the lineage graph
```

To watch change data capture at work, run the loader and the activity simulator side by side,
then rebuild the models:

```bash
make cdc       # terminal 1: stream legacy changes into bronze every 5 seconds (Ctrl+C to stop)
make changes   # terminal 2: 300 inserts/updates/deletes in the legacy database over 60 seconds
make dbt       # after stopping `make cdc`: refresh silver/gold, including gold.cdc_latency
```

DuckDB allows one writer at a time. The CDC loader opens the lakehouse only while it writes a
batch and retries on the next poll if the file is busy, but `make dbt` can still fail if it starts
in the middle of a write; stopping `make cdc` first avoids that.

Some data tests are configured as **warnings** on purpose: they flag issues that exist in the
legacy source. The `gold.dq_issue_summary` model counts them.

## The simulated legacy source

`legacy_db/init/01_schema.sql` reproduces typical legacy traits: cryptic names, dates stored as
`VARCHAR(8)`, single-character codes and no foreign keys. The generator injects known issues at
documented rates (duplicate members, invalid gender codes, unparseable birth dates, claims for
unknown members, paid amounts above charges, orphan claim lines, claims with a service date
outside the member's coverage), so detection can be verified.

`legacy_db/seed/simulate_changes.py` generates day-to-day activity, one transaction per event:
prior authorization decisions and new requests, claim adjudication, new claims, member address
changes and deletion of voided claims.

## Change data capture

`ingestion/cdc_legacy.py` reads the legacy database's write-ahead log through a logical
replication slot (wal2json). It does not change the legacy tables and it captures deletes.

- **Initial snapshot.** The slot is created before the tables are copied, so no change committed
  during the copy is lost. Changes that are both in the copy and in the slot are replayed on top of
  the copied rows; each change carries the full new row, so the final state is the same.
- **Bronze change tables.** Each legacy table has an append-only `bronze.legacy_<table>_changes`
  table: one row per change (`_op` = S snapshot, I insert, U update, D delete, T truncate), with the
  log position (`_lsn`, plus `_seq` for rows that a bulk write such as `COPY` puts in one WAL record),
  the source commit time (`_commit_ts`) and the arrival time (`_loaded_at`).
- **No lost or repeated batches.** Changes are read without being consumed (`peek`), written to
  DuckDB in one transaction together with the last commit LSN (`bronze._cdc_state`), and only then
  released from the slot (`advance`). If the loader stops between the two steps, the next run skips
  the batch that was already stored. A dbt test checks that no log position (`_lsn`, `_seq`) appears
  twice.
- **Current state.** Silver models rebuild each table's current state with the
  `legacy_current_state` macro: the latest version of each primary key after the last TRUNCATE,
  without deleted keys. Rows that share an `_lsn` are ordered by `_seq`.
- **Operational note.** A replication slot makes Postgres keep WAL until it is consumed. If the
  loader is stopped for a long time, run `python ingestion/cdc_legacy.py drop-slot`; the next
  `make ingest` takes a new snapshot.

## Orchestration

`airflow/dags/payer_lakehouse.py` defines two DAGs, run by Airflow 3 in Docker
(`make airflow-up`, UI at http://localhost:8080, no login in this local setup):

| DAG                      | Schedule          | Tasks                                                   |
|--------------------------|-------------------|---------------------------------------------------------|
| `legacy_cdc_refresh`     | every 30 minutes  | `cdc_sync` → `dbt_build`                                |
| `lakehouse_full_refresh` | manual            | `cdc_sync` → `load_fhir` → `dbt_build` → `dbt_coverage` |

Both DAGs start paused. Design choices:

- **Airflow only orchestrates.** Tasks call the same scripts and `dbt build` used by `make`,
  through `BashOperator`. The loaders and dbt run in their own virtualenv inside the image, so
  their dependencies never conflict with Airflow's. Versions are pinned in `requirements.txt`,
  because the host and the container write the same DuckDB file.
- **One writer at a time.** Every task runs in the `lakehouse` pool, which has one slot: tasks
  from both DAGs wait for each other instead of failing on a locked DuckDB file. Do not run the
  `make` targets that write to the lakehouse while Airflow is running tasks.
- **Failure policy.** Each task retries twice. Data tests configured as warnings (known legacy
  issues) do not fail `dbt_build`; test errors do.
- **Local setup.** `airflow standalone` with the LocalExecutor and a separate Postgres for Airflow
  metadata. A production deployment would run the scheduler, API server and workers as separate
  services with real authentication.

## Metrics

| Indicator                              | Where it is measured                    | Status      |
|----------------------------------------|-----------------------------------------|-------------|
| Load success rate, volume and duration | `gold.load_reliability`                 | Phase 1, 2  |
| Known data-quality issues detected     | `gold.dq_issue_summary`                 | Phase 1, 3  |
| Prior authorization decision timeliness| `gold.fct_prior_auth_timeliness`        | Phase 1     |
| Source-to-lakehouse latency (CDC)      | `gold.cdc_latency`                      | Phase 2     |
| Test coverage and documented lineage   | `make coverage` (dbt artifacts)         | Phase 3     |
| FHIR view parity (hand-written vs. ViewDefinition) | `fhir/view_definitions/`    | Phase 4     |

## Results

Figures from 2026-09-28. All figures below come from synthetic data: the legacy source was
generated by `legacy_db/seed/generate_legacy_data.py` with its default fixed seed (42) and
Synthea was run with seed 42 (population 500). They describe how the pipeline behaves on that
data, not the performance of any real payer. Running the Quickstart commands produces them
again; see the reproducibility note at the end of this section.

In Phase 3 the generator changed: claims now fall inside the member's coverage, except for the
injected claims outside eligibility. This changed the random sequence, so the claim and prior
authorization figures differ from the ones published in Phase 1 (see the git history).

### Data quality

From `gold.dq_issue_summary`. Member checks run on the deduplicated member table (5,000 members
from 5,094 source rows).

| Check                        | Detected | Population      | Detected % | Injection rate in generator |
|------------------------------|---------:|-----------------|-----------:|-----------------------------|
| Duplicate member IDs         |       94 | 5,000 members   |      1.88% | 1.5% of members             |
| Invalid gender code          |       51 | 5,000 members   |      1.02% | 1.0% of members             |
| Unparseable birth date       |       44 | 5,000 members   |      0.88% | 1.0% of members             |
| Claims with unknown member   |      242 | 25,000 claims   |      0.97% | 1.0% of claims              |
| Paid amount exceeds charged  |      405 | 25,000 claims   |      1.62% | 2.0% of paid claims         |
| Claims outside eligibility   |      251 | 24,758 claims   |      1.01% | 1.0% of claims              |
| Orphan claim lines           |      312 | 62,795 lines    |      0.50% | 0.5% of lines               |

Injection rates are probabilities, so observed counts differ from the nominal rate by random
variation. The paid-exceeds-charged rate applies only to paid claims: 405 of 20,173 paid
claims (2.01%). The outside-eligibility check applies only to claims whose member exists
(24,758), because claims with an unknown member have no coverage to compare with.

**Traceability of injected issues.** For two checks, the injected records can be identified
in bronze independently of the dbt models, because the generator assigns them IDs outside the
valid ranges:

- *Orphan claim lines:* the generator adds `int(62,483 × 0.005) = 312` lines whose claim IDs
  start with `C9`, which never matches a real header. The check flagged 312 lines, all 312 of
  them injected (312/312, no other lines flagged).
- *Claims with unknown member:* injected claims reference member IDs from `M900000000` up,
  while real members run from `M000000001` to `M000005000`. Bronze has 242 such claims (0.97%
  of 25,000, against a nominal 1.0%). The check flagged 242 claims, all 242 of them injected.

The other five checks have no independent ground truth in the data, so only their counts are
reported.

### Prior authorization timeliness

From `gold.fct_prior_auth_timeliness`, measured against the CMS-0057-F timeframes. Pending
requests have no decision timestamp and are left out of the timing figures.

| Request type | Requests | Decided | Pending | Mean decision time | Median decision time | Timeframe       | Within timeframe       |
|--------------|---------:|--------:|--------:|-------------------:|---------------------:|-----------------|------------------------|
| Expedited    |      638 |     612 |      26 |             30.8 h |               21.1 h | 72 h            | 557 of 612 (91.0%)     |
| Standard     |    2,362 |   2,241 |     121 |             85.9 h |               59.9 h | 168 h (7 days)  | 1,927 of 2,241 (86.0%) |

The generator draws decision times from exponential distributions (means of 30 h for
expedited and 90 h for standard requests). These figures show that the model computes the
metric correctly; they say nothing about how any real organization performs.

### Bronze load reliability

From `bronze._load_log` (the source of `gold.load_reliability`), for the loads of a Quickstart
run: the CDC snapshot of the legacy tables and the FHIR load.

| Source                          | Loads | Successful | Rows loaded | Total load time |
|---------------------------------|------:|-----------:|------------:|----------------:|
| Legacy (Postgres, CDC snapshot) |     7 |          7 |     101,194 |          0.18 s |
| Synthea (FHIR)                  |    20 |         20 |     589,293 |          5.14 s |
| **Total**                       |    27 |         27 |     690,487 |          5.32 s |

Each table and resource type was loaded once, so the 100% success rate comes from a single run
and does not yet show a trend. The largest load was `Observation` (224,817 resources).

### Change data capture latency (Phase 2)

Run on 2026-09-28: a fresh snapshot, then `make cdc` (5-second polling) running while
`make changes` committed 300 events over 60 seconds. From `gold.cdc_latency`; latency is the time
between the commit in Postgres and the arrival of the change in bronze.

| Table      | Changes | Inserts | Updates | Deletes | p50 latency | p95 latency | Max latency |
|------------|--------:|--------:|--------:|--------:|------------:|------------:|------------:|
| `clm_ln`   |     340 |     189 |     117 |      34 |      2.06 s |      4.86 s |      5.05 s |
| `clm_hdr`  |     143 |      76 |      53 |      14 |      2.13 s |      4.86 s |      5.05 s |
| `pa_req`   |     121 |      49 |      72 |       0 |      2.50 s |      4.68 s |      4.97 s |
| `mbr_mstr` |      36 |       0 |      36 |       0 |      2.86 s |      4.53 s |      4.86 s |

Across all 640 changes the median latency was 2.16 s and the maximum 5.05 s, which is what a
5-second polling interval implies: latency here is set by the polling interval, not by processing
time. The 640 changes arrived in 27 batches; all 77 table writes succeeded (`bronze._load_log`),
with an average write time of 0.03 s. After the run, the current state rebuilt from the change
tables matched the Postgres tables row for row in all seven tables.

Both processes ran on the same machine, so the source and lakehouse clocks agree; with separate
hosts, clock skew would add to the measured latency.

### Documentation and test coverage (Phase 3)

From `make coverage`, which reads the dbt manifest (descriptions and tests) and catalog (the
columns that exist in the database).

| Layer  | Models | Models documented | Models with tests | Columns | Columns documented | Columns with tests |
|--------|-------:|------------------:|------------------:|--------:|-------------------:|-------------------:|
| Silver |      9 |              100% |              100% |      67 |               100% |              41.8% |
| Gold   |      9 |              100% |              100% |      79 |               100% |              17.7% |

Every model and column has a description, and every model has at least one test. Most columns
have no test of their own: tests cover keys, relationships, accepted values and the quality
checks, not descriptive attributes such as names or amounts. `dbt build` ran 87 nodes: 82
passed, 5 ended with the expected warnings for the legacy issues above, and none failed.
`lakehouse_full_refresh` ran the same steps in Airflow with the same result.

### Reproducibility

The generator anchors dates to the day it runs (`date.today()`), and `scripts/generate_synthea.sh`
downloads the latest Synthea build. Running it on the same day with the same Synthea build gives
the same results. Running it on a different day, or with a newer Synthea build, can change the
claim, prior authorization and FHIR counts. The checks and metric definitions stay the same.
The CDC latency figures depend on timing and on the simulator, which runs without a fixed seed
by default, so a new run gives similar but not identical numbers.

## Roadmap

- [x] **Phase 1 – Foundation:** legacy source, synthetic data, bronze loaders, first silver/gold models
- [x] **Phase 2 – Ingestion:** change data capture from the legacy database and latency metrics
- [x] **Phase 3 – Modeling:** complete silver/gold layers (eligibility, providers, plans), Airflow orchestration
- [ ] **Phase 4 – FHIR:** run SQL on FHIR ViewDefinitions, compare with hand-written models, map legacy data to FHIR-aligned outputs
- [ ] **Phase 5 – Dissemination:** technical write-up and reusable migration checklist

## Project structure

```
legacy_db/     Legacy schema, Postgres image (wal2json), data generator and activity simulator
scripts/       Synthea download and configuration, dbt coverage report
ingestion/     Bronze-layer loaders (CDC for legacy, NDJSON for FHIR) with load logging
dbt/           Silver and gold models, tests, lineage
airflow/       Airflow image and DAGs
fhir/          SQL on FHIR ViewDefinitions
```

## Author

Matheus Julio de Oliveira, data engineering specialist focused on data platform modernization.

## License

MIT
