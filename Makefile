PY ?= python

.PHONY: up down reset seed synthea ingest cdc changes views conformance dbt coverage fhir-validate docs airflow-up airflow-down all

up:        ## Build and start the legacy Postgres database (with wal2json for CDC)
	docker compose up -d --build legacy-db

down:      ## Stop containers (including Airflow)
	docker compose --profile airflow down

reset:     ## Stop containers and delete all local data
	docker compose --profile airflow down -v
	rm -rf data/

seed:      ## Generate synthetic legacy payer data (with deliberate quality issues)
	$(PY) legacy_db/seed/generate_legacy_data.py

synthea:   ## Generate synthetic FHIR R4 data with Synthea
	bash scripts/generate_synthea.sh

ingest:    ## Snapshot (first run) or sync legacy changes via CDC, and load FHIR NDJSON into bronze
	$(PY) ingestion/cdc_legacy.py sync
	$(PY) ingestion/load_fhir.py

cdc:       ## Stream legacy changes into bronze continuously (Ctrl+C to stop)
	$(PY) ingestion/cdc_legacy.py sync --follow --interval 5

changes:   ## Simulate activity in the legacy database (300 events over 60 seconds)
	$(PY) legacy_db/seed/simulate_changes.py --events 300 --duration 60

views:     ## Compile the SQL on FHIR ViewDefinitions (fhir/view_definitions) into dbt macros
	$(PY) fhir/build_views.py

conformance: ## Run the official SQL on FHIR v2 test suite against the ViewDefinition compiler
	$(PY) fhir/conformance.py

dbt:       ## Build silver and gold layers and run data tests
	cd dbt && dbt build --profiles-dir .

coverage:  ## Documentation and test coverage of the dbt models (writes dbt/target/coverage.json)
	cd dbt && dbt docs generate --profiles-dir .
	$(PY) scripts/dbt_coverage.py

airflow-up:   ## Build and start Airflow (UI at http://localhost:8080)
	AIRFLOW_UID=$$(id -u) docker compose --profile airflow up -d --build airflow

airflow-down: ## Stop Airflow (the legacy database keeps running)
	docker compose --profile airflow stop airflow airflow-db

fhir-validate: ## Validate a sample of the legacy-derived FHIR resources with the HL7 validator (Java)
	$(PY) fhir/validate.py

docs:      ## Generate and serve dbt docs (lineage graph)
	cd dbt && dbt docs generate --profiles-dir . && dbt docs serve --profiles-dir .

all: up seed synthea ingest dbt
