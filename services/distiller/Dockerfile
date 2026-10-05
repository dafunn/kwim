# Distiller; the team comes from the mounted kwim-api-key.
# Build context is the repo root, for clients/python:
#   docker build -f services/distiller/Dockerfile -t <tag> .
FROM python:3.12-slim

WORKDIR /app

COPY services/distiller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The KWIM client modules, from clients/python.
COPY clients/python/kwim.py clients/python/secret_reader.py clients/python/llm_router.py ./
# otel helper - distiller-local, not part of the client package.
COPY services/distiller/otel.py ./
COPY services/distiller/distiller.py ./

ENV PYTHONUNBUFFERED=1

# Ephemeral job - runs to completion and exits.
CMD ["python", "distiller.py"]
