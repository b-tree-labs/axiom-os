# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rotating location codes (ADR-146, presence).

A location has a secret in the vault. Its fixed display shows a six-digit code
derived from that secret and the current 30-second window (the HOTP
truncation of RFC 4226 over HMAC-SHA256). A portable device proves it is in
the room by entering the code it can see. The current and the previous window
are accepted, so a code read at the end of a window still works.

A location with no secret cannot be proven by code; nothing falls back.
The secret is read from the vault at the moment of checking and is never
logged, returned or put in a record.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime

WINDOW_SECONDS = 30
DIGITS = 6


def _vault_name(site_id: str, location: str) -> str:
    return f"attest.location.{site_id}.{location}"


def _vault_secret(site_id: str, location: str) -> bytes | None:
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.paths import get_user_state_dir

    store = ForeignCredentialStore(get_user_state_dir())
    name = _vault_name(site_id, location)
    if not store.exists(name):
        return None
    with store.get(name) as secret:
        return bytes.fromhex(secret.as_str())


_provider: Callable[[str, str], bytes | None] = _vault_secret


def set_secret_provider(provider: Callable[[str, str], bytes | None]) -> None:
    global _provider
    _provider = provider


def reset_secret_provider() -> None:
    global _provider
    _provider = _vault_secret


def init_location(site_id: str, location: str) -> None:
    """Create a location's secret in the vault. Refuses to overwrite: rotating
    one invalidates the code on its display, which is a deliberate act."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.paths import get_user_state_dir

    store = ForeignCredentialStore(get_user_state_dir())
    name = _vault_name(site_id, location)
    if store.exists(name):
        raise ValueError(f"location {location!r} at {site_id} already has a secret")
    store.set(name, secrets.token_hex(32).encode("ascii"), provider="attest")


def _window(now: datetime) -> int:
    return int(now.timestamp()) // WINDOW_SECONDS


def code(secret: bytes, location: str, at: datetime) -> str:
    return _code_for_window(secret, location, _window(at))


def _code_for_window(secret: bytes, location: str, window: int) -> str:
    mac = hmac.new(secret, f"{location}:{window}".encode(), hashlib.sha256).digest()
    offset = mac[-1] & 0x0F
    value = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_code(site_id: str, location: str, now: datetime | None = None) -> str | None:
    """What the location's fixed display shows now, or ``None`` with no secret."""
    secret = _provider(site_id, location)
    if secret is None:
        return None
    return code(secret, location, now or datetime.now(UTC))


def verify_code(site_id: str, location: str, entered: str, *, now: datetime | None = None) -> bool:
    secret = _provider(site_id, location)
    if secret is None or not entered:
        return False
    w = _window(now or datetime.now(UTC))
    return any(
        hmac.compare_digest(_code_for_window(secret, location, x), entered) for x in (w, w - 1)
    )


__all__ = [
    "DIGITS",
    "WINDOW_SECONDS",
    "code",
    "current_code",
    "init_location",
    "reset_secret_provider",
    "set_secret_provider",
    "verify_code",
]
