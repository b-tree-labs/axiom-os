# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Propose an entry for a person to sign (F1 design note, questions 1 and 9).

A wrapper over the attest service: no rule here replaces one there. Software proposes and a
person signs (ADR-142), so a draft from an agent or integration may not carry an observed
value (ADR-144), and a caller that says a person typed the values names that person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.attest import registry, service
from axiom.extensions.builtins.attest.logbooks import LogbookError
from axiom.extensions.builtins.attest.service import AttestRefused
from axiom.infra.principal import PrincipalContext

from ._site import site_or_refuse

__all__ = ["AttestRefused", "Draft", "create", "get"]


@dataclass(frozen=True)
class Draft:
    id: str
    site_id: str
    logbook: str
    entry_type: str
    meaning: str
    origin: str
    for_principal: str
    status: str
    content: dict[str, Any]
    provenance: dict[str, Any]


def get(draft_id: str) -> Draft:
    d = service.draft(draft_id)
    return Draft(
        id=d["draft_id"], site_id=d["site_id"], logbook=d["logbook"], entry_type=d["entry_type"],
        meaning=d["meaning"], origin=d["origin"], for_principal=d["for_principal"],
        status=d["status"], content=d["content"], provenance=d["provenance"],
    )  # fmt: skip


def _only_meaning(logbook: str, entry_type: str) -> str:
    try:
        meanings = registry.get(logbook).type(entry_type).meanings
    except LogbookError as exc:
        raise AttestRefused(str(exc)) from None
    if len(meanings) != 1:
        raise AttestRefused(f"{logbook}.{entry_type} carries {list(meanings)}; name the meaning")
    return meanings[0]


def create(
    logbook: str,
    entry_type: str,
    content: dict[str, Any],
    origin: str,
    for_principal: str,
    *,
    site: str | None = None,
    meaning: str | None = None,
    entered_by: PrincipalContext | None = None,
) -> Draft:
    """``origin`` is ``"human"`` when ``entered_by`` typed the values, and ``entered_by`` must be
    ``for_principal``; otherwise it names the software (``agent:<id>``, ``integration:<ext>``)."""
    if origin == "human":
        if entered_by is None or entered_by.handle != for_principal:
            raise AttestRefused("origin 'human' needs entered_by, the person the draft is for")
    elif entered_by is not None:
        raise AttestRefused(f"origin {origin!r} is software; a person is not its entered_by")
    draft_id = service.create_draft(
        site_id=site_or_refuse(site),
        logbook=logbook,
        entry_type=entry_type,
        meaning=meaning if meaning is not None else _only_meaning(logbook, entry_type),
        content=content,
        origin=origin,
        for_principal=for_principal,
    )
    return get(draft_id)
