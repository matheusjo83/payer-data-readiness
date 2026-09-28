#!/usr/bin/env bash
# Generate synthetic FHIR R4 data (bulk NDJSON) with Synthea.
# Requires Java (17+ recommended).
set -euo pipefail

POPULATION="${POPULATION:-500}"
SEED="${SEED:-42}"
STATE="${STATE:-Texas}"
CITY="${CITY:-Austin}"
JAR="tools/synthea-with-dependencies.jar"
JAR_URL="https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar"

mkdir -p tools data/synthea
if [ ! -f "$JAR" ]; then
  echo "Downloading Synthea..."
  curl -fL -o "$JAR" "$JAR_URL"
fi

java -jar "$JAR" -c scripts/synthea.properties -s "$SEED" -p "$POPULATION" "$STATE" "$CITY"
echo "FHIR NDJSON files written to data/synthea/fhir/"
