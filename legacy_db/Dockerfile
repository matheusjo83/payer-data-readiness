# Legacy Postgres with the wal2json logical decoding plugin, used for
# change data capture (CDC) into the lakehouse bronze layer.
FROM postgres:16

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-wal2json \
    && rm -rf /var/lib/apt/lists/*
