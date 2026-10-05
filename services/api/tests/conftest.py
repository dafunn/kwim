"""Shared pytest fixtures + environment for the KWIM service test suite.

`settings` and the auth key map are built once, on first import, so this sets the
environment every test module needs before anything imports kwim_api:

  - api keys (union of every key the suite authenticates with):
      devkey   -> acme      (key_id "devkey")  general + review-capable
      promoter -> acme      (key_id "promot")  promote/seed-capable
      otherkey -> otherteam (key_id "otherk")  neither promote nor review
  - promote/review allowlists are keyed on the 6-char key_id prefix.
  - gate tunables match what test_gate asserts against.
"""
import os

# --- Env superset - must be set before any `app.*` import (see module docstring).
os.environ["KWIM_API_KEYS"] = "devkey:acme,promoter:acme,otherkey:otherteam"
os.environ["KWIM_PROMOTE_KEYS"] = "promot"   # key_id of "promoter"
os.environ["KWIM_REVIEW_KEYS"] = "devkey"    # key_id of "devkey"
os.environ["KWIM_MM_ACTION_SECRET"] = "topsecret"
os.environ["KWIM_GATE_VERIFY"] = "1"
os.environ["KWIM_GATE_DUP_DIST"] = "0.05"
os.environ["KWIM_GATE_REVIEW_DIST"] = "0.25"
# test_otel needs OTEL unconfigured at the start.
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
os.environ.pop("OTEL_SERVICE_NAME", None)

import pytest


@pytest.fixture(scope="session")
def client():
    """A TestClient over the real FastAPI app.

    Created without `with`, so the lifespan does not run; tests set
    `kwim_api.runtime.State.*` and `app.state.gate` to fakes.
    """
    from fastapi.testclient import TestClient

    from kwim_api.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_key_cache():
    """auth.py's key-resolution cache is a module-level dict, not per-request
    state, so it is cleared around every test."""
    from kwim_api.auth import invalidate_key_cache

    invalidate_key_cache()
    yield
    invalidate_key_cache()


@pytest.fixture(autouse=True)
def _reset_admin_login_rate_limit():
    """routers/admin.py's failed-login counters are a module-level dict for the
    reason as the key cache, so they are cleared around every test."""
    from kwim_api.routers.admin import _failures

    _failures.clear()
    yield
    _failures.clear()
