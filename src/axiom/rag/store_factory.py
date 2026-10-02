# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Factory for creating the appropriate RAG store backend."""

from __future__ import annotations


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
      - ``sqlite:///``                      — local SQLite store
      - ``http://`` / ``https://``          — remote peer's retrieval endpoint
        (a node whose corpus lives on a peer; see ``remote_store``)
    """
    if database_url.startswith(("postgresql://", "postgres://")):
        from .store import RAGStore

        return RAGStore(database_url, ensure_schema=ensure_schema)
    elif database_url.startswith("sqlite://"):
        from .sqlite_store import SQLiteRAGStore

        return SQLiteRAGStore(database_url)
    elif database_url.startswith(("http://", "https://")):
        import os

        from .remote_store import RemoteRetrievalStore

        # A remote peer that enforces auth (not the dev-mode "accept any
        # non-empty pair") needs a real credential sent as X-Signature. Thread
        # it from the environment — it is a secret, so it never lives in the
        # store URL or committed settings. Absent → the store's dev-mode
        # defaults, unchanged. The Ed25519/NodeRegistry path (remote_store
        # docstring) supersedes this shared-secret seam later.
        kwargs: dict[str, str] = {}
        signature = os.environ.get("AXIOM_RAG_REMOTE_SIGNATURE", "")
        node_id = os.environ.get("AXIOM_RAG_REMOTE_NODE_ID", "")
        if signature:
            kwargs["signature"] = signature
        if node_id:
            kwargs["node_id"] = node_id
        return RemoteRetrievalStore(database_url, **kwargs)
    else:
        raise ValueError(
            f"Unsupported store URL scheme: {database_url.split('://')[0]}://. "
            "Use postgresql://, sqlite:///, or http(s):// (remote peer)."
        )
