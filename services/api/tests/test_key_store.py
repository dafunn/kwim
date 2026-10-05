"""Pure-logic tests for kwim_api.keys - key format and password hashing.

No database: these are all pure functions.
"""
from kwim_api.keys import (
    generate_key,
    hash_password,
    hash_secret,
    parse_key,
    verify_password,
)


def test_generate_key_round_trips_through_parse_key():
    full_key, key_prefix, key_hash = generate_key()
    parsed = parse_key(full_key)
    assert parsed is not None
    prefix, secret = parsed
    assert prefix == key_prefix
    assert key_hash == hash_secret(secret)


def test_prefix_is_not_a_substring_of_the_secret():
    """The display handle must carry no secret material: it is generated
    independently of the key, not taken from its first characters."""
    full_key, key_prefix, _ = generate_key()
    _, secret = parse_key(full_key)
    assert key_prefix not in secret


def test_generate_key_produces_distinct_keys():
    keys = {generate_key()[0] for _ in range(20)}
    assert len(keys) == 20


def test_parse_key_rejects_malformed_input():
    for bad in ("", "kwim_", "nope", "kwim_tooshort_x", "kwim_" + "A" * 12,
                "kwim_" + "A" * 12 + "_", "kwim_" + "A" * 11 + "_secret"):
        assert parse_key(bad) is None


def test_parse_key_rejects_out_of_alphabet_halves():
    """The bearer value is untrusted. A prefix or secret outside the alphabets
    generate_key draws from could not be a key this service minted."""
    valid_prefix, valid_secret = "ABCDEFGHIJKL", "abcDEF-123_xyz"
    assert parse_key(f"kwim_{valid_prefix}_{valid_secret}") is not None
    # Non-ascii, and characters outside base64url / base32 respectively.
    assert parse_key(f"kwim_{valid_prefix}_") is None
    for bad_secret in ("\u65e5\u672c\u8a9e", "has spaces", "semi;colon", "plus+slash/"):
        assert parse_key(f"kwim_{valid_prefix}_{bad_secret}") is None
    for bad_prefix in ("abcdefghijkl", "ABCDEFGHIJK1", "ABCDEF-HIJKL"):
        assert parse_key(f"kwim_{bad_prefix}_{valid_secret}") is None


def test_hash_secret_never_raises_on_odd_input():
    """It sits on the request-path auth check, so a crash here is a 500 where a
    401 belongs."""
    for odd in ("\u65e5\u672c\u8a9e", "", "\x00\x01", "e" * 10_000):
        assert len(hash_secret(odd)) == 64


def test_hash_password_round_trips_and_rejects_wrong_password():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def test_hash_password_uses_a_fresh_salt_each_time():
    a = hash_password("same password")
    b = hash_password("same password")
    assert a != b
    assert verify_password("same password", a)
    assert verify_password("same password", b)


def test_verify_password_rejects_malformed_encoded_hash():
    assert not verify_password("anything", "not-an-encoded-hash")
    assert not verify_password("anything", "")


def test_verify_password_denies_rather_than_raises_on_a_corrupt_row():
    """A corrupt stored hash must fail the login, not 500 it. These are shapes
    that parse cleanly but make scrypt itself reject the parameters."""
    for corrupt in (
        "scrypt$n=16384$r=8$p=1$c2FsdA==$",        # empty derived key -> dklen 0
        "scrypt$n=3$r=8$p=1$c2FsdA==$aGFzaA==",    # n is not a power of two
        "scrypt$n=0$r=8$p=1$c2FsdA==$aGFzaA==",
        "scrypt$n=16384$r=0$p=1$c2FsdA==$aGFzaA==",
        "scrypt$nope$r=8$p=1$c2FsdA==$aGFzaA==",
        "scrypt$n=16384$r=8$p=1$!!!notb64$aGFzaA==",
    ):
        assert not verify_password("anything", corrupt)
