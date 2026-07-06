# KWIM Python client (canonical)

The KWIM client library.

- `kwim.py` - the client: `read_episodic`, `knowledge_propose`, `wisdom_propose`,
  `_post`. Talks to the KWIM service over HTTP; reads its API key via `secret_reader`.
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
pip install httpx pytest
pytest test_kwim.py -v
```
