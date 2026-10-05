"""The operator CLIs: admin_bootstrap, admin_key, admin_import_keys.

Covers their guards and that a secret is printed once and never stored readably.
Tests that change the environment or `settings` restore them.
"""
import dataclasses

import pytest

from kwim_api import admin_import_keys, admin_key, auth
from kwim_api.config import settings
from kwim_api.keys import parse_key


class FakeAdminStore:
    """In-memory kwim_admin.api_keys + operators for the CLIs."""

    def __init__(self):
        self.keys: list[dict] = []
        self.operators: list[dict] = []
        self.connected = self.closed = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def create_api_key(self, *, team, label, key_prefix, key_hash,
                             capabilities, created_by=None, expires_at=None):
        row = {"id": f"id-{len(self.keys)}", "team": team, "label": label,
               "key_prefix": key_prefix, "key_hash": key_hash,
               "capabilities": list(capabilities), "created_at": None,
               "created_by": created_by, "expires_at": expires_at,
               "revoked_at": None, "last_used_at": None}
        self.keys.append(row)
        return row

    async def list_api_keys(self, *, team=None, include_revoked=False):
        return [k for k in self.keys
                if (team is None or k["team"] == team)
                and (include_revoked or k["revoked_at"] is None)]

    async def revoke_api_key(self, key_prefix):
        for k in self.keys:
            if k["key_prefix"] == key_prefix and k["revoked_at"] is None:
                k["revoked_at"] = "now"
                return {"id": k["id"], "team": k["team"], "label": k["label"]}
        return None

    async def api_key_labels(self, *, team=None):
        return {k["label"] for k in self.keys if team is None or k["team"] == team}

    async def count_operators(self):
        return len(self.operators)

    async def create_operator(self, *, username, password_hash, display_name=None):
        row = {"id": f"op-{len(self.operators)}", "username": username,
               "password_hash": password_hash, "display_name": display_name,
               "created_at": None}
        self.operators.append(row)
        return row


@pytest.fixture
def store(monkeypatch):
    fake = FakeAdminStore()
    for mod in (admin_key, admin_import_keys):
        monkeypatch.setattr(mod, "AdminStore", lambda: fake)
    return fake


@pytest.fixture
def legacy_env(monkeypatch):
    """A KWIM_API_KEYS map and matching allowlists, restored after the test."""
    def _apply(key_map: dict[str, str], promote="", review=""):
        monkeypatch.setattr(auth, "_load_key_map", lambda: dict(key_map))
        replaced = dataclasses.replace(settings, promote_keys=promote, review_keys=review)
        monkeypatch.setattr(auth, "settings", replaced)
    return _apply


# ---------------------------------------------------------------------------
# admin_key
# ---------------------------------------------------------------------------

def test_mint_prints_the_key_once_and_stores_only_its_hash(store, capsys):
    rc = admin_key.main(["mint", "--team", "acme", "--label", "agent runner",
                         "--capability", "read", "--capability", "propose"])
    assert rc == 0
    out = capsys.readouterr().out

    row = store.keys[0]
    assert row["team"] == "acme"
    assert row["capabilities"] == ["propose", "read"]

    printed = [w for w in out.split() if w.startswith("kwim_")]
    assert len(printed) == 1, "the key is printed exactly once"
    prefix, secret = parse_key(printed[0])
    assert prefix == row["key_prefix"]
    assert secret not in str(row), "the secret must not be stored in any form"
    assert row["key_hash"] != secret


def test_mint_rejects_an_unknown_capability(store, capsys):
    with pytest.raises(SystemExit):
        # argparse `choices` rejects it before the handler runs.
        admin_key.main(["mint", "--team", "acme", "--label", "x",
                        "--capability", "superuser"])
    assert store.keys == []


def test_mint_rejects_a_malformed_expiry(store, capsys):
    rc = admin_key.main(["mint", "--team", "acme", "--label", "x",
                         "--expires-at", "next tuesday"])
    assert rc == 1
    assert store.keys == [], "nothing is minted when the arguments are rejected"


def test_mint_with_no_capabilities_is_read_only(store):
    admin_key.main(["mint", "--team", "acme", "--label", "reader"])
    assert store.keys[0]["capabilities"] == []


def test_revoke_reports_the_cache_lag(store, capsys):
    admin_key.main(["mint", "--team", "acme", "--label", "x"])
    prefix = store.keys[0]["key_prefix"]
    capsys.readouterr()

    rc = admin_key.main(["revoke", "--key-prefix", prefix])
    assert rc == 0
    out = capsys.readouterr().out
    assert store.keys[0]["revoked_at"] is not None
    assert "in-memory" in out and "immediately" in out, (
        "an operator not told about the lag reads 'it still worked' as a breach")


def test_revoke_is_idempotent(store, capsys):
    rc = admin_key.main(["revoke", "--key-prefix", "NOSUCHPREFIX"])
    assert rc == 0
    assert "nothing to revoke" in capsys.readouterr().out


def test_list_never_prints_a_hash(store, capsys):
    admin_key.main(["mint", "--team", "acme", "--label", "x", "--capability", "review"])
    key_hash = store.keys[0]["key_hash"]
    capsys.readouterr()

    admin_key.main(["list"])
    out = capsys.readouterr().out
    assert store.keys[0]["key_prefix"] in out
    assert key_hash not in out


def test_list_filters_by_team_and_hides_revoked(store, capsys):
    admin_key.main(["mint", "--team", "acme", "--label", "a"])
    admin_key.main(["mint", "--team", "beta", "--label", "b"])
    admin_key.main(["revoke", "--key-prefix", store.keys[1]["key_prefix"]])
    capsys.readouterr()

    admin_key.main(["list", "--team", "beta"])
    assert "No keys for team 'beta'" in capsys.readouterr().out

    admin_key.main(["list", "--team", "beta", "--include-revoked"])
    assert "REVOKED" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# admin_import_keys
# ---------------------------------------------------------------------------

def test_dry_run_mints_nothing_at_all(store, legacy_env, capsys):
    legacy_env({"devkey": "acme", "promoter": "acme"}, promote="promot")
    rc = admin_import_keys.main([])
    assert rc == 0
    out = capsys.readouterr().out

    assert store.keys == []
    assert "kwim_" not in out, "a preview must not be mistakable for a real credential"
    assert "DRY-RUN" in out


def test_import_writes_one_row_per_entry_not_per_team(store, legacy_env):
    """Collapsing a team's entries would merge their capabilities into one key."""
    legacy_env({"devkey": "acme", "promoter": "acme", "otherkey": "beta"},
               promote="promot", review="devkey")
    admin_import_keys.main(["--commit"])

    assert len(store.keys) == 3
    acme = sorted(tuple(k["capabilities"]) for k in store.keys if k["team"] == "acme")
    assert acme == [("promote",), ("review",)], (
        "each entry keeps its own grant rather than being unioned or collapsed")


def test_import_warns_on_a_legacy_key_id_collision(store, legacy_env, capsys):
    """Two entries sharing a six-character legacy key_id import as separate rows,
    but the capability grant for that key_id was ambiguous."""
    legacy_env({"abcdef-one": "acme", "abcdef-two": "beta"})
    admin_import_keys.main([])
    err = capsys.readouterr().err
    assert "abcdef" in err and "ambiguous" in err


def test_rerunning_the_import_does_not_duplicate(store, legacy_env, capsys):
    """Recovering from a partial run must not mint a second key for every team
    that already succeeded."""
    legacy_env({"devkey": "acme", "otherkey": "beta"})
    admin_import_keys.main(["--commit"])
    assert len(store.keys) == 2
    first_prefixes = {k["key_prefix"] for k in store.keys}
    capsys.readouterr()

    admin_import_keys.main(["--commit"])
    out = capsys.readouterr().out
    assert len(store.keys) == 2, "a re-run minted duplicates"
    assert {k["key_prefix"] for k in store.keys} == first_prefixes
    assert "SKIPPED" in out


def test_import_resumes_after_a_partial_run(store, legacy_env, capsys):
    """The half that already landed is skipped; the half that did not is minted."""
    legacy_env({"devkey": "acme"})
    admin_import_keys.main(["--commit"])
    legacy_env({"devkey": "acme", "otherkey": "beta"})
    capsys.readouterr()

    admin_import_keys.main(["--commit"])
    assert len(store.keys) == 2
    assert {k["team"] for k in store.keys} == {"acme", "beta"}
    out = capsys.readouterr().out
    assert "SKIPPED 1" in out and "IMPORTED 1" in out


def test_import_with_an_empty_env_map_is_a_clean_exit(store, legacy_env, capsys):
    legacy_env({})
    assert admin_import_keys.main(["--commit"]) == 0
    assert store.keys == []
    assert "nothing to import" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# admin_bootstrap
# ---------------------------------------------------------------------------

def test_bootstrap_refuses_when_an_operator_exists(store, monkeypatch, capsys):
    """The guard against a second bootstrap silently creating another admin."""
    from kwim_api import admin_bootstrap

    monkeypatch.setattr(admin_bootstrap, "AdminStore", lambda: store)
    monkeypatch.setattr(admin_bootstrap, "_read_password", lambda: "hunter2")
    store.operators.append({"id": "op-0", "username": "alice"})

    rc = admin_bootstrap.main(["--username", "mallory"])
    assert rc == 1
    assert len(store.operators) == 1, "no second operator was created"


def test_bootstrap_creates_the_first_operator_without_printing_a_secret(
    store, monkeypatch, capsys,
):
    from kwim_api import admin_bootstrap

    monkeypatch.setattr(admin_bootstrap, "AdminStore", lambda: store)
    monkeypatch.setattr(admin_bootstrap, "_read_password", lambda: "hunter2")

    rc = admin_bootstrap.main(["--username", "alice"])
    assert rc == 0
    assert store.operators[0]["username"] == "alice"
    assert store.operators[0]["password_hash"].startswith("scrypt$")
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert store.operators[0]["password_hash"] not in out
