"""How a stored credential is represented.

Password and API-key hashing lives beside the identity tables rather than in
the API layer because the hash *is* the stored column: the algorithm and the
schema have to change together, and nothing above persistence should be able
to write a credential in a different format. The API layer imports these.

Argon2id defaults from `argon2-cffi` are used deliberately over a hand-tuned
cost: the library tracks the current OWASP guidance, and `PasswordHasher`
encodes its parameters into the hash string, so raising them later leaves
existing hashes verifiable.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return str(_hasher.hash(password))


def verify_password(password: str, hashed: str) -> bool:
    """False rather than raising: a caller must not be able to tell a malformed
    stored hash from a wrong password, and neither is an exceptional event."""
    try:
        return bool(_hasher.verify(hashed, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_secret, prefix, hash). The secret is never stored.

    The prefix is stored in the clear so a presented key can be looked up by a
    single indexed equality before any Argon2 verification runs; it carries no
    secret material of its own.
    """
    prefix = "sk_" + secrets.token_hex(4)
    body = secrets.token_urlsafe(32)
    full = f"{prefix}.{body}"
    return full, prefix, hash_password(full)
