# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Discovery value types. A Finding never carries the credential."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


def fingerprint(value: str) -> str:
    """Stable, non-reversing identity for a credential value.

    Truncated SHA-256. Enough to recognise the same secret in two places and to
    match it against the store, and useless to anyone who obtains it — a
    discovery report is a document about secrets that must be safe to paste
    into a ticket.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def credential_target(text: str) -> str | None:
    """What a credential is FOR, with the secret stripped.

    ``postgresql://triga_ro:hunter2@localhost:5432/axiom_db`` becomes
    ``postgresql://triga_ro@localhost:5432/axiom_db``.

    This is the identity two locations must share to be compared. Correlating
    by *variable name* is not enough and misses the common case: the same
    database reached through ``TRIGA_TELEMETRY_DSN`` in a unit file and
    ``RAG_DB_URL`` in a service's environment is one credential wearing two
    names, and a rotation that updates one leaves the other stale and silent.
    """
    m = re.search(r"(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<user>[^:/@\s]+):[^@/\s]+@(?P<rest>[^\s\"']+)", text, re.I)
    if not m:
        return None
    return f"{m.group('scheme').lower()}://{m.group('user')}@{m.group('rest')}"


@dataclass(frozen=True)
class RawHit:
    """Internal: carries the value, never leaves the probe layer."""

    locator: str
    value: str
    probe: str
    detail: str = ""
    target: str | None = None
    """What the credential authenticates to, secret stripped — see
    :func:`credential_target`. Present only for URL-shaped credentials."""


@dataclass(frozen=True)
class Finding:
    locator: str
    """Where it was found — a path, a remote name, a unit file."""
    probe: str
    matcher: str
    fingerprint: str
    managed: bool | None = None
    """True if the fingerprint is in the store, False if not, None if unchecked."""
    detail: str = ""
    hints: tuple[str, ...] = field(default_factory=tuple)
    """What to do about it, in the caller's terms."""

    @property
    def severity(self) -> str:
        """Unmanaged material is the finding; managed material is inventory."""
        if self.managed is False:
            return "unmanaged"
        if self.managed is True:
            return "consumer"
        return "unknown"

    def to_dict(self) -> dict:
        return {
            "locator": self.locator,
            "probe": self.probe,
            "matcher": self.matcher,
            "fingerprint": self.fingerprint,
            "managed": self.managed,
            "severity": self.severity,
            "detail": self.detail,
            "hints": list(self.hints),
        }


__all__ = ["Finding", "RawHit", "credential_target", "fingerprint"]
