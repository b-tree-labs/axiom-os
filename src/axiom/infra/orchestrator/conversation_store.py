# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pick the conversation store honestly, and move it from local to a Site.

The web chat surface needs one store API (create / load / save / list_meta /
conversation / get_detail / set_starred / set_scope / record_feedback / archive)
whether it runs on a laptop with nothing configured or against a Site's shared
Postgres. `DatabaseSessionStore` provides that API and runs on either engine, so
"local dev" is not a different store — it is the same store over a **local SQLite
file** instead of a shared database. `choose_session_backend` stays the honest
decider: no database configured → a local file (SQLite here, so the full API is
available to the web surface); a database configured → that database.

`reconcile_local_to_shared` is the seam the onboarding flow calls when an install
joins a Site: it copies the local conversations into the Site's store under the
now-proven principal + granted scope, idempotently and preserving conversation
ids, so history follows the person across the transition without duplication.
"""

from __future__ import annotations

import threading
from typing import Any

from axiom.infra.orchestrator.session_backend import choose_session_backend
from axiom.infra.orchestrator.session_db import DatabaseSessionStore

_local_engine: Any | None = None
_local_lock = threading.Lock()


def local_sqlite_engine(path: str | None = None):
    """A process-wide engine for the zero-config local store (a SQLite file).

    Tables are created on first use (no Alembic locally). Memoized so every
    request rides one engine + connection, like the shared path does.
    """
    global _local_engine
    if path is None and _local_engine is not None:
        return _local_engine
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.chat.db_models import Base

    if path is None:
        from axiom import REPO_ROOT

        p = REPO_ROOT / "runtime" / "chat.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{p}"
    else:
        url = f"sqlite:///{path}"
    engine = create_engine(
        url, future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    if path is None:
        with _local_lock:
            if _local_engine is None:
                _local_engine = engine
        return _local_engine
    return engine


def resolve_conversation_store(engine: Any | None = None) -> DatabaseSessionStore:
    """The store for this deployment: injected engine (tests) > configured
    database > zero-config local SQLite. Uses choose_session_backend as the
    decider so the choice matches what the planes surface reports."""
    if engine is not None:
        return DatabaseSessionStore(engine=engine)
    # default_url="" so an unconfigured box resolves to "files" (no localhost-DB
    # assumption); we serve that as a local SQLite file, which gives the web
    # surface the full store API with nothing to install.
    choice = choose_session_backend(default_url="")
    if choice.kind == "database":
        return DatabaseSessionStore()  # engine=None → session_for("chat")
    return DatabaseSessionStore(engine=local_sqlite_engine())


def reconcile_local_to_shared(
    local: DatabaseSessionStore,
    shared: DatabaseSessionStore,
    *,
    principal: str,
    site_id: str = "",
    tenant_id: str = "",
) -> int:
    """Move local conversations into a Site's store on join. Returns the count
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


__all__ = ["local_sqlite_engine", "resolve_conversation_store", "reconcile_local_to_shared"]
