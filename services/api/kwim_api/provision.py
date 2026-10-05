"""Render db/team-schema.sql.j2 for one team by substituting {{ kwim_team }}.
See docs/DESIGN.md, "Provisioning teams".
"""
import os
import re

from .stores.postgres import _IDENT

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# The template's location in the image (/app/db, one level up) and in a source
# checkout (the repo root's db/, three levels up), nearest first.
_CANDIDATE_PATHS = tuple(
    os.path.normpath(os.path.join(os.path.dirname(__file__), *parts))
    for parts in (("..", "db", "team-schema.sql.j2"),
                  ("..", "..", "..", "db", "team-schema.sql.j2")))
TEMPLATE_PATH = _CANDIDATE_PATHS[0]


def load_team_schema_template(path: str | None = None) -> str:
    """Read the template; with no `path`, from the image layout, then the source
    checkout. FileNotFoundError names every path tried."""
    candidates = (path,) if path is not None else _CANDIDATE_PATHS
    for candidate in candidates:
        try:
            with open(candidate) as fh:
                return fh.read()
        except OSError:
            continue
    raise FileNotFoundError(
        f"team schema template not found (tried {', '.join(map(repr, candidates))}) - "
        "expected db/team-schema.sql.j2 to be copied into the image "
        "(see services/api/Dockerfile)")


def render_team_schema(template_sql: str, team: str) -> str:
    """Substitute {{ kwim_team }} in the schema template. Raises ValueError for an
    invalid team name or any other placeholder in the template."""
    if not _IDENT.match(team):
        raise ValueError(f"unsafe team identifier: {team!r}")

    placeholders = set(_PLACEHOLDER.findall(template_sql))
    unknown = placeholders - {"kwim_team"}
    if unknown:
        raise ValueError(f"template has unknown placeholder(s): {sorted(unknown)}")

    return _PLACEHOLDER.sub(
        lambda m: team if m.group(1) == "kwim_team" else m.group(0), template_sql)
