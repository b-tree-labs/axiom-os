# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Ported from SoilMetrix, Inc (dba Aiterra.ag) by Benjamin Booth, CEO.

"""Password hashing and validation.

Lift note: the SoilMetrix original used ``passlib[bcrypt]``. Since webapp is
greenfield (no existing password hashes to verify) and neither passlib nor
bcrypt is a current Axiom dependency, this uses stdlib ``hashlib.scrypt`` behind
the same interface — no new dependencies, memory-hard, and self-describing hash
strings. TODO(dedup): if/when we unify SoilMetrix's existing user table into
Axiom, add a passlib/bcrypt verify path so pre-existing bcrypt hashes validate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a stored hash (constant-time compare).

    Cost params are read from the hash string, so hashes made with older/newer
    parameters still validate.
    """
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
