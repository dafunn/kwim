"""API key format and password hashing - pure functions, no I/O.

Keys are `kwim_<prefix>_<secret>`: a 12-character base32 prefix and a 32-byte
url-safe base64 secret, stored as SHA-256. Passwords use scrypt. See
docs/DESIGN.md, "Team keys".
"""
import base64
import hashlib
import hmac
import re
import secrets

# The alphabets generate_key draws from; parse_key rejects anything outside them.
_PREFIX_RE = re.compile(r"^[A-Z2-7]+$")          # base32
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]+$")     # url-safe base64, padding stripped

_PREFIX_LEN = 12
_SECRET_BYTES = 32
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _random_base32(length: int) -> str:
    # A multiple of 5 bytes encodes to base32 without padding.
    nbytes = -(-length * 5 // 8)
    nbytes += (5 - nbytes % 5) % 5
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii")[:length]


def generate_key() -> tuple[str, str, str]:
    """Mint a new API key. Returns (full_key, key_prefix, key_hash)."""
    key_prefix = _random_base32(_PREFIX_LEN)
    secret_bytes = secrets.token_bytes(_SECRET_BYTES)
    secret = base64.urlsafe_b64encode(secret_bytes).rstrip(b"=").decode("ascii")
    full_key = f"kwim_{key_prefix}_{secret}"
    return full_key, key_prefix, hash_secret(secret)


def parse_key(raw: str) -> tuple[str, str] | None:
    """Split `kwim_<prefix>_<secret>` into (prefix, secret), or None if malformed.
    Splits at fixed offsets, since the secret may contain underscores."""
    if not raw.startswith("kwim_"):
        return None
    rest = raw[len("kwim_"):]
    if len(rest) <= _PREFIX_LEN + 1 or rest[_PREFIX_LEN] != "_":
        return None
    prefix, secret = rest[:_PREFIX_LEN], rest[_PREFIX_LEN + 1:]
    if not _PREFIX_RE.match(prefix) or not _SECRET_RE.match(secret):
        return None
    return prefix, secret


def hash_secret(secret: str) -> str:
    """sha256 hex of the secret half of an API key. Encodes as utf-8 so it never
    raises on the request path."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """scrypt with a fresh random salt; returns an encoded salt-and-hash string."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                             r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return "$".join((
        "scrypt", f"n={_SCRYPT_N}", f"r={_SCRYPT_R}", f"p={_SCRYPT_P}",
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    ))


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification against an encoded scrypt hash. A stored value
    that cannot be parsed or derived returns False rather than raising."""
    try:
        scheme, n_field, r_field, p_field, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_field[2:]), int(r_field[2:]), int(p_field[2:])
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                 dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)
