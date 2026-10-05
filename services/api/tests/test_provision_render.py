"""Tests for kwim_api/provision.py.

Pure functions, no database.
"""
import os
import re

import pytest

from kwim_api import provision


def test_render_substitutes_every_occurrence_with_and_without_whitespace():
    template = "CREATE SCHEMA IF NOT EXISTS {{kwim_team}};\n" \
               "CREATE TABLE {{ kwim_team }}.episodic_events (id uuid);\n"
    out = provision.render_team_schema(template, "acme")
    assert out == ("CREATE SCHEMA IF NOT EXISTS acme;\n"
                   "CREATE TABLE acme.episodic_events (id uuid);\n")


def test_unknown_placeholder_raises_value_error():
    template = "CREATE SCHEMA IF NOT EXISTS {{ kwim_team }}; -- {{ mystery_var }}"
    with pytest.raises(ValueError, match="unknown placeholder"):
        provision.render_team_schema(template, "acme")


def test_invalid_team_identifier_raises_before_any_substitution():
    template = "CREATE SCHEMA IF NOT EXISTS {{ kwim_team }};"
    with pytest.raises(ValueError, match="unsafe team identifier"):
        provision.render_team_schema(template, "DROP TABLE;")


def test_rendered_output_contains_no_remaining_placeholder_braces():
    template = "CREATE SCHEMA IF NOT EXISTS {{ kwim_team }};" \
               "CREATE INDEX ON {{kwim_team}}.commit_log (seq);"
    out = provision.render_team_schema(template, "acme")
    assert "{{" not in out


def test_load_team_schema_template_finds_it_at_the_packaged_layout(tmp_path):
    """Simulates the image layout (services/api/Dockerfile:
    COPY db/team-schema.sql.j2 ./db/, WORKDIR /app): db/ and kwim_api/ side by side."""
    pkg_dir = tmp_path / "kwim_api"
    pkg_dir.mkdir()
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    template_path = db_dir / "team-schema.sql.j2"
    template_path.write_text("CREATE SCHEMA IF NOT EXISTS {{ kwim_team }};")

    resolved = os.path.normpath(os.path.join(str(pkg_dir), "..", "db", "team-schema.sql.j2"))
    assert resolved == str(template_path)
    assert provision.load_team_schema_template(resolved) == \
        "CREATE SCHEMA IF NOT EXISTS {{ kwim_team }};"


def test_missing_template_raises_naming_the_expected_path(tmp_path):
    missing = tmp_path / "db" / "team-schema.sql.j2"
    with pytest.raises(FileNotFoundError, match=re.escape(str(missing))):
        provision.load_team_schema_template(str(missing))


def test_template_loads_with_no_explicit_path():
    """The default has to work in both layouts the service runs in - the image
    (db/ beside the package) and a source checkout (db/ at the repo root)."""
    from kwim_api.provision import load_team_schema_template, render_team_schema

    sql = render_team_schema(load_team_schema_template(), "pathteam")
    assert "CREATE SCHEMA IF NOT EXISTS pathteam" in sql
    assert "{{" not in sql


def test_missing_template_names_what_it_tried():
    from kwim_api.provision import load_team_schema_template

    with pytest.raises(FileNotFoundError) as exc:
        load_team_schema_template("/nonexistent/team-schema.sql.j2")
    assert "/nonexistent/team-schema.sql.j2" in str(exc.value)
    assert "Dockerfile" in str(exc.value)
