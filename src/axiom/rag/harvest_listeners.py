# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Catch records as they are written, without the writer knowing.

A product should not have to remember to harvest. Every call site that saves a
record would need the call, every new call site would be a silent gap, and the
gap is invisible — a missing card looks exactly like a question with no answer.

So the hook is on the session, not on the call sites. Three events, and the
split between them is the whole correctness argument:

``after_flush`` renders. The object is still loaded and its identity still
resolvable, which is the only moment a *deleted* record can still be described.
Rendering is string formatting, so the write path pays almost nothing.

``after_commit`` queues. Not before — a record that rolls back was never real,
and publishing it would leave retrieval answering from data the database does
not have.

``after_rollback`` discards. Without it the pending set would leak into the next
transaction on the same session and publish rows that never committed.

Installation is explicit and takes a scope resolver. There is no default
resolver: which corpus a record lands in decides who can retrieve it, and a
framework that guesses that is a framework that leaks tenants into each other.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axiom.rag.harvest import HarvestScope, ScopeUndeclared, build_card
from axiom.rag.harvest_queue import OP_DELETE, OP_UPSERT, HarvestIntent, enqueue

log = logging.getLogger(__name__)

_PENDING_KEY = "_axiom_rag_harvest_pending"


def _collect(session, queue_path, scope_resolver, should_harvest) -> None:
    """Render every touched record now, while it is still describable."""
    pending: list[HarvestIntent] = session.info.setdefault(_PENDING_KEY, [])

    for obj, op in [
        *((o, OP_UPSERT) for o in list(session.new)),
        *((o, OP_UPSERT) for o in list(session.dirty)),
        *((o, OP_DELETE) for o in list(session.deleted)),
    ]:
        try:
            if not should_harvest(obj):
                continue
            card = build_card(obj, scope_resolver)
        except ScopeUndeclared:
            # Refusing to guess is the correct outcome, not an error to bury —
            # but it must not fail the caller's write either.
            log.warning(
                "no harvest scope declared for %s; it will not be retrievable",
                type(obj).__name__, exc_info=True,
            )
            continue
        except Exception:  # noqa: BLE001 - harvesting must never fail a write
            log.exception(
                "could not render %s for harvest; the write is unaffected",
                type(obj).__name__,
            )
            continue
        if card is None:
            continue
        pending.append(
            HarvestIntent(
                entity_type=card.entity_type,
                entity_id=card.entity_id,
                op=op,
                corpus=card.scope.corpus,
                owner=card.scope.owner,
                title=card.title,
                # A delete carries no card: there is nothing to index, and
                # keeping the text would only tempt a drain into writing it.
                text="" if op == OP_DELETE else card.text,
            )
        )


def _flush_pending(session, queue_path) -> None:
    """Commit happened; the records are real. Record the intent durably."""
    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    for intent in pending:
        enqueue(queue_path, intent)


def _discard_pending(session) -> None:
    """Rolled back, so nothing was real. Drop it before it leaks forward."""
    session.info.pop(_PENDING_KEY, None)


def install(
    session_target: Any,
    *,
    queue_path: str | Path,
    scope_resolver: Callable[[Any], HarvestScope | None],
    should_harvest: Callable[[Any], bool] = lambda _obj: True,
) -> None:
    """Harvest every record written through *session_target*.

    ``session_target`` is a Session class, sessionmaker or Session instance.
    ``scope_resolver`` decides corpus and owner per record and is required —
    see the module docstring for why there is no default. ``should_harvest``
    narrows what is considered, for the records that are noise rather than
    knowledge.
    """
    from sqlalchemy import event

    def after_flush(session, _flush_context):
        _collect(session, queue_path, scope_resolver, should_harvest)

    def after_commit(session):
        _flush_pending(session, queue_path)

    def after_rollback(session, previous_transaction=None):
        # after_soft_rollback passes the previous transaction as a second
        # argument; accepting it keeps the listener attachable to either event.
        _discard_pending(session)

    event.listen(session_target, "after_flush", after_flush)
    event.listen(session_target, "after_commit", after_commit)
    event.listen(session_target, "after_soft_rollback", after_rollback)


__all__ = ["install"]
