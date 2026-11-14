# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What waits for one person in the logs (unit X1): drafts to sign and entries to co-sign.

:func:`for_person` is what Needs you reads (``notifications.needs_you``): the person's open drafts
as "To sign", and the statements waiting for a co-signature that a role the person holds may give,
never their own, as "Co-sign". Each item links to the Log page and carries no action: signing
happens there, at the card, never from a queue.

Deep links (the Log page's format, recorded in the spec): ``#/log?logbook=<id>&record=<id>`` for a
record and ``#/log?logbook=<id>&draft=<id>`` for a draft.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import registry, service
from . import roles as role_store
from .logbooks import LogbookError

#: The most of each kind one queue lists.
LIMIT = 50


def _state_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir())


def _sites_of(person: str) -> list[str]:
    """The sites the person holds a role at (the role file is per site)."""
    rows = role_store._load(_state_dir())
    return sorted({r["site"] for r in rows if r["principal"] == person and r["roles"]})


def record_href(logbook: str, attestation_id: str) -> str:
    return f"#/log?logbook={logbook}&record={attestation_id}"


def draft_href(logbook: str, draft_id: str) -> str:
    return f"#/log?logbook={logbook}&draft={draft_id}"


def _title(book_id: str, entry_type: str | None, title: str | None) -> str:
    try:
        display = registry.get(book_id).display
    except LogbookError:
        display = book_id
    what = title or (entry_type or "entry").replace("_", " ").lower()
    return f"{what} in {display}"


def for_person(person: str, site: str | None = None) -> list[dict[str, Any]]:
    """The person's "To sign" and "Co-sign" items, oldest first within each."""
    out: list[dict[str, Any]] = []
    for d in service.list_drafts(for_principal=person, site_id=site, limit=LIMIT):
        out.append({
            "id": f"attest:draft:{d['draft_id']}",
            "section": "sign",
            "kind": "sign",
            "title": f"Sign: {_title(d['logbook'], d.get('entry_type'), (d.get('content') or {}).get('title'))}",
            "source": "attest",
            "due": None,
            "priority": "normal",
            "href": draft_href(d["logbook"], d["draft_id"]),
            "action": None,
            "created_at": d.get("created_at"),
        })  # fmt: skip
    state_dir = _state_dir()
    for s in [site] if site else _sites_of(person):
        held = role_store.roles_for(person, s, state_dir=state_dir)
        if not held:
            continue
        for book in registry.all_logbooks():
            if not any(t.cosign for t in book.types):
                continue
            for p in service.pending_cosigns(s, book.id, for_roles=held):
                if p.get("signer") == person:
                    continue  # nobody co-signs their own entry
                out.append({
                    "id": f"attest:cosign:{p['attestation_id']}",
                    "section": "cosign",
                    "kind": "cosign",
                    "title": f"Co-sign: {_title(book.id, p.get('entry_type'), p.get('title'))}",
                    "source": "attest",
                    "due": None,
                    "priority": "normal",
                    "href": record_href(book.id, p["attestation_id"]),
                    "action": None,
                    "created_at": p.get("occurred_at"),
                })  # fmt: skip
                if sum(1 for i in out if i["section"] == "cosign") >= LIMIT:
                    break
    return out


__all__ = ["LIMIT", "draft_href", "for_person", "record_href"]
