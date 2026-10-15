# Distiller; the team comes from the mounted kwim-api-key.
# Build context is the repo root, for clients/python:
#   docker build -f services/distiller/Dockerfile -t <tag> .
FROM docker.io/library/python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc /uv /uvx /bin/

WORKDIR /app

# Locked dependencies into /app/.venv; the dev extra (tests) stays out.
COPY services/distiller/pyproject.toml services/distiller/uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/app/.venv UV_PYTHON_DOWNLOADS=never \
    uv sync --frozen --no-install-project --no-cache

# The KWIM client modules, from clients/python.
COPY clients/python/kwim.py clients/python/secret_reader.py clients/python/llm_router.py ./
# otel helper - distiller-local, not part of the client package.
COPY services/distiller/otel.py ./
COPY services/distiller/distiller.py ./

# CMD and any CronJob command run `python` by name, so the venv goes first on PATH.
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# Ephemeral job - runs to completion and exits.
CMD ["python", "distiller.py"]
