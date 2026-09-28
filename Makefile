PY ?= python

.PHONY: up down reset seed synthea ingest dbt docs all

up:        ## Start the legacy Postgres database
	docker compose up -d legacy-db

down:      ## Stop containers
	docker compose down

reset:     ## Stop containers and delete all local data
	docker compose down -v
	rm -rf data/

seed:      ## Generate synthetic legacy payer data (with deliberate quality issues)
	$(PY) legacy_db/seed/generate_legacy_data.py

synthea:   ## Generate synthetic FHIR R4 data with Synthea
	bash scripts/generate_synthea.sh

ingest:    ## Load legacy tables and FHIR NDJSON into the bronze layer
	$(PY) ingestion/load_legacy.py
	$(PY) ingestion/load_fhir.py

dbt:       ## Build silver and gold layers and run data tests
	cd dbt && dbt build --profiles-dir .

docs:      ## Generate and serve dbt docs (lineage graph)
	cd dbt && dbt docs generate --profiles-dir . && dbt docs serve --profiles-dir .

all: up seed synthea ingest dbt
