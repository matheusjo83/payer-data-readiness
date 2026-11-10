PY ?= python

.PHONY: up down reset seed pbm synthea ingest cdc changes slot-check slot-drill views conformance dbt review coverage fhir-validate fhir-validate-carin docs docs-static airflow-up airflow-down all

up:        ## Build and start the legacy Postgres database (with wal2json for CDC)
	docker compose up -d --build legacy-db

down:      ## Stop containers (including Airflow)
	docker compose --profile airflow down

reset:     ## Stop containers and delete all local data
	docker compose --profile airflow down -v
	rm -rf data/

seed:      ## Generate synthetic legacy payer data (with deliberate quality issues)
	$(PY) legacy_db/seed/generate_legacy_data.py

pbm:       ## Generate the synthetic PBM feed (members, pharmacies, claims) from the legacy members
	$(PY) scripts/generate_pbm_feed.py

synthea:   ## Generate synthetic FHIR R4 data with Synthea
	bash scripts/generate_synthea.sh

ingest:    ## Snapshot (first run) or sync legacy changes via CDC, and load FHIR NDJSON, the PBM feed and the code system releases into bronze
	$(PY) ingestion/cdc_legacy.py sync
	$(PY) ingestion/load_fhir.py
	$(PY) ingestion/load_pbm.py
	$(PY) ingestion/load_terminology.py

cdc:       ## Stream legacy changes into bronze continuously (Ctrl+C to stop)
	$(PY) ingestion/cdc_legacy.py sync --follow --interval 5

slot-check: ## Check replication slot lag (fails above 512 MB of retained WAL, or if the slot is at risk or lost)
	$(PY) ingestion/cdc_legacy.py slot-status

slot-drill: ## Prove the slot monitoring: push the slot past its WAL cap, check the alerts and the recovery
	$(PY) scripts/slot_drill.py

changes:   ## Simulate activity in the legacy database (300 events over 60 seconds)
	$(PY) legacy_db/seed/simulate_changes.py --events 300 --duration 60

views:     ## Compile the SQL on FHIR ViewDefinitions (fhir/view_definitions) into dbt macros
	$(PY) fhir/build_views.py

conformance: ## Run the official SQL on FHIR v2 test suite against the ViewDefinition compiler
	$(PY) fhir/conformance.py

dbt:       ## Build silver and gold layers and run data tests
	cd dbt && dbt build --profiles-dir .

review:    ## Work the identity review queue as a simulated reviewer (ground truth) until it is empty; writes dbt/seeds/identity_review_decisions.csv
	@for round in 1 2 3 4 5; do \
	  $(PY) scripts/review_identity_queue.py; rc=$$?; \
	  if [ $$rc -eq 0 ]; then exit 0; elif [ $$rc -ne 3 ]; then exit $$rc; fi; \
	  (cd dbt && dbt build --profiles-dir . -s identity_review_decisions+) > /dev/null || exit 1; \
	done; echo "The review queue is not empty after 5 rounds"; exit 1

coverage:  ## Documentation and test coverage of the dbt models (writes dbt/target/coverage.json)
	cd dbt && dbt docs generate --profiles-dir .
	$(PY) scripts/dbt_coverage.py

airflow-up:   ## Build and start Airflow (UI at http://localhost:8080)
	AIRFLOW_UID=$$(id -u) docker compose --profile airflow up -d --build airflow

airflow-down: ## Stop Airflow (the legacy database keeps running)
	docker compose --profile airflow stop airflow airflow-db

fhir-validate: ## Validate a sample of the legacy-derived FHIR resources against base FHIR R4 (Java)
	$(PY) fhir/validate.py

fhir-validate-carin: ## Validate a sample against the CARIN Blue Button IG 2.1.0 profiles, with references resolved and codes checked by tx.fhir.org (Java)
	$(PY) fhir/validate.py --ig carin-bb --top 60 --tx https://tx.fhir.org

docs:      ## Generate and serve dbt docs (lineage graph)
	cd dbt && dbt docs generate --profiles-dir . && dbt docs serve --profiles-dir .

docs-static: ## Write the dbt docs as one static page (dbt/static_index.html), with the local path removed
	cd dbt && dbt docs generate --static --profiles-dir .
	sed 's#$(CURDIR)/dbt#.#g' dbt/target/static_index.html > dbt/static_index.html

all: up seed pbm synthea ingest dbt
