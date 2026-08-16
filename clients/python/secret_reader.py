"""Read secrets from mounted secret files.

Your secret manager (sealed-secrets, external-secrets, a plain Secret mount, or
any injector) materializes each secret as a file under a mount directory; this
reads them by name. The directory defaults to ``/secrets`` and is overridable
with the ``KWIM_SECRETS_DIR`` env var.
"""

import os
import pathlib

_SECRETS_DIR = pathlib.Path(os.environ.get("KWIM_SECRETS_DIR", "/secrets"))


def secrets_dir() -> pathlib.Path:
    """The directory secrets are read from - for error messages and preflights.

    Callers that fail loudly on a missing secret should name this path in the
    error: the usual cause is a mount at some other directory with
    ``KWIM_SECRETS_DIR`` left unset.
    """
    return _SECRETS_DIR


def read_secret(name: str) -> str:
    """Return the content of ``<secrets-dir>/<name>``, stripped.

    Raises FileNotFoundError if the file is absent - callers should treat that
    as a hard startup failure (the secret manager didn't provision it).
    """
    path = _SECRETS_DIR / name
    return path.read_text(encoding="utf-8").strip()
