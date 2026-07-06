"""Shared pytest fixtures + environment for the KWIM service test suite.

WHY THIS FILE EXISTS
--------------------
The old script-style tests each ran in their *own* process, so every file could
set its own `KWIM_*` env vars and get a fresh `app.config.settings` /
`app.auth._KEY_MAP` on import. Under pytest the whole suite shares one process
and one import of those modules - both `settings` (a frozen dataclass) and the
auth key map are built exactly once, at first import.

So we set a *superset* of the env every module used, here, before anything
imports `app.*`. pytest imports conftest.py ahead of the test modules in its
directory, so this runs first and the singletons are built with all keys/tunables
present.

  - api keys (union of every key the suite authenticates with):
      devkey   -> acme      (key_id "devkey")  general + review-capable
      promoter -> acme      (key_id "promot")  promote/seed-capable
      otherkey -> otherteam (key_id "otherk")  neither promote nor review
  - promote/review allowlists are keyed on the 6-char key_id prefix.
  - gate tunables match what test_gate_verify_logic asserts against.

`pythonpath = .` in pytest.ini puts the service root on sys.path, so `import app`
resolves without the per-file sys.path.insert hacks.
"""
import os

# --- Env superset - MUST be set before any `app.*` import (see module docstring).
os.environ["KWIM_API_KEYS"] = "devkey:acme,promoter:acme,otherkey:otherteam"
os.environ["KWIM_PROMOTE_KEYS"] = "promot"   # key_id of "promoter"
os.environ["KWIM_REVIEW_KEYS"] = "devkey"    # key_id of "devkey"
os.environ["KWIM_MM_ACTION_SECRET"] = "topsecret"
os.environ["KWIM_GATE_VERIFY"] = "1"
os.environ["KWIM_GATE_DUP_DIST"] = "0.05"
os.environ["KWIM_GATE_REVIEW_DIST"] = "0.25"
# OTEL must start unconfigured so test_otel's "no-op when endpoint unset" phase is
# meaningful (it installs a real provider itself, in-process, in part 2).
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
os.environ.pop("OTEL_SERVICE_NAME", None)

import pytest


@pytest.fixture(scope="session")
def client():
    """A TestClient over the real FastAPI app.

    Instantiated without `with`, so the lifespan (which would open real DB/broker
    connections) never runs - tests wire `app.main.State.*` and `app.state.gate`
    to fakes themselves, via the monkeypatch-based fixtures in each module.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
