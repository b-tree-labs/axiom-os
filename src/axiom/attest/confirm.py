# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Show a person exactly what they will sign, and record their answer (F1 design note, Q2, Q3).

A wrapper over the attest service: every refusal is the service's (ADR-144). Presenting is not
signing, so ``present`` is open to any caller; ``respond`` is a person's answer, and no skill on
an agent surface may call it (``tests/logbook_acceptance/test_agent_rule.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from axiom.extensions.builtins.attest import service
from axiom.extensions.builtins.attest.service import AttestRefused, Response
from axiom.infra.principal import PrincipalContext

__all__ = ["AttestRefused", "Presentation", "Response", "present", "respond"]


@dataclass(frozen=True)
class Presentation:
    """One modality's presentation; ``also`` holds the others presented in the same call."""

    id: str
    digest: str
    rendered: str
    forms: dict[str, str]
    speakable: str
    expires_at: datetime | None
    modality: str
    also: tuple[Presentation, ...] = ()


def _view(p: service.Presentation) -> Presentation:
    return Presentation(
        id=p.presentation_id, digest=p.digest, rendered=p.rendered, forms=dict(p.forms or {}),
        speakable=p.speakable, expires_at=p.expires_at, modality=p.modality,
    )  # fmt: skip


def present(draft_id: str, modalities: Sequence[str]) -> Presentation:
    """Present the draft by each modality in order; the first is returned, the rest in ``.also``.
    They share one digest, so each counts toward the type's ``modalities_all``."""
    if isinstance(modalities, str) or not modalities:
        raise AttestRefused("name at least one modality to present by, as a list")
    first, *rest = (_view(service.present(draft_id, modality=m)) for m in modalities)
    return replace(first, also=tuple(rest))


def respond(
    presentation_id: str,
    *,
    principal: PrincipalContext,
    answer: str,
    via: str,
    transcript: str | None = None,
    latency_ms: int | None = None,
    console_id: str | None = None,
    correction: dict[str, Any] | None = None,
) -> Response:
    """Record the person's answer: ``sign``, ``hold`` or ``ask``. An ``ask`` with a correction
    returns the corrected content's presentation in ``.next_presentation``."""
    return service.respond(
        presentation_id, principal=principal, answer=answer, via=via, transcript=transcript,
        latency_ms=latency_ms, console_id=console_id, correction=correction,
    )  # fmt: skip
