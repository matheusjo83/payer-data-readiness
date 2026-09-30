# Payer Data Readiness Lakehouse

[![CI](https://github.com/matheusjo83/payer-data-readiness/actions/workflows/ci.yml/badge.svg)](https://github.com/matheusjo83/payer-data-readiness/actions/workflows/ci.yml)

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

**Read more:** the [technical write-up](docs/write-up.md) covers the design decisions, results and
limitations, and the [migration checklist](docs/migration-checklist.md) turns them into steps for
other legacy migrations.

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
- **Documentation.** Claude Code drafted the Results section (now in `docs/results.md`) and the
  README sections added from Phase 2 on (now in `docs/architecture.md`), the technical
  write-up and the migration checklist, in the language, format and voice Matheus chose. Matheus
  reviewed the drafts and checked facts independently, including whether CMS requires or
  recommends the CARIN Blue Button guide.
- **Git history.** Matheus made the commits and merged the pull requests, so git lists him as the
  author of every commit. Commit authorship records who committed a change, not who wrote the code.

## Architecture

```
Synthea (synthetic FHIR R4) ──── NDJSON load ──┐
                                               ├─► Bronze ─► Silver ─► Gold ─► SQL on FHIR views
Legacy payer DB (Postgres) ──── CDC (WAL) ─────┤   (raw)    (tested)  (models)  Quality & lineage
                                (logged)       │                      Identity resolution
PBM files (pipe-delimited) ──── file load ─────┘
          └──── orchestrated by Airflow (CDC sync, FHIR and PBM loads, dbt build, coverage) ────┘
```

| Layer  | Location (DuckDB)         | Contents                                                 |
|--------|---------------------------|----------------------------------------------------------|
| Bronze | `bronze.*`                | Legacy change tables (CDC), raw FHIR JSON, PBM files, load log |
| Silver | `silver.*` (dbt staging)  | Current state of each source: deduplicated, typed, tested |
| Gold   | `gold.*` (dbt marts)      | Dimensions, facts, identity cross-reference, quality scorecard, reliability metrics, FHIR resources |

How each component works (change data capture, orchestration, SQL on FHIR, the legacy-to-FHIR
mapping, identity resolution) is described in [docs/architecture.md](docs/architecture.md).

## Stack

Runs fully local, at zero cost: **Postgres** (legacy source, Docker, with the **wal2json** logical
decoding plugin for CDC), **DuckDB** (lakehouse engine), **dbt** (transformations, tests, lineage),
**Synthea** (synthetic FHIR data), **Splink** (probabilistic record linkage), **Python** (ingestion),
**Apache Airflow** (orchestration, Docker).

## Quickstart

Requirements: Docker, Python 3.11+, Java 17+ (for Synthea), `make`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make up        # build and start the legacy Postgres database
make seed      # generate legacy data with deliberate quality issues
make pbm       # generate the PBM feed: the same members under other IDs, with known variations
make synthea   # generate synthetic FHIR R4 data (bulk NDJSON)
make ingest    # CDC snapshot of the legacy tables + FHIR and PBM loads into the bronze layer
make dbt       # build silver/gold layers and run data tests
make coverage  # documentation and test coverage of the dbt models
make docs      # browse models and the lineage graph
```

FHIR (Phases 4 and 6):

```bash
make conformance          # run the official SQL on FHIR v2 test suite against the view compiler
make views                # recompile fhir/view_definitions/*.json into dbt macros (after editing a view)
make fhir-validate-carin  # validate a sample against the CARIN Blue Button 2.1.0 profiles (HL7 validator, Java)
make fhir-validate        # validate a sample against base FHIR R4 only
```

The legacy schema gained columns in Phase 6. A database created before that needs `make reset`
(which deletes the local data) and the Quickstart again.

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

**Continuous integration.** On every pull request and push to `main`, GitHub Actions
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs `make up seed pbm synthea ingest dbt
conformance fhir-validate-carin` on a clean runner. The run fails if a model or data test fails, if
any shareable SQL on FHIR test fails, or if the HL7 validator reports an error against the CARIN
Blue Button profiles in the sample.

Some data tests are configured as **warnings** on purpose: they flag issues that exist in the
legacy source. The `gold.dq_issue_summary` model counts them.

## Results at a glance

Figures for the reference date 2026-09-28, from synthetic data. Every figure, where it is measured
and how to reproduce it are in [docs/results.md](docs/results.md).

| Area                | Result                                                                                           |
|---------------------|--------------------------------------------------------------------------------------------------|
| Data quality        | 312 of 312 injected orphan claim lines and 242 of 242 claims with an unknown member detected      |
| Change data capture | 640 streamed changes, median latency 2.16 s with 5-second polling; rebuilt state matched the source row for row |
| SQL on FHIR         | 133 of 133 shareable tests of the specification's test suite passed                              |
| Legacy data as FHIR | 24,507 ExplanationOfBenefit resources round-tripped with zero differing rows                     |
| Identity resolution | PBM cardholders linked to members with 100% precision and 98.2% recall against ground truth (deterministic baseline: 99.5% and 93.8%); 69 uncertain matches sent to review |
| FHIR validation     | 0 errors on a 1,400-resource sample against the CARIN Blue Button 2.1.0 profiles, references resolved (terminology not checked); 1,000 of 1,000 failed before remediation |

## Roadmap

- [x] **Phase 1 – Foundation:** legacy source, synthetic data, bronze loaders, first silver/gold models
- [x] **Phase 2 – Ingestion:** change data capture from the legacy database and latency metrics
- [x] **Phase 3 – Modeling:** complete silver/gold layers (eligibility, providers, plans), Airflow orchestration
- [x] **Phase 4 – FHIR:** run SQL on FHIR ViewDefinitions, compare with hand-written models, map legacy data to FHIR-aligned outputs
- [x] **Phase 5 – Dissemination:** technical write-up and reusable migration checklist
- [x] **Phase 6 – Profile conformance:** CARIN Blue Button 2.1.0 profiles (baseline, remediation, Organization resources, reference checks), validated in CI
- [x] **Phase 7 – Identity resolution:** a PBM feed with the same members under other IDs, probabilistic matching (Splink) against a deterministic baseline, measured against ground truth, through to FHIR

## Project structure

```
legacy_db/     Legacy schema, Postgres image (wal2json), data generator and activity simulator
scripts/       Synthea download and configuration, PBM feed generator, dbt coverage report
ingestion/     Bronze-layer loaders (CDC for legacy, NDJSON for FHIR, files for the PBM) with load logging
dbt/           Silver and gold models (including the Splink Python model), seeds, tests, lineage
airflow/       Airflow image and DAGs
fhir/          SQL on FHIR ViewDefinitions, DuckDB compiler, conformance runner, HL7 validation (base and CARIN)
docs/          Technical write-up, migration checklist, architecture details and results
.github/       CI workflow (runs the Quickstart and the FHIR checks on every pull request)
```

## Author

Matheus Julio de Oliveira, data engineering specialist focused on data platform modernization.

## License

MIT
