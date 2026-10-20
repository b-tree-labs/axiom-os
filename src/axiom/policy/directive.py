# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Directive — immutable scoped policy record."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Directive:
    id: str
    issuer: str  # principal handle of the human who issued it
    targets: tuple[str, ...]  # resolved agent/principal identifiers
    body: str  # natural-language or structured directive content
    scope_kind: str  # period | classroom | course | session
    scope_id: str  # id of the scoping artifact
    issued_at: float
    active: bool = True
    revoked_at: float | None = None
    revocation_reason: str | None = None

    def revoke(self, *, now: float, reason: str | None = None) -> Directive:
        return replace(self, active=False, revoked_at=now, revocation_reason=reason)

    def expire(self, *, now: float) -> Directive:
        return replace(self, active=False, revoked_at=now, revocation_reason="scope_ended")
