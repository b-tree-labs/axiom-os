# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The seam through which a held action's decision becomes a signed record (logbook unit X3).

``infra`` defines the shape; the attest extension fills it when it binds, so this package never
imports an extension. With no signer installed, or with the installed signer inactive (its flag
off), every decision path keeps the behaviour it had before: ``approval.approve`` and
``approval.reject`` decide directly and name the decider.

With an active signer, a decision is made only by a person signing an ``ACTION_DECISION`` in the
``approvals`` log, and paths that cannot carry a signature (an email or SMS token, a channel
button) decide nothing and answer with the address of the card where the person signs.
"""

from __future__ import annotations

from typing import Any, Protocol

from axiom.infra.orchestrator.actions import Action

__all__ = [
    "DecisionSigner",
    "active",
    "attestation",
    "card_address",
    "reset_signer",
    "set_signer",
    "signer",
]


class DecisionSigner(Protocol):
    def active(self) -> bool:
        """Whether signed decisions are on for this node."""

    def decide(self, gate: Any, action: Action, *, approving: bool, reason: str, ctx: Any) -> Any:
        """Present the decision to the person at ``ctx`` and sign it; returns a ``SkillResult``.
        Nothing is decided unless a person signs."""

    def card_address(self, action_id: str) -> str:
        """Where a person opens the card to decide ``action_id``."""

    def record(self, attestation_id: str) -> Any:
        """The signed record ``attestation_id`` as a mapping, or None when there is none."""


_signer: DecisionSigner | None = None


def set_signer(s: DecisionSigner | None) -> None:
    global _signer
    _signer = s


def reset_signer() -> None:
    set_signer(None)


def signer() -> DecisionSigner | None:
    return _signer


def active() -> DecisionSigner | None:
    """The installed signer when it is active, else ``None`` (the legacy path)."""
    s = _signer
    try:
        return s if s is not None and s.active() else None
    except Exception:  # noqa: BLE001 - an unreadable flag must not open a path; the signer decides
        return s


def card_address(action_id: str) -> str:
    """The card a person decides ``action_id`` on: the installed signer's, or Control's route."""
    s = _signer
    if s is not None:
        return s.card_address(action_id)
    return f"/#/control?decide={action_id}"


def attestation(attestation_id: str) -> Any:
    """The signed record ``attestation_id`` through the installed signer, or None (no signer, no
    record, or an unreadable store). Used to check a link before writing it (logbook unit X4)."""
    s = _signer
    if s is None or not attestation_id:
        return None
    try:
        return s.record(attestation_id)
    except Exception:  # noqa: BLE001 - an unreadable record is no record; nothing is linked
        return None
