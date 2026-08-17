# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Step-up: elevate the acting principal to meet a posture floor (ENF-2, ADR-074 §4).

Decisions (2026-06-11): **hybrid** — when an interactive caller is present, step
up inline (the OS keychain unlock / SSO sign-in is the prompt); when headless,
raise ``StepUpRequired`` with a precise "run X, then retry". **Session-persistent**
— an elevation is cached for the process; ``require_mfa`` high-value releases
still demand a fresh tap (handled at the KEEP boundary, not here).

The runtime calls ``step_up`` *before* a floored operation, then hands KEEP the
elevated principal; KEEP enforces the floor (ENF-4). Clean separation: elevate
here, enforce there.
"""

from __future__ import annotations

import sys
from typing import Optional

from axiom.infra.principal import PrincipalContext, attested, open_principal

# Session-persistent elevation cache (per process): posture -> elevated principal.
_ELEVATED: dict[str, PrincipalContext] = {}


class StepUpRequired(Exception):
    """A higher posture is needed but can't be obtained non-interactively.
    ``remediation`` is the exact command to run, then retry."""

    def __init__(self, posture: str, remediation: str) -> None:
        super().__init__(f"posture '{posture}' required: {remediation}")
        self.posture = posture
        self.remediation = remediation


def clear_step_up() -> None:
    """Drop cached elevations (logout / tests)."""
    _ELEVATED.clear()


def step_up(
    target: str,
    *,
    current: Optional[PrincipalContext] = None,
    interactive: Optional[bool] = None,
    custody: object = None,
) -> PrincipalContext:
    """Return a principal meeting ``target`` (elevating if needed).

    Raises ``StepUpRequired`` when elevation needs an interaction that isn't
    available (headless).
    """
    current = current or open_principal()
    if current.meets(target):
        return current

    cached = _ELEVATED.get(target)
    if cached is not None and cached.meets(target):
        return cached

    if interactive is None:
        interactive = sys.stdin.isatty()

    if target == "attested":
        if not interactive:
            raise StepUpRequired(
                "attested", "run `axi identity init` to unlock your local identity, then retry"
            )
        # The OS keychain unlock (Touch ID / login) IS the interactive prompt.
        from axiom.vega.identity.local import load_or_create_local_keypair

        principal = attested(load_or_create_local_keypair(custody=custody).public_bytes)
        _ELEVATED["attested"] = principal
        return principal

    if target in ("sso", "service"):
        raise StepUpRequired(
            target, "run `axi auth login --provider entra --tenant <id> --client-id <id>`, then retry"
        )

    raise StepUpRequired(target, f"unsupported step-up target '{target}'")


__all__ = ["StepUpRequired", "clear_step_up", "step_up"]
