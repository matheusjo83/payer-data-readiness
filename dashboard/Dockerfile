# The data quality history dashboard (Streamlit), reading the lakehouse read-only.
FROM python:3.12-slim

WORKDIR /app
COPY dashboard/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    # The DuckLake and Postgres extensions, so the container needs no download.
    && python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL ducklake'); c.execute('INSTALL postgres')"
COPY ingestion/lakehouse.py dashboard/data.py dashboard/app.py /app/

RUN useradd --uid 1000 --create-home dashboard \
    && mkdir -p /home/dashboard/.duckdb && cp -r /root/.duckdb/extensions /home/dashboard/.duckdb/ \
    && chown -R dashboard /home/dashboard/.duckdb
USER dashboard

EXPOSE 8501
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=2)"
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", \
     "--browser.gatherUsageStats=false", "--client.toolbarMode=viewer"]
