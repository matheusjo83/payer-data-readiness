#!/usr/bin/env bash
# Generate synthetic FHIR R4 data (bulk NDJSON) with Synthea.
# Requires Java (17+ recommended).
#
# The Synthea release, the seeds, the reference date and the end date are pinned
# so that the same settings always produce the same data (without -e, Synthea
# simulates up to the moment it runs). To use another release, change
# SYNTHEA_VERSION and SYNTHEA_SHA256 (the digest is listed on the release page;
# set SYNTHEA_SHA256="" to skip the check).
set -euo pipefail

SYNTHEA_VERSION="${SYNTHEA_VERSION:-v4.0.0}"
SYNTHEA_SHA256="${SYNTHEA_SHA256-ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1}"
POPULATION="${POPULATION:-500}"
SEED="${SEED:-42}"
CLINICIAN_SEED="${CLINICIAN_SEED:-$SEED}"
REFERENCE_DATE="${REFERENCE_DATE:-20260928}"   # YYYYMMDD; date of the published figures
END_DATE="${END_DATE:-$REFERENCE_DATE}"         # YYYYMMDD; the simulation stops here, not at the current time
STATE="${STATE:-Texas}"
CITY="${CITY:-Austin}"
JAR="tools/synthea-with-dependencies-${SYNTHEA_VERSION}.jar"
JAR_URL="https://github.com/synthetichealth/synthea/releases/download/${SYNTHEA_VERSION}/synthea-with-dependencies.jar"

mkdir -p tools data/synthea
if [ ! -f "$JAR" ]; then
  echo "Downloading Synthea ${SYNTHEA_VERSION}..."
  curl -fL -o "$JAR.tmp" "$JAR_URL"
  mv "$JAR.tmp" "$JAR"
fi
if [ -n "$SYNTHEA_SHA256" ]; then
  echo "$SYNTHEA_SHA256  $JAR" | sha256sum -c --quiet - \
    || { echo "Checksum mismatch for $JAR (expected Synthea ${SYNTHEA_VERSION})." >&2; exit 1; }
fi

# Start from an empty output directory so files from earlier runs never mix in.
rm -rf data/synthea/fhir

java -jar "$JAR" -c scripts/synthea.properties \
  -s "$SEED" -cs "$CLINICIAN_SEED" -r "$REFERENCE_DATE" -e "$END_DATE" -p "$POPULATION" "$STATE" "$CITY"
echo "FHIR NDJSON files written to data/synthea/fhir/ (Synthea ${SYNTHEA_VERSION}, reference date ${REFERENCE_DATE}, end date ${END_DATE})"
