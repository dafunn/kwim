#!/usr/bin/env bash
# Regenerates src/api/schema.d.ts from the live FastAPI app definition.
#
# Needs services/api/.venv but no backends: app.openapi() does not run the lifespan.
set -euo pipefail
cd "$(dirname "$0")/.."

API_DIR="../api"
PYTHON="$API_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "error: $PYTHON not found - build the services/api virtualenv first" >&2
  exit 1
fi

(cd "$API_DIR" && ./.venv/bin/python -c '
from kwim_api.main import app
import json
print(json.dumps(app.openapi()))
') > openapi.json

npx openapi-typescript openapi.json -o src/api/schema.d.ts

echo "wrote openapi.json and src/api/schema.d.ts"
