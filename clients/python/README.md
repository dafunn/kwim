# KWIM Python client (canonical)

The KWIM client library.

- `kwim.py` - the client: `emit_episodic`, `read_episodic`, `knowledge_query`,
  `knowledge_search`, `knowledge_propose`, `wisdom_rules`, `wisdom_check`,
  `wisdom_propose`, `memory_context`, `memory_semantic`, and for documents
  `documents_list`, `documents_search`, `documents_section`, `documents_figure`
  and `documents_promote`, and for skills `skills_search`, `skill_get`,
  `skills_list`, `skill_propose` and `skill_promote`. Talks to the KWIM service
  over HTTP; reads its API key via `secret_reader`.
- `skills_search` reports when skills are unavailable instead of returning an
  empty result: without configuration or with KWIM unreachable it returns
  `coverage.available` false and the reason, so an agent can say it is working
  without skills.
- `secret_reader.py` - tiny secret-file reader (`read_secret`), a dependency of the
  client (reads mounted files under `/secrets/<name>`, or `KWIM_SECRETS_DIR`).
- `test_kwim.py` - the client's tests.

## Consumers

- **distiller** (`services/distiller/`) - vendors these modules directly (COPY at
  build time; repo-root build context).
- Any service that talks to KWIM can install this client so `import kwim` keeps
  working without vendoring a copy.

## Packaging

`pyproject.toml` makes this pip-installable (top-level modules `kwim`,
`secret_reader`) so `import kwim` works for any consumer.

## Tests

```bash
uv sync --frozen --extra dev
.venv/bin/python -m pytest test_kwim.py -v
```
