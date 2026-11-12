PY ?= python

.PHONY: up down reset seed pbm synthea ingest cdc changes slot-check slot-drill cdc-check cdc-reconcile views conformance dbt review coverage fhir-validate fhir-validate-carin fhir-validate-pdex fhir-validate-provider fhir-validate-p2p fhir-up fhir-publish fhir-api-check fhir-provider-api-check docs docs-static airflow-up airflow-down all

up:        ## Build and start the legacy Postgres database (with wal2json for CDC) and the lakehouse catalog (DuckLake)
	docker compose up -d --build --wait legacy-db lakehouse-catalog

down:      ## Stop containers (including Airflow and the FHIR servers)
	docker compose --profile airflow --profile api down

reset:     ## Stop containers and delete all local data
	docker compose --profile airflow --profile api down -v
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

cdc-check: ## Exercise the incremental CDC current state: simulated changes, sync, incremental dbt build, parity with a full rebuild, reconciliation with Postgres
	$(PY) legacy_db/seed/simulate_changes.py --events 200 --duration 20
	$(PY) ingestion/cdc_legacy.py sync
	cd dbt && dbt build --profiles-dir . -s path:models/staging/legacy/current assert_cdc_current_state_matches_full
	$(PY) scripts/cdc_reconcile.py

cdc-reconcile: ## Compare the lakehouse current state of each legacy table with Postgres, row by row (writes data/cdc_reconcile.json)
	$(PY) scripts/cdc_reconcile.py

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

fhir-validate-pdex: ## Validate the prior authorizations (one sample per decision) against the Da Vinci PDex IG 2.1.0, codes checked by tx.fhir.org (Java)
	$(PY) fhir/validate.py --ig pdex --top 60 --tx https://tx.fhir.org

fhir-validate-provider: ## Validate the Provider Access / Payer-to-Payer data: claims without financial data (CARIN Basis), attribution lists and opt-outs (PDex), codes checked by tx.fhir.org (Java)
	$(PY) fhir/validate.py --ig provider-access --sample 20 --top 60 --tx https://tx.fhir.org

fhir-validate-p2p: ## Validate the Payer-to-Payer $$bulk-member-match request and response saved by fhir-provider-api-check (PDex 2.1.0, Java)
	$(PY) fhir/validate.py --ig payer-to-payer --top 60 --tx https://tx.fhir.org

fhir-up:   ## Start the HAPI FHIR servers empty (recreated each time) and the gateway: Patient Access at http://localhost:8090/fhir, Provider Access and Payer-to-Payer at http://localhost:8092/fhir
	docker compose --profile api up -d --force-recreate fhir-server fhir-server-provider fhir-gateway

fhir-publish: ## Load the gold FHIR resources (and the CARIN/PDex search parameters) into both FHIR servers
	$(PY) fhir/publish.py

fhir-api-check: ## Check the FHIR server against gold: counts, CARIN/PDex searches, Bulk $$export (writes data/fhir_api.json)
	$(PY) fhir/api_check.py

fhir-provider-api-check: ## Check the Provider Access and Payer-to-Payer APIs (gateway) against gold: attribution, $$davinci-data-export, opt-outs, $$bulk-member-match (writes data/fhir_provider_api.json)
	$(PY) fhir/provider_api_check.py

docs:      ## Generate and serve dbt docs (lineage graph)
	cd dbt && dbt docs generate --profiles-dir . && dbt docs serve --profiles-dir .

docs-static: ## Write the dbt docs as one static page (dbt/static_index.html), with the local path removed
	cd dbt && dbt docs generate --static --profiles-dir .
	sed 's#$(CURDIR)/dbt#.#g' dbt/target/static_index.html > dbt/static_index.html

all: up seed pbm synthea ingest dbt
