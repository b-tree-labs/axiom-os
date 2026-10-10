# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Connector detection — the uniform probe every connector type shares (ADR-068).

A best-in-class onboarding opens on *what's already true*, not a blank form.
Every connector (chat channel, SMS, cloud/identity surface, code host, store,
data source) answers one question the same way:

    detect() -> DetectResult(state, summary, next_action)

``state`` is one of four:

- ``CONFIGURED`` — valid creds, a live (or last-known-good) health check passes.
  Re-running is a quiet no-op; the wizard says "nothing to do".
- ``PARTIAL``    — some setup exists (creds present, not yet verified; or app
  created but Request URL unwired). The wizard *resumes* from the gap.
- ``BROKEN``     — creds present but auth fails / reconnect_required. The wizard
  *self-heals* the bad part only.
- ``ABSENT``     — nothing configured. Full onboarding.

``default_detect`` derives the state generically from three cheap probes
(stored-secret presence, the status store's last outcome, an optional live
health check) so EVERY connector gets detection for free. Richer vendors
override ``detect`` to add account/session signals (Slack CLI auth,
``gh auth status``, ``az`` login) — but the state model + branching stay
uniform.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConnectorState(str, Enum):
    CONFIGURED = "configured"
    PARTIAL = "partial"
    BROKEN = "broken"
    ABSENT = "absent"


@dataclass(frozen=True)
class DetectResult:
    state: ConnectorState
    summary: str
    next_action: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """True when the operator needs to do something (not CONFIGURED)."""
        return self.state is not ConnectorState.CONFIGURED


def default_detect(
    *,
    vendor: str,
    name: str,
    secrets_present: bool,
    secrets_complete: bool,
    last_reconnect_required: bool | None,
    last_ok: bool | None,
    health: Callable[[], bool] | None = None,
) -> DetectResult:
    """Derive a :class:`DetectResult` from the three universal probes.

    ``secrets_present`` / ``secrets_complete``: whether *any* / *all* required
    secrets exist in the backend. ``last_*``: the status store's most recent
    outcome (``None`` if never run). ``health``: optional live check (e.g.
    ``auth.test``); when provided it is authoritative over stale status.
    """
    if not secrets_present and last_ok is None and last_reconnect_required is None:
        return DetectResult(
            ConnectorState.ABSENT,
            summary=f"{vendor} is not configured.",
            next_action=f"Run the connect wizard to add {vendor!r}.",
        )

    if secrets_present and not secrets_complete:
        return DetectResult(
            ConnectorState.PARTIAL,
            summary=f"{vendor} setup is incomplete (some credentials missing).",
            next_action="Resume the wizard to finish the remaining fields.",
        )

    # Live health is authoritative when available.
    if health is not None:
        try:
            healthy = bool(health())
        except Exception:
            healthy = False
        if healthy:
            return DetectResult(
                ConnectorState.CONFIGURED,
                summary=f"{vendor} ({name}) is connected and healthy.",
            )
        return DetectResult(
            ConnectorState.BROKEN,
            summary=f"{vendor} ({name}) credentials present but the live check failed.",
            next_action="Reconnect to refresh the credential.",
        )

    # No live check — fall back to the last recorded outcome.
    if last_reconnect_required:
        return DetectResult(
            ConnectorState.BROKEN,
            summary=f"{vendor} ({name}) needs reconnect (last run flagged it).",
            next_action="Reconnect to refresh the credential.",
        )
    if last_ok:
        return DetectResult(
            ConnectorState.CONFIGURED,
            summary=f"{vendor} ({name}) configured; last run succeeded.",
        )
    if secrets_present:
        return DetectResult(
            ConnectorState.PARTIAL,
            summary=f"{vendor} ({name}) has credentials but hasn't been verified.",
            next_action="Run a verification to confirm it works.",
        )
    return DetectResult(
        ConnectorState.ABSENT,
        summary=f"{vendor} is not configured.",
        next_action=f"Run the connect wizard to add {vendor!r}.",
    )


def detect_connector(handler: Any, **probes: Any) -> DetectResult:
    """Detect a connector's state, preferring a vendor ``detect`` override.

    A handler that implements ``detect(**probes) -> DetectResult`` (to add
    account/session signals) is used as-is; otherwise the generic
    :func:`default_detect` runs. This keeps detection uniform across every
    connector type while letting richer vendors enhance it.
    """
    override = getattr(handler, "detect", None)
    if callable(override):
        return override(**probes)
    return default_detect(vendor=getattr(handler, "vendor", "connector"), **probes)


__all__ = ["ConnectorState", "DetectResult", "default_detect", "detect_connector"]
