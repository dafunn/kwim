"""/health reports the release version stamped into the image."""
from __future__ import annotations

from kwim_api.main import app, release_version


def test_health_reports_the_app_version(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["version"] == app.version == release_version()


def test_release_version_is_dev_when_unset(monkeypatch):
    monkeypatch.delenv("KWIM_VERSION", raising=False)
    assert release_version() == "dev"


def test_release_version_is_dev_when_blank(monkeypatch):
    monkeypatch.setenv("KWIM_VERSION", "   ")
    assert release_version() == "dev"


def test_release_version_reports_the_build_value(monkeypatch):
    monkeypatch.setenv("KWIM_VERSION", "0.3.1-4-gabc1234")
    assert release_version() == "0.3.1-4-gabc1234"
