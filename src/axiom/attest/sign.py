# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Sign the presentation a person answered ``sign`` (F1 design note, questions 4 and 8).

A person signs; software never does (ADR-142). The caller names the person; the node supplies
their roles and its signing key, so neither can be passed in. Every check is the service's: the
person the draft is for, their recorded ``sign`` answer, an unchanged digest, a signing posture,
a role the logbook lists. No skill on an agent surface may import this module
(``tests/logbook_acceptance/test_agent_rule.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from axiom.extensions.builtins.attest import service, signing
from axiom.extensions.builtins.attest.roles import roles_for
from axiom.extensions.builtins.attest.service import AttestRefused, Signatory, SignResult
from axiom.infra.paths import get_user_state_dir
from axiom.infra.principal import PrincipalContext

__all__ = ["AttestRefused", "SignResult", "cosign", "sign", "supplement"]


def sign(
    presentation_id: str,
    *,
    principal: PrincipalContext,
    grant: str | None = None,
    occurred_at: datetime | None = None,
) -> SignResult:
    """Chain the record. ``grant`` is the signing grant (ADR-146); a type that needs a fresh
    sign-in or presence, or refuses personal devices, is refused without one."""
    site = service.presentation_context(presentation_id)["site_id"]
    who = Signatory(
        principal=principal,
        roles=roles_for(principal.handle, site, state_dir=get_user_state_dir()),
        display=principal.handle,
    )
    return service.sign(
        presentation_id, signatory=who, signer=signing.signer(), source="library",
        occurred_at=occurred_at, grant=grant, grant_keys=signing.public_keys(),
    )  # fmt: skip


def supplement(target: str, reason: str, content: dict[str, Any], grant: str) -> SignResult:
    """Sign a supplement to the signed record ``target`` without changing it (unit L2).

    A grant is minted for one presentation, so this is the last step, not the only one: create
    the draft with ``drafts.supplement``, present it, record the person's ``sign`` answer, get a
    grant for that presentation (ADR-146), then call this. It refuses unless the grant's
    presentation is of a supplement to ``target`` for ``reason`` with exactly ``content``, and
    it signs as the person the grant names; every other check is ``service.sign``'s."""
    from axiom.extensions.builtins.attest import grants

    claims = grants.read(grant, signing.public_keys())
    ctx = service.presentation_context(claims["presentation_id"])
    d = service.draft(ctx["draft_id"])
    asked = service.normalised_content(d["site_id"], d["logbook"], d["entry_type"], content)
    if (d["kind"], d["target"], d["reason"]) != ("supplement", target, reason) or (
        d["content"] != asked
    ):
        raise AttestRefused(
            "the grant is for a presentation of another draft; present this supplement and "
            "request a grant for it"
        )
    principal = PrincipalContext(
        handle=claims["principal"], posture=claims["posture"], assured=True, idp=claims.get("idp")
    )
    return sign(claims["presentation_id"], principal=principal, grant=grant)


def cosign(target: str, grant: str) -> SignResult:
    """Sign a co-signature of the statement ``target`` (unit L4).

    As :func:`supplement`: the grant names one presentation, so this is the last step after
    ``drafts.cosign``, ``confirm.present`` and ``confirm.respond``. It refuses unless the grant's
    presentation is of a co-signature of ``target``, and signs as the person the grant names;
    the freshness window, the device rule and separation of duties are the service's."""
    from axiom.extensions.builtins.attest import grants

    claims = grants.read(grant, signing.public_keys())
    ctx = service.presentation_context(claims["presentation_id"])
    d = service.draft(ctx["draft_id"])
    if (d["kind"], d["target"]) != ("cosign", target):
        raise AttestRefused(
            "the grant is for a presentation of another draft; present this co-signature and "
            "request a grant for it"
        )
    principal = PrincipalContext(
        handle=claims["principal"], posture=claims["posture"], assured=True, idp=claims.get("idp")
    )
    return sign(claims["presentation_id"], principal=principal, grant=grant)
