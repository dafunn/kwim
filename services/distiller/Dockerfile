# Distiller - KWIM's episodic->semantic memory distiller. Team-agnostic (the team is
# the mounted kwim-api-key). Owned + built by the KWIM project.
#
# SELF-CONTAINED slim image - no shared agent-framework base image dependency.
# The distiller's real footprint is tiny: 3 helper modules + the KWIM client + 5 pip libs.
# Build context is the REPO ROOT so the canonical client under clients/python is reachable:
#   docker build -f services/distiller/Dockerfile -t <tag> .
FROM python:3.12-slim

WORKDIR /app

COPY services/distiller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# KWIM client + LLM factory - canonical source in clients/python (KWIM owns its
# client). llm_router now ships in the kwim-client package, no longer vendored.
COPY clients/python/kwim.py clients/python/secret_reader.py clients/python/llm_router.py ./
# otel helper - distiller-local, not part of the client package.
COPY services/distiller/otel.py ./
COPY services/distiller/distiller.py ./

ENV PYTHONUNBUFFERED=1

# Ephemeral job - runs to completion and exits.
CMD ["python", "distiller.py"]
