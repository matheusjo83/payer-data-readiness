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
Synthea (synthetic FHIR R4)  ─┐
                              ├─► Ingestion ─► Bronze ─► Silver ─► Gold ─► SQL on FHIR views
Legacy payer DB (Postgres)   ─┘   (logged)     (raw)    (tested)  (models)  Quality & lineage
```

| Layer  | Location (DuckDB)         | Contents                                                 |
|--------|---------------------------|----------------------------------------------------------|
| Bronze | `bronze.*`                | Raw legacy snapshots and raw FHIR JSON, plus a load log  |
| Silver | `silver.*` (dbt staging)  | Deduplicated, typed, standardized and tested data        |
| Gold   | `gold.*` (dbt marts)      | Business models, quality scorecard, reliability metrics  |

## Stack

Runs fully local, at zero cost: **Postgres** (legacy source, Docker), **DuckDB** (lakehouse engine),
**dbt** (transformations, tests, lineage), **Synthea** (synthetic FHIR data), **Python** (ingestion).

## Quickstart

Requirements: Docker, Python 3.11+, Java 17+ (for Synthea), `make`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make up        # start the legacy Postgres database
make seed      # generate legacy data with deliberate quality issues
make synthea   # generate synthetic FHIR R4 data (bulk NDJSON)
make ingest    # load everything into the bronze layer
make dbt       # build silver/gold layers and run data tests
make docs      # browse models and the lineage graph
```

Some data tests are configured as **warnings** on purpose: they flag issues that exist in the
legacy source. The `gold.dq_issue_summary` model counts them.

## The simulated legacy source

`legacy_db/init/01_schema.sql` reproduces typical legacy traits: cryptic names, dates stored as
`VARCHAR(8)`, single-character codes and no foreign keys. The generator injects known issues at
documented rates (duplicate members, invalid gender codes, unparseable birth dates, claims for
unknown members, paid amounts above charges, orphan claim lines), so detection can be verified.

## Metrics

| Indicator                              | Where it is measured                    | Status      |
|----------------------------------------|-----------------------------------------|-------------|
| Load success rate, volume and duration | `gold.load_reliability`                 | Phase 1     |
| Known data-quality issues detected     | `gold.dq_issue_summary`                 | Phase 1     |
| Prior authorization decision timeliness| `gold.fct_prior_auth_timeliness`        | Phase 1     |
| Source-to-lakehouse latency (CDC)      | Ingestion log                           | Phase 2     |
| Test coverage and documented lineage   | dbt artifacts                           | Phase 3     |
| FHIR view parity (hand-written vs. ViewDefinition) | `fhir/view_definitions/`    | Phase 4     |

## Roadmap

- [x] **Phase 1 – Foundation:** legacy source, synthetic data, bronze loaders, first silver/gold models
- [ ] **Phase 2 – Ingestion:** change data capture from the legacy database and latency metrics
- [ ] **Phase 3 – Modeling:** complete silver/gold layers (eligibility, providers, plans), Airflow orchestration
- [ ] **Phase 4 – FHIR:** run SQL on FHIR ViewDefinitions, compare with hand-written models, map legacy data to FHIR-aligned outputs
- [ ] **Phase 5 – Dissemination:** technical write-up and reusable migration checklist

## Project structure

```
legacy_db/     Legacy schema and synthetic data generator
scripts/       Synthea download and configuration
ingestion/     Bronze-layer loaders with load logging
dbt/           Silver and gold models, tests, lineage
fhir/          SQL on FHIR ViewDefinitions
```

## Author

Matheus Julio de Oliveira, data engineering specialist focused on data platform modernization.

## License

MIT
