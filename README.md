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
  (Splink compared with a deterministic baseline) and the scope (through FHIR). The replication
  slot monitoring that followed v2.0.0 was built the same way; Matheus chose where to measure and
  alert (history in the lakehouse plus a separate check) and to cap the WAL the slot can hold.
  After v2.0.0, Matheus also chose how to check codes (tx.fhir.org in the CARIN validation, plus
  the public CMS and FDA code lists checked in dbt against every resource, with CPT left
  unverified because it is licensed) and how to finish identity resolution (reviewer decisions in
  a versioned dbt seed applied over the model, with an audit trail and a simulated reviewer, and a
  second Splink model that deduplicates the PBM's own cardholders). When the merged CI run still
  showed warnings, Matheus asked for the project to be completely clean and chose to treat every
  warning Claude Code traced, rather than only document them. For the FHIR API, Matheus chose a
  HAPI FHIR server loaded from gold, prior authorizations as Da Vinci PDex ExplanationOfBenefit
  resources (STU 2.1.0, without PAS), and, when the prior authorization data fell short of the
  profile, to fix the legacy generator (requests inside the member's coverage, a requested
  amount) and to code denials with X12's A3, which no open source could confirm. For the
  storage, Matheus chose DuckLake (a Postgres catalog with Parquet data) and an incremental CDC
  current state merged by key from an `_lsn` watermark, with deletes applied. After v3.0.0,
  Matheus chose the Provider Access and Payer-to-Payer APIs as the next step (keeping the other
  options in the backlog below) and, for them, a gateway in front of HAPI for the Da Vinci PDex
  operations HAPI lacks, the claims without financial data as a gold model served from a second
  HAPI server, the opt-outs as a legacy table captured by CDC, and PDex 2.1.0. For API security,
  Matheus chose a minimal SMART authorization server written for the project over Keycloak, the
  enforcement in the gateway over HAPI interceptors, and, for a provider's plain search, that it
  must name one member on the provider's list who has not opted out.
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
                                (logged)       │                 │    Identity resolution
PBM files (pipe-delimited) ──── file load ─────┘                 │
                                                                 └─ publish ─► HAPI FHIR server
                                                                 │             (Patient Access: search, Bulk $export)
                                                                 └─ publish ─► HAPI FHIR server ◄─ FHIR gateway
                                                                               (Provider Access, Payer-to-Payer:
                                                                                no financial data; PDex operations)
          └─── orchestrated by Airflow (CDC sync, slot monitor, FHIR and PBM loads, dbt, coverage) ───┘
```

| Layer  | Location (DuckLake)       | Contents                                                 |
|--------|---------------------------|----------------------------------------------------------|
| Bronze | `bronze.*`                | Legacy change tables (CDC), raw FHIR JSON, PBM files, load log |
| Silver | `silver.*` (dbt staging)  | Current state of each source (incremental for the legacy tables): deduplicated, typed, tested |
| Gold   | `gold.*` (dbt marts)      | Dimensions, facts, identity cross-reference, quality scorecard, reliability metrics, FHIR resources |

The lakehouse is [DuckLake](https://ducklake.select/): its catalog is a Postgres database and its
data is Parquet files in `data/lakehouse/`, so several processes can write at once. It is not
Iceberg or Delta Lake (see [docs/architecture.md](docs/architecture.md#the-lakehouse-ducklake)).

How each component works (change data capture, orchestration, SQL on FHIR, the legacy-to-FHIR
mapping, identity resolution) is described in [docs/architecture.md](docs/architecture.md).

## Stack

Runs fully local, at zero cost: **Postgres** (legacy source, Docker, with the **wal2json** logical
decoding plugin for CDC), **DuckDB** with **DuckLake** (lakehouse engine and format: a Postgres
catalog, Parquet data), **dbt** (transformations, tests, lineage),
**Synthea** (synthetic FHIR data), **Splink** (probabilistic record linkage), **Python** (ingestion),
**Apache Airflow** (orchestration, Docker), **HAPI FHIR** (the FHIR APIs, Docker), and a small
**Python** gateway (standard library) for the Da Vinci PDex operations HAPI does not implement.

## Quickstart

Requirements: Docker, Python 3.11+, Java 17+ (for Synthea), `make`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make up        # build and start the legacy Postgres database and the lakehouse catalog (Postgres)
make seed      # generate legacy data with deliberate quality issues
make pbm       # generate the PBM feed: the same members under other IDs, with known variations
make synthea   # generate synthetic FHIR R4 data (bulk NDJSON)
make ingest    # CDC snapshot of the legacy tables + FHIR, PBM and code system loads into the bronze layer
make dbt       # build silver/gold layers and run data tests
make review    # optional: work the identity review queue as a simulated reviewer (rewrites the decisions seed)
make coverage  # documentation and test coverage of the dbt models
make docs      # browse models and the lineage graph
make docs-static  # write the dbt docs as one static page (dbt/static_index.html)
```

FHIR (Phases 4 and 6):

```bash
make conformance          # run the official SQL on FHIR v2 test suite against the view compiler
make views                # recompile fhir/view_definitions/*.json into dbt macros (after editing a view)
make fhir-validate-carin  # validate a sample against the CARIN Blue Button 2.1.0 profiles, codes checked by tx.fhir.org (HL7 validator, Java)
make fhir-validate-pdex   # validate the prior authorizations against Da Vinci PDex 2.1.0 (HL7 validator, Java)
make fhir-validate-provider  # validate the Provider Access data: claims without financial data (CARIN Basis), attribution lists and opt-outs (PDex)
make fhir-validate        # validate a sample against base FHIR R4 only
```

FHIR APIs:

```bash
make fhir-up              # start the FHIR servers empty, the SMART authorization server (http://localhost:8093)
                          #   and the gateways: Patient Access at http://localhost:8090/fhir,
                          #   Provider Access and Payer-to-Payer at http://localhost:8092/fhir
make fhir-publish         # register the SMART clients (make fhir-clients) and load the gold FHIR resources into both
make fhir-api-check       # check the Patient Access server against gold: counts, CARIN/PDex searches, Bulk $export
make fhir-provider-api-check  # check the gateway against gold: attribution, $davinci-data-export, opt-outs, $bulk-member-match
make fhir-security-check  # check the SMART authorization: tokens, a member's own data only, attribution and opt-outs
make fhir-validate-p2p    # validate the Payer-to-Payer request and response the check saved (PDex 2.1.0)
make fhir-validate-all    # fhir-validate-carin, -pdex, -provider and -p2p in one validator run, as CI does
```

The legacy schema gained columns in Phase 6, `pa_req.req_amt` with the FHIR API and the table
`mbr_cnsnt` with the Provider Access API. A database created before any of them needs `make reset`
(which deletes the local data) and the Quickstart again.

To watch change data capture at work, run the loader and the activity simulator side by side,
then rebuild the models:

```bash
make cdc       # terminal 1: stream legacy changes into bronze every 5 seconds (Ctrl+C to stop)
make changes   # terminal 2: 300 inserts/updates/deletes in the legacy database over 60 seconds
make dbt       # after stopping `make cdc`: refresh silver/gold, including gold.cdc_latency
```

`make slot-check` reports how much WAL the replication slot holds and fails above 512 MB
(the database caps it at 1 GB); `make slot-drill` pushes the slot past the cap with the loader
stopped and checks the alert and the recovery. The recovery is a new snapshot, which replaces the
CDC change history (and so `gold.cdc_latency`); run the drill before a `make cdc` demo, not after.

`make cdc` can keep running while `make dbt` builds: DuckLake takes several writers, and the dbt
models of the legacy tables apply only the changes that arrived since their last build. `make
cdc-check` exercises that path (simulated changes, a sync, the incremental build) and checks the
result against a full rebuild and against Postgres.

**Continuous integration.** On every pull request and push to `main`, GitHub Actions
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs `make up fhir-up seed pbm synthea ingest
slot-drill dbt conformance fhir-publish fhir-api-check fhir-provider-api-check fhir-security-check
fhir-validate-all cdc-check` on a clean runner (`fhir-validate-all` runs the four `fhir-validate-*` samples with IGs
in one HL7 validator run). The run fails
if the slot drill fails, if a model or data test fails, if any shareable SQL on FHIR test fails, if
the HL7 validator reports an error against the CARIN Blue Button or PDex profiles in the samples or
the Payer-to-Payer messages, if a check of a FHIR API against gold or of its authorization fails, or if the incremental
current state differs from a full rebuild or from Postgres.

Some data tests are configured as **warnings** on purpose: they flag issues that exist in the
legacy source. The `gold.dq_issue_summary` model counts them.

## Results at a glance

Figures for the reference date 2026-09-28, from synthetic data. Every figure, where it is measured
and how to reproduce it are in [docs/results.md](docs/results.md).

| Area                | Result                                                                                           |
|---------------------|--------------------------------------------------------------------------------------------------|
| Data quality        | 312 of 312 injected orphan claim lines and 242 of 242 claims with an unknown member detected      |
| Change data capture | 640 streamed changes, median latency 2.16 s with 5-second polling; rebuilt state matched the source row for row. On DuckLake, the incremental current state equals a full rebuild and Postgres after simulated changes, a new snapshot and a TRUNCATE, and the loader kept writing during a full dbt build (626 changes, median latency 0.42 s) |
| SQL on FHIR         | 133 of 133 shareable tests of the specification's test suite passed                              |
| Legacy data as FHIR | 24,507 ExplanationOfBenefit resources round-tripped with zero differing rows                     |
| Identity resolution | PBM cardholders linked to members with 100% precision and 97.9% recall against ground truth (deterministic baseline: 99.5% and 93.4%), 98.9% after the review queue was worked; 93 of 94 duplicate cardholder IDs within the PBM merged, none wrongly |
| FHIR validation     | 0 errors and 0 warnings on a 1,600-resource sample against the CARIN Blue Button 2.1.0 profiles and on 547 prior authorizations against Da Vinci PDex 2.1.0, references resolved, codes checked by tx.fhir.org (warnings the data cannot remove counted apart, with their reason); 1,000 of 1,000 failed CARIN before remediation |
| FHIR API            | 42,550 resources (32,224 ExplanationOfBenefit, 3,000 of them prior authorizations) served by HAPI FHIR; 143 of 143 checks of counts, CARIN and PDex searches and a Bulk $export passed, every exported resource equal to gold |
| Provider Access and Payer-to-Payer | 300 attribution lists from claims, 89 opted-out members left out and checked again at export; claims without financial data valid against the CARIN Basis profiles, attribution lists and opt-outs against PDex 2.1.0 (0 errors); 129 of 129 checks through the PDex gateway passed, including an opt-out added after the list was built and a 22-member `$bulk-member-match` |
| API security        | SMART App Launch (PKCE) for Patient Access and SMART Backend Services (signed JWTs) for Provider Access and Payer-to-Payer; 73 of 73 checks passed: a member's app sees only that member, a provider only members on its list who have not opted out (an opt-out applies to the next search), another payer only its own match groups |
| Terminology         | Every ICD-10-CM, NDC and place of service code in the 29,224 exported claims valid on its service date (CMS and FDA releases); CPT, NUBC and NCPDP codes counted as unverified |

## Roadmap

- [x] **Phase 1 – Foundation:** legacy source, synthetic data, bronze loaders, first silver/gold models
- [x] **Phase 2 – Ingestion:** change data capture from the legacy database and latency metrics
- [x] **Phase 3 – Modeling:** complete silver/gold layers (eligibility, providers, plans), Airflow orchestration
- [x] **Phase 4 – FHIR:** run SQL on FHIR ViewDefinitions, compare with hand-written models, map legacy data to FHIR-aligned outputs
- [x] **Phase 5 – Dissemination:** technical write-up and reusable migration checklist
- [x] **Phase 6 – Profile conformance:** CARIN Blue Button 2.1.0 profiles (baseline, remediation, Organization resources, reference checks), validated in CI
- [x] **Phase 7 – Identity resolution:** a PBM feed with the same members under other IDs, probabilistic matching (Splink) against a deterministic baseline, measured against ground truth, through to FHIR
- [x] **Operations – Slot monitoring:** a WAL retention cap, an alert outside the loader, slot lag history in gold, recovery from a lost slot, and a drill in CI
- [x] **Terminology:** codes checked by tx.fhir.org in the CARIN validation, and every code in every exported claim checked in dbt against the CMS ICD-10-CM and FDA NDC releases
- [x] **Identity – Review and deduplication:** reviewer decisions as a versioned seed with an audit trail, a simulated reviewer, and duplicate cardholders within the PBM merged by a second Splink model
- [x] **Cleanup:** every CI, Synthea, Splink and validator warning traced to its cause and fixed, or counted apart when the data cannot remove it
- [x] **FHIR API:** a HAPI FHIR server loaded from gold (search and Bulk `$export`), with prior authorization as PDex ExplanationOfBenefit
- [x] **DuckLake:** a Postgres catalog with Parquet data, and an incremental CDC current state
- [x] **Provider Access and Payer-to-Payer APIs:** attribution lists from claims, opt-outs captured by CDC and checked at export, claims without financial data (CARIN Basis), and a gateway implementing the PDex `$davinci-data-export`, `$bulk-member-match` and HRex `$member-match`
- [x] **API security:** SMART on FHIR for the Patient Access API and SMART Backend Services for the provider and payer APIs, from a minimal authorization server, enforced in the gateways

Backlog (not started):

- [ ] **Prior authorization exchange:** Da Vinci PAS, CRD and DTR, from the provider's request to the payer's decision
- [ ] **Observable data quality:** the gold metrics over time (coverage, CDC latency, conformance, linkage) on a dashboard
- [ ] **Scale:** many more members, measuring DuckLake and Splink as the data grows

## Project structure

```
legacy_db/     Legacy schema, Postgres image (wal2json), data generator and activity simulator
scripts/       Synthea download and configuration, PBM feed generator, simulated identity reviewer, dbt coverage report
ingestion/     Lakehouse connection (DuckLake) and bronze-layer loaders (CDC for legacy, NDJSON for FHIR, files for the PBM, CMS and FDA code lists) with load logging
dbt/           Silver and gold models (including the Splink Python models), seeds (with the review decisions), tests, lineage
airflow/       Airflow image and DAGs
fhir/          SQL on FHIR ViewDefinitions, DuckDB compiler, conformance runner, HL7 validation (base, CARIN, PDex), FHIR server publish, the PDex gateway and API checks
docs/          Technical write-up, migration checklist, architecture details and results
.github/       CI workflow (runs the Quickstart and the FHIR checks on every pull request)
```

## Author

Matheus Julio de Oliveira, data engineering specialist focused on data platform modernization.

## License

MIT
