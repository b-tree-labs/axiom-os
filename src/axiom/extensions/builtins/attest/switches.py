# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Log's product switches (ADR-210 flags this extension declares): shift logs, Paper view and
log acknowledgements. Each is a permanent switch a site turns on in Settings, off by default.

``shift_logs``            A shift log is a logbook that declares a site-clock period
                          (``[period.<id>]``, #1806). Off, such a log does not exist for anyone:
                          every listing omits it, a read of it is the not-found a missing log gets,
                          and signing, drafting or sealing in it is refused with :data:`SHIFT_LOGS_OFF`.
                          Records already stored are kept.
``log_paper``             The Paper view of a log. Read by the web app; the server only declares it.
``log_acknowledgements``  Off, an acknowledgement (record kind ``ack``) is neither drafted nor
                          signed (:data:`ACKS_OFF`), and nothing asks anyone to acknowledge: no
                          unacknowledged list, no acknowledgement draft waiting in Needs you, no open
                          item in a close-out. Acknowledgements already signed stay readable.

A site that used acknowledgements before the switch existed keeps them: :func:`keep_acks_on_upgrade`
writes a node override ``on`` once, with a reason, when there is no override yet and an
acknowledgement record or an open acknowledgement draft exists. It runs when the attest routes
mount and on the first read of the switch in a process; it is idempotent.

A value is read from the flag resolver for this node (and the default account), cached for one
second so a page that lists many logs asks once, while an admin who turns a switch on sees it on the
next page they open. ``AXIOM_FLAG_<KEY>`` pins it, as for every flag.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

SHIFT_LOGS = "shift_logs"
LOG_PAPER = "log_paper"
LOG_ACKNOWLEDGEMENTS = "log_acknowledgements"

SHIFT_LOGS_OFF = "Shift logs are off on this site; an admin can turn them on in Settings."
ACKS_OFF = "Log acknowledgements are off on this site; an admin can turn them on in Settings."
KEPT_ON_REASON = "kept on: this site already used log acknowledgements"
#: Who the upgrade's override is recorded as being by.
UPGRADE_BY = "@attest:upgrade"

#: How long a resolved value is reused (seconds): one page's requests, never the admin's next page.
TTL = 1.0

_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()
_upgrade_checked = False


def reset() -> None:
    """Forget cached values and the upgrade check (tests)."""
    global _upgrade_checked
    with _lock:
        _cache.clear()
        _upgrade_checked = False


def _resolve(key: str) -> bool:
    from axiom.extensions.builtins.features.resolver import FlagService
    from axiom.extensions.builtins.features.skills._flags import default_account, this_node

    return FlagService().is_enabled(key, node=this_node(), account=default_account())


def is_on(key: str) -> bool:
    """Whether the switch ``key`` is on here. An env pin wins; an unreadable store is the default (off)."""
    from axiom.extensions.builtins.features.resolver import env_override

    pinned = env_override(key)
    if pinned is not None:
        return pinned == "on"
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < TTL:
        return hit[1]
    try:
        value = _resolve(key)
    except Exception:  # noqa: BLE001 - an unreadable flag store leaves the default (off)
        value = False
    with _lock:
        _cache[key] = (now, value)
    return value


def shift_logs_on() -> bool:
    return is_on(SHIFT_LOGS)


def upgrade() -> None:
    """Run the one-shot upgrade (:func:`keep_acks_on_upgrade`) once in this process. Called when the
    attest routes mount and before the first read of ``log_acknowledgements``; never raises."""
    global _upgrade_checked
    if _upgrade_checked:
        return
    _upgrade_checked = True
    if os.environ.get("AXIOM_FLAG_" + LOG_ACKNOWLEDGEMENTS.upper()):
        return  # pinned: nothing to keep
    try:
        keep_acks_on_upgrade()
    except Exception as exc:  # noqa: BLE001 - retried by the next process
        log.warning("log acknowledgements: the upgrade check could not run: %s", exc)


def acks_on() -> bool:
    upgrade()
    return is_on(LOG_ACKNOWLEDGEMENTS)


def is_shift_log(book: Any) -> bool:
    """A shift log declares a site-clock period (``[period.<id>]``)."""
    return bool(getattr(book, "periods", None))


def refuse_acks() -> None:
    from .service import AttestRefused

    if not acks_on():
        raise AttestRefused(ACKS_OFF)


# ------------------------------------------------------------------ upgrade


def _ack_history() -> bool:
    """Whether this node has an acknowledgement record or an open acknowledgement draft."""
    from sqlalchemy import select

    from . import store
    from .db_models import AttestDraft, AttestRecord

    with store.session_scope() as s:
        if s.execute(select(AttestRecord.attestation_id).where(AttestRecord.kind == "ack").limit(1)).first():
            return True
        return bool(s.execute(
            select(AttestDraft.draft_id).where(AttestDraft.kind == "ack", AttestDraft.status == "open").limit(1)
        ).first())


def keep_acks_on_upgrade(
    *, service: Any = None, has_history: Any = None, node: str | None = None, state_dir: Any = None
) -> dict[str, Any] | None:
    """Write ``log_acknowledgements = on`` for this node once, when the site already used them.

    Nothing is written when any override of the flag exists (at any scope: an explicit choice is
    never touched), when the flag is pinned by the environment, or when there is no history.
    Returns the stored row when one was written, else None. The write is recorded in the action
    audit exactly as ``flags.set`` records one (``change = {kind: flag, ...}``).
    """
    from axiom.extensions.builtins.features.resolver import FlagService, env_override

    if env_override(LOG_ACKNOWLEDGEMENTS) is not None:
        return None
    svc = service or FlagService()
    if LOG_ACKNOWLEDGEMENTS not in svc.get_registry():
        return None
    rows = svc.overrides.rows(LOG_ACKNOWLEDGEMENTS) if hasattr(svc.overrides, "rows") else []
    if rows:
        return None
    if not (has_history or _ack_history)():
        return None
    if node is None:
        from axiom.extensions.builtins.features.skills._flags import this_node

        node = this_node()
    row = svc.set_override("node", node, LOG_ACKNOWLEDGEMENTS, "on", by=UPGRADE_BY, reason=KEPT_ON_REASON)
    with _lock:
        _cache.pop(LOG_ACKNOWLEDGEMENTS, None)
    try:
        from axiom.infra.audit_trail import _shown_value, record_invocation

        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        subject = f"{LOG_ACKNOWLEDGEMENTS} (node {node})"
        record_invocation(
            state_dir=state_dir,
            skill="flags.set",
            params={"key": LOG_ACKNOWLEDGEMENTS, "value": "on", "scope": "node", "scope_id": node,
                    "reason": KEPT_ON_REASON},
            principal=UPGRADE_BY,
            site=os.environ.get("AXIOM_SITE") or None,
            outcome="ok",
            errors_count=0,
            change={"kind": "flag", "subject": subject, "old": None,
                    "new": _shown_value("flag", subject, (row or {}).get("value", "on"))},
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - the override stands; the audit failure is logged
        log.warning("log acknowledgements: the upgrade's audit record could not be written: %s", exc)
    log.info("log acknowledgements kept on for node %s: this site already used them", node)
    return row


__all__ = [
    "ACKS_OFF",
    "KEPT_ON_REASON",
    "LOG_ACKNOWLEDGEMENTS",
    "LOG_PAPER",
    "SHIFT_LOGS",
    "SHIFT_LOGS_OFF",
    "acks_on",
    "is_on",
    "is_shift_log",
    "keep_acks_on_upgrade",
    "refuse_acks",
    "reset",
    "shift_logs_on",
    "upgrade",
]
