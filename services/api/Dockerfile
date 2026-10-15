# KWIM API service - the K/W/M HTTP/JSON API + the gate + the code-graph extractor.
#
# Build context is the repo root, for db/team-schema.sql.j2:
#   docker build -f services/api/Dockerfile -t <tag> \
#     --build-arg KWIM_VERSION=$(git describe --tags --match 'kwim-v*' | sed 's/^kwim-v//') .
FROM docker.io/library/python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc /uv /uvx /bin/

WORKDIR /app

# Locked dependencies into /app/.venv; the dev extra (tests, lint) stays out.
COPY services/api/pyproject.toml services/api/uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/app/.venv UV_PYTHON_DOWNLOADS=never \
    uv sync --frozen --no-install-project --no-cache

COPY services/api/kwim_api ./kwim_api
COPY services/api/with-secrets.sh /app/with-secrets.sh
RUN chmod +x /app/with-secrets.sh

# Canonical per-team schema template - see kwim_api/provision.py for how this
# is resolved at runtime.
COPY db/team-schema.sql.j2 ./db/

# The manifests and operator commands run `uvicorn` and `python -m kwim_api...`
# by name, so the venv goes first on PATH.
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# The release version /health reports, from the build's `git describe` against
# the kwim-v* tags. A build without it reports `dev`.
ARG KWIM_VERSION=dev
# The RUN uses the arg so podman's layer cache keys on it; an ENV alone is
# reused across builds with different values, and would report a stale version.
RUN printf '%s\n' "${KWIM_VERSION}" > /app/VERSION
ENV KWIM_VERSION=${KWIM_VERSION}

# Listens on 8000; settings and secrets come from the environment.
EXPOSE 8000
CMD ["uvicorn", "kwim_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
