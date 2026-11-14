# The FHIR gateways (fhir/gateway/gateway.py) and the SMART authorization server
# (fhir/auth/server.py): Python with the JWT library, and the Postgres driver the
# prior authorization service writes the payer's UM tables with; the code is mounted.
FROM python:3.12-slim
RUN pip install --no-cache-dir --disable-pip-version-check pyjwt==2.15.1 cryptography==50.0.2 "psycopg[binary]==3.3.6"
