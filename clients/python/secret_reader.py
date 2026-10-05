"""Read secrets from mounted secret files.

Reads each secret by name from a directory: ``/secrets``, or ``KWIM_SECRETS_DIR``.
"""

import os
import pathlib

_SECRETS_DIR = pathlib.Path(os.environ.get("KWIM_SECRETS_DIR", "/secrets"))


def secrets_dir() -> pathlib.Path:
    """The directory secrets are read from, for error messages."""
    return _SECRETS_DIR


def read_secret(name: str) -> str:
    """Return the content of ``<secrets-dir>/<name>``, stripped.

    Raises FileNotFoundError if the file is absent.
    """
    path = _SECRETS_DIR / name
    return path.read_text(encoding="utf-8").strip()
