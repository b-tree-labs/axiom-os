# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The conversation store for the web chat surface: Postgres, and moving between two.

The web chat surface needs one store API (create / load / save / list_meta /
conversation / get_detail / set_starred / set_scope / record_feedback / archive).
`DatabaseSessionStore` provides it over the ``chat`` schema (ADR-052). The runtime
database is Postgres everywhere (ADR-174): a laptop runs the local stack's Postgres, a
Site runs its shared one, and the choice is configuration (``AXIOM_DB_URL``), not a
different store. There is no local file fallback; an unreachable database is an error the
caller sees, not a silent switch to a store nobody else can reach.

`reconcile_local_to_shared` is the seam the onboarding flow calls when an install joins a
Site: it copies the conversations from one store into another under the now-proven
principal + granted scope, idempotently and preserving conversation ids, so history follows
the person across the transition without duplication.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.orchestrator.session_db import DatabaseSessionStore


def resolve_conversation_store(engine: Any | None = None) -> DatabaseSessionStore:
    """The store for this deployment: an injected engine (tests), else the ``chat`` schema
    of the configured Postgres (``session_for("chat")``)."""
    if engine is not None:
        return DatabaseSessionStore(engine=engine)
    return DatabaseSessionStore()


def reconcile_local_to_shared(
    local: DatabaseSessionStore,
    shared: DatabaseSessionStore,
    *,
    principal: str,
    site_id: str = "",
    tenant_id: str = "",
) -> int:
    """Move conversations from one store into a Site's on join. Returns the count
    newly migrated.

    Preserves each conversation id (so it is the *same* thread across the move),
    re-owns it to the now-proven principal + granted scope, and is idempotent: a
    conversation already present in `shared` is left as-is (append-only save adds
    no duplicate), so a re-run after a partial transfer is safe. Empty
    conversations are skipped — there is nothing to carry.
    """
    migrated = 0
    for meta in local.list_meta(include_archived=True):
        sid = meta["id"]
        already = shared.conversation(sid) is not None
        session = local.load(sid)
        if session is None or not session.messages:
            continue
        session.principal_id = principal
        session.site_id = site_id
        session.tenant_id = tenant_id
        shared.save(session)  # create-path sets scope; append-only stays idempotent
        if not already:
            migrated += 1
    return migrated


__all__ = ["resolve_conversation_store", "reconcile_local_to_shared"]
