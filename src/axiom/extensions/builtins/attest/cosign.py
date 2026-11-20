# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Telling the people who can co-sign that an entry is waiting (unit L4).

When a statement whose type declares ``cosign`` is signed, everyone holding one of the declared
roles at the site, except the signer, is sent one notification through ``notifications.send``
with ``dedup_key = "cosign:<attestation_id>"``. The sender is a seam, as ``obligations.set_sender``
is, so tests capture it; the role holders come from the node's role source. Delivery is best
effort: it runs after the commit and never fails or delays a signature.

See ``docs/working/design-note-l4-cosign.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .logbooks import PLATFORM_ADMIN_ROLES

Sender = Callable[[str, dict[str, Any]], None]


def _state_dir():
    """Where roles are read. A seam, so tests point it at their own directory."""
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def _default_sender(recipient: str, info: dict[str, Any]) -> None:
    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        Priority,
        SendContext,
        send,
    )
    from axiom.governance.classification import Classification

    send(
        SendContext.default(),
        actor=f"@attest:{info['site_id']}",
        recipient=recipient,
        payload=NotificationPayload(
            summary=f"Co-signature needed: {info['entry_type']} #{info['seq']}",
            body=f"{info['title'] or info['entry_type']} in {info['logbook']}, signed by "
            f"{info['signer']}, waits for a {' or '.join(info['cosign_roles'])} to co-sign.",
            metadata={k: str(v) for k, v in info.items() if k != "cosign_roles"},
        ),
        classification=Classification.INTERNAL,
        priority=Priority.HIGH,
        dedup_key=info["dedup_key"],
    )


_sender: Sender = _default_sender


def set_sender(sender: Sender) -> None:
    """Seam: where notifications go (tests capture them)."""
    global _sender
    _sender = sender


def reset_sender() -> None:
    global _sender
    _sender = _default_sender


def recipients(site_id: str, cosign_roles: tuple[str, ...], *, except_principal: str) -> list[str]:
    """Who holds one of ``cosign_roles`` at ``site_id``, except the signer and never by an
    administration role alone."""
    from .roles import assignments

    wanted = set(cosign_roles) - PLATFORM_ADMIN_ROLES
    return [
        row["principal"]
        for row in assignments(site_id, state_dir=_state_dir())
        if row["principal"] != except_principal and wanted & set(row["roles"])
    ]


def request(brief: dict[str, Any], cosign_roles: tuple[str, ...]) -> list[str]:
    """Tell each role holder once that ``brief`` waits for a co-signature; returns who was
    told. Best effort: a failing sender is swallowed, because the signature is committed."""
    info = {
        **brief,
        "cosign_roles": list(cosign_roles),
        "dedup_key": f"cosign:{brief['attestation_id']}",
    }
    told: list[str] = []
    for who in recipients(brief["site_id"], cosign_roles, except_principal=brief["signer"]):
        try:
            _sender(who, info)
            told.append(who)
        except Exception:  # noqa: BLE001 - the record is the truth; delivery is best effort
            continue
    return told


__all__ = ["Sender", "recipients", "request", "reset_sender", "set_sender"]
