# Airflow orchestrates the pipeline; the loaders and dbt run in their own
# virtualenv so their dependencies never conflict with Airflow's.
FROM apache/airflow:3.3.2-python3.12

USER root
RUN mkdir /opt/pipeline-venv && chown airflow:0 /opt/pipeline-venv
USER airflow

COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/pipeline-venv \
    && /opt/pipeline-venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
