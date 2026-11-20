# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Ported from SoilMetrix, Inc (dba Aiterra.ag) by Benjamin Booth, CEO.

"""Password hashing and validation.

Hashing uses stdlib ``hashlib.scrypt``: no third-party dependency, memory-hard,
and self-describing hash strings that carry their own cost parameters so old
hashes keep validating when those parameters change.

Verification dispatches on the scheme recorded in the stored hash, which is the
part that lets a product with existing users adopt this module. An earlier
version hashed and verified scrypt only — correct while the only consumer was
greenfield, and a wall for anyone else, because adopting Axiom's auth would have
failed every login made before the switch. A platform that only supports what it
happens to have written is a platform nobody can migrate to.

Legacy bcrypt hashes therefore verify too, through an optional dependency that
stays optional: recognising a scheme never requires its library, so a deployment
missing one can still explain itself instead of returning a bare "wrong
password". Pair :func:`verify_password` with :func:`needs_rehash` to re-store a
legacy hash in the current scheme at the one moment the plaintext is
legitimately in hand — a successful login — which is what makes a migration
finish rather than become a flag-day.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re

# scrypt cost parameters. N=2^14 keeps the working set (~16 MiB) within
# hashlib's default maxmem while staying memory-hard. The params are stored
# IN each hash string, so raising them later never breaks existing hashes.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_SCHEME = "scrypt"

log = logging.getLogger(__name__)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    # maxmem must exceed the 128*n*r*p-byte working set; give headroom.
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=132 * n * r * p,
    )


def get_password_hash(password: str) -> str:
    """Hash a password into a self-describing ``scrypt$N$r$p$salt$dk`` string."""
    salt = os.urandom(_SALT_BYTES)
    dk = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN)
    return (
        f"{_SCHEME}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


#: Schemes this module can verify. Hashing always uses the current one; older
#: schemes are read-only, which is what lets a product with history adopt this
#: module without invalidating every password made before the switch.
_LEGACY_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2x$", "$2y$")


def bcrypt_available() -> bool:
    """Whether legacy bcrypt hashes can be verified in this environment."""
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        try:
            import passlib.hash  # noqa: F401
        except ImportError:
            return False
    return True


def scheme_of(hashed_password: str) -> str | None:
    """Name the scheme a stored hash was made with, or None if unrecognised.

    Deliberately separate from verification: recognising a bcrypt hash must not
    require bcrypt to be installed, or a deployment missing an optional
    dependency could not even explain itself.
    """
    if not isinstance(hashed_password, str) or not hashed_password:
        return None
    if hashed_password.startswith(_SCHEME + "$"):
        return _SCHEME
    if hashed_password.startswith(_LEGACY_BCRYPT_PREFIXES):
        return "bcrypt"
    return None


def needs_rehash(hashed_password: str) -> bool:
    """Whether this hash should be re-stored in the current scheme.

    True for anything not made by the current scheme. Callers should rehash at
    the one moment the plaintext is legitimately in hand — a successful login —
    which is what turns a migration into something that finishes rather than a
    flag-day.
    """
    return scheme_of(hashed_password) != _SCHEME


def _verify_bcrypt(plain_password: str, hashed_password: str) -> bool:
    """Verify a legacy bcrypt hash, if the optional dependency is present."""
    try:
        import bcrypt

        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except ImportError:
        pass
    try:
        from passlib.hash import bcrypt as passlib_bcrypt

        return bool(passlib_bcrypt.verify(plain_password, hashed_password))
    except ImportError:
        # A deployment error, not a wrong password. Saying so is the difference
        # between fixing an install and hunting a user's typing.
        log.error(
            "cannot verify a legacy bcrypt hash: neither 'bcrypt' nor 'passlib' "
            "is installed, so every password stored before the scrypt migration "
            "will fail to verify. Install the 'bcrypt' extra on this deployment."
        )
        return False
    except ValueError:
        return False


def _verify_scrypt(plain_password: str, hashed_password: str) -> bool:
    try:
        parts = hashed_password.split("$")
        if len(parts) != 6 or parts[0] != _SCHEME:
            return False
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        expected = base64.b64decode(parts[5])
        candidate = _scrypt(plain_password, salt, n, r, p, len(expected))
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a stored hash (constant-time compare).

    Dispatches on the scheme recorded in the hash, so a hash made by an older
    scheme — or with older cost parameters — still validates. Pair with
    :func:`needs_rehash` to upgrade a legacy hash on successful login.
    """
    scheme = scheme_of(hashed_password)
    if scheme == _SCHEME:
        return _verify_scrypt(plain_password, hashed_password)
    if scheme == "bcrypt":
        return _verify_bcrypt(plain_password, hashed_password)
    return False


def validate_password(password: str, *, complexity: str = "standard") -> tuple[bool, str]:
    """Validate password strength.

    ``complexity``: ``"standard"`` (8-128 chars, upper/lower/digit, no common
    passwords) or ``"relaxed"`` (length only). Returns ``(is_valid, message)``;
    message is empty when valid.
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    if len(password) > 128:
        return False, "Password must be less than 128 characters long"
    if complexity == "relaxed":
        return True, ""
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"\d", password):
        return False, "Password must contain at least one number"
    weak_passwords = {"password", "12345678", "qwerty", "abc12345", "password123"}
    if password.lower() in weak_passwords:
        return False, "Password is too common. Please choose a more secure password"
    return True, ""
