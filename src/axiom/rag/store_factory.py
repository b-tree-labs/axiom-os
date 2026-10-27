# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Factory for creating the appropriate RAG store backend.

Postgres is the runtime store (ADR-174). A ``sqlite://`` URL is refused unless a test has
opted in with :func:`allow_sqlite_for_tests`: the SQLite store remains only so tests can run
without a server, and a runtime that quietly accepted it would be the fallback this decision
removed.
"""

from __future__ import annotations

import os
from typing import NamedTuple

_sqlite_allowed = False

#: The retrieval-store setting the chat, the MCP primitive and ``axi rag`` share.
RAG_URL_SETTING = "rag.database_url"


class StoreUrl(NamedTuple):
    """Which retrieval store a surface opens, and why: ``origin`` says where the URL came from."""

    url: str
    origin: str
    site: str = ""  # the bound site, when the URL is the binding's face


def resolve_store_url(*, honour_env: bool = True) -> StoreUrl:
    """The retrieval store URL: ``DATABASE_URL``, then ``rag.database_url``, then the site binding.

    A laptop bound to a site node (``site bind``) keeps that node's documents where its data
    already comes from: with no store configured, retrieval follows the binding's face URL, and
    :func:`create_store` sends the binding's key to it. A configured store always wins, and a
    ``DATABASE_URL`` in the environment counts as configured even for a surface that does not
    read it (``honour_env=False``), so a node whose database is set never starts asking a binding.

    Configuration only: no network, never raises.
    """
    env = os.environ.get("DATABASE_URL") or ""
    if env and honour_env:
        return StoreUrl(env, "DATABASE_URL env")
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        setting = SettingsStore().get(RAG_URL_SETTING, "") or ""
    except Exception:  # noqa: BLE001 — settings absent is unconfigured
        setting = ""
    if setting:
        return StoreUrl(setting, f"{RAG_URL_SETTING} setting")
    if env:
        return StoreUrl("", "")
    try:
        from axiom.extensions.builtins.data_platform.read_target import current_binding

        bound = current_binding()
    except Exception:  # noqa: BLE001 — an unreadable binding is no binding
        bound = None
    if bound is not None:
        site, face = bound
        return StoreUrl(face, f"site binding {site}", site)
    return StoreUrl("", "")


def _bound_key_for(url: str) -> str:
    """The binding's key for ``url`` (see ``read_target.bound_key_for``); empty on any failure."""
    try:
        from axiom.extensions.builtins.data_platform.read_target import bound_key_for

        return bound_key_for(url)
    except Exception:  # noqa: BLE001 — no vault, no binding: no key, never a crash
        return ""


def allow_sqlite_for_tests(allowed: bool = True) -> None:
    """Tests opt in to the SQLite store; production never calls this."""
    global _sqlite_allowed
    _sqlite_allowed = allowed


def create_store(database_url: str, *, ensure_schema: bool = True):
    """Create a RAG store from a store URL.

    ``ensure_schema=False`` builds a store that will NOT run migration DDL on
    connect. A **monitoring** caller needs this: schema-ensure runs
    ``CREATE TABLE``, which fails with ``InsufficientPrivilege`` against the
    read-only role monitoring should be using — and a health check has no
    business migrating the schema it is inspecting. Honoured by the postgres
    backend; the others do not run migration DDL on construction.

    Supported schemes:
      - ``postgresql://`` / ``postgres://`` — local pgvector store
      - ``sqlite:///``                      — tests only (see ``allow_sqlite_for_tests``)
      - ``http://`` / ``https://``          — remote peer's retrieval endpoint
        (a node whose corpus lives on a peer; see ``remote_store``)
    """
    if not (database_url or "").strip():
        # No store configured is not a bad scheme. Saying "Unsupported store
        # URL scheme: ://" sent people looking for a typo that was not there.
        raise ValueError(
            "no RAG store is configured: set AXIOM_RAG_DSN to postgresql://… or an http(s):// peer"
        )
    if database_url.startswith("postgresql+"):
        # The platform's own spelling names the driver (require_named_driver);
        # psycopg2 takes the plain libpq form, and the meaning is the same.
        database_url = "postgresql://" + database_url.split("://", 1)[1]
    if database_url.startswith(("postgresql://", "postgres://")):
        from .store import RAGStore

        return RAGStore(database_url, ensure_schema=ensure_schema)
    elif database_url.startswith("sqlite://"):
        if not _sqlite_allowed:
            raise ValueError(
                "sqlite:// is not a runtime RAG store: Postgres is the runtime database (ADR-174). "
                "Use postgresql://… (the local stack provides one), or an http(s):// peer."
            )
        from .sqlite_store import SQLiteRAGStore

        return SQLiteRAGStore(database_url)
    elif database_url.startswith(("http://", "https://")):
        from .remote_store import RemoteRetrievalStore

        # Two credentials, for two kinds of caller. A peer node signs with the federation secret
        # (X-Node-ID + X-Signature, from the environment: it is a secret, so it never lives in the
        # store URL or committed settings; absent, the store's dev-mode defaults). A person's
        # laptop bound to a site node holds a gate key instead, and sends it as a bearer to that
        # node only, resolved exactly as a bound data read resolves it.
        kwargs: dict[str, str] = {}
        signature = os.environ.get("AXIOM_RAG_REMOTE_SIGNATURE", "")
        node_id = os.environ.get("AXIOM_RAG_REMOTE_NODE_ID", "")
        if signature:
            kwargs["signature"] = signature
        if node_id:
            kwargs["node_id"] = node_id
        bearer = _bound_key_for(database_url)
        if bearer:
            kwargs["bearer"] = bearer
        return RemoteRetrievalStore(database_url, **kwargs)
    else:
        raise ValueError(
            f"Unsupported store URL scheme: {database_url.split('://')[0]}://. "
            "Use postgresql:// or http(s):// (remote peer)."
        )
