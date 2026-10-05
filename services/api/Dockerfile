# KWIM API service - the K/W/M HTTP/JSON API + the gate + the code-graph extractor.
#
# Build context is the repo root, for db/team-schema.sql.j2:
#   docker build -f services/api/Dockerfile -t <tag> .
FROM python:3.12-slim

WORKDIR /app

COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/api/kwim_api ./kwim_api
COPY services/api/with-secrets.sh /app/with-secrets.sh
RUN chmod +x /app/with-secrets.sh

# Canonical per-team schema template - see kwim_api/provision.py for how this
# is resolved at runtime.
COPY db/team-schema.sql.j2 ./db/

ENV PYTHONUNBUFFERED=1

# Listens on 8000; settings and secrets come from the environment.
EXPOSE 8000
CMD ["uvicorn", "kwim_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
