# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An ingest finds its document store the way the rest of the platform does.

``data.refresh`` read its watermark through the platform resolver, then
``run_ingest`` built the store from the connector's own variable only, so a
process that resolved its database the standard way failed every connector at
the store with "env 'DP1_RAG_DSN' is unset".
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import ConnectorConfig
from axiom.extensions.builtins.data_platform.agents.plinth.skills import run_ingest as ri


def _config() -> ConnectorConfig:
    return ConnectorConfig(name="docs", kind="box", bronze_root="/tmp/x", rag_dsn_env="DP1_RAG_DSN")


def test_the_platform_database_is_used_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv("DP1_RAG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@db.example/platform")
    built = {}
    monkeypatch.setattr("axiom.rag.store.RAGStore", lambda dsn, **kw: built.setdefault("dsn", dsn))

    ri._build_store(_config())
    assert built["dsn"].endswith("@db.example/platform")


def test_the_connectors_own_variable_still_wins(monkeypatch):
    monkeypatch.setenv("DP1_RAG_DSN", "postgresql://u@rag.example/docs")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@db.example/platform")
    built = {}
    monkeypatch.setattr("axiom.rag.store.RAGStore", lambda dsn, **kw: built.setdefault("dsn", dsn))

    ri._build_store(_config())
    assert built["dsn"].endswith("@rag.example/docs")
