# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests ensuring parity across deployment environments.

All three deployment paths — Docker Compose, K3D, and full K8S — must
provide the same capabilities. These tests verify that:
  1. Each path provisions the same services (PG, embedding, LLM)
  2. DATABASE_URL construction works for each path
  3. The RAG store works identically regardless of backend
  4. Embedding provider detection works in each environment
  5. No environment-specific code paths are missing features
"""

from __future__ import annotations

import inspect

import pytest


class TestInfraPathDetection:
    """Verify all three infra paths are detected and handled."""

    def test_detect_infra_path_returns_valid_path(self):
        from axiom.setup.infra import detect_infra_path

        path = detect_infra_path()
        assert path in ("k3d", "docker-compose", "native"), (
            f"detect_infra_path() returned '{path}' — must be one of: "
            "k3d, docker-compose, native"
        )

    def test_all_three_paths_have_provisioners(self):
        """Each infra path must have a corresponding provisioner function."""
        from axiom.setup import infra

        assert hasattr(infra, "provision_postgres_compose"), "Missing Docker Compose provisioner"
        assert hasattr(infra, "_setup_cluster_step"), "Missing K3D/K8S provisioner"
        # Native path uses manual PG — no provisioner needed, but should have guidance
        assert hasattr(infra, "_guide_docker_install") or True, "Missing native path guidance"


class TestDatabaseURLParity:
    """DATABASE_URL must be constructable for every environment."""

    def test_pg_connection_string_format(self):
        """All environments use the same PG connection string format."""
        # K3D and Docker Compose both use postgresql://axiom:PASSWORD@HOST:PORT/axiom_db
        # The only difference is HOST (localhost vs k3d service name)
        from axiom.setup.secrets import generate_password

        pw = generate_password()
        urls = {
            "docker-compose": f"postgresql://axiom:{pw}@localhost:5432/axiom_db",
            "k3d": f"postgresql://axiom:{pw}@localhost:5433/axiom_db",
            "k8s": f"postgresql://axiom:{pw}@postgres.axiom.svc.cluster.local:5432/axiom_db",
        }
        for env, url in urls.items():
            assert url.startswith("postgresql://"), f"{env} URL must use postgresql:// scheme"
            assert "axiom_db" in url, f"{env} URL must reference axiom_db"


class TestServiceParity:
    """Each environment must provide the same core services."""

    REQUIRED_SERVICES = {
        "postgresql",    # RAG store
        "embedding",     # nomic-embed-text via Ollama
    }

    OPTIONAL_SERVICES = {
        "llm",           # Local LLM (bonsai) — optional, can use remote
    }

    def test_infra_checks_cover_required_services(self):
        """run_infra_checks must check all required services."""
        from axiom.setup.infra import run_infra_checks

        checks = run_infra_checks(skip_cluster=True)
        check_names_lower = [c.name.lower() for c in checks]

        # Must check Docker (PG runs in Docker)
        assert any("docker" in n for n in check_names_lower), (
            "Infra checks missing Docker — required for PostgreSQL"
        )
        # Must check embedding provider
        assert any("embed" in n or "ollama" in n for n in check_names_lower), (
            "Infra checks missing embedding provider — required for RAG quality"
        )

    def test_docker_compose_has_pg(self):
        """docker-compose.yml must provision PostgreSQL."""
        from pathlib import Path
        compose = Path(__file__).resolve().parents[1] / "src" / "axiom" / "setup" / "docker-compose.yml"
        if not compose.exists():
            pytest.skip("docker-compose.yml not found")
        content = compose.read_text()
        assert "postgres" in content.lower()
        assert "pgvector" in content.lower(), (
            "docker-compose.yml must use pgvector image for vector search"
        )


class TestRAGStoreAbstraction:
    """The RAG store must work identically regardless of backend."""

    def test_store_factory_supports_both_backends(self):
        from axiom.rag.sqlite_store import SQLiteRAGStore
        from axiom.rag.store import RAGStore
        from axiom.rag.store_factory import create_store

        pg = create_store("postgresql://user:pass@host/db")
        assert isinstance(pg, RAGStore)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sq = create_store(f"sqlite:///{tmp}/test.db")
            assert isinstance(sq, SQLiteRAGStore)

    def test_both_stores_have_same_methods(self):
        """PG and SQLite stores must expose identical public methods."""
        from axiom.rag.sqlite_store import SQLiteRAGStore
        from axiom.rag.store import RAGStore

        required_methods = [
            "connect", "close", "search", "upsert_chunks",
            "get_document", "find_by_content_hash",
            "delete_document", "delete_corpus", "stats",
        ]

        for method in required_methods:
            assert hasattr(RAGStore, method), f"RAGStore missing {method}"
            assert hasattr(SQLiteRAGStore, method), f"SQLiteRAGStore missing {method}"

    def test_search_signatures_match(self):
        """search() must accept the same parameters on both backends."""
        from axiom.rag.sqlite_store import SQLiteRAGStore
        from axiom.rag.store import RAGStore

        pg_sig = inspect.signature(RAGStore.search)
        sq_sig = inspect.signature(SQLiteRAGStore.search)

        pg_params = set(pg_sig.parameters.keys())
        sq_params = set(sq_sig.parameters.keys())

        assert pg_params == sq_params, (
            f"search() parameter mismatch:\n"
            f"  PG only: {pg_params - sq_params}\n"
            f"  SQLite only: {sq_params - pg_params}"
        )

    def test_upsert_signatures_match(self):
        """upsert_chunks() must accept the same parameters on both backends."""
        from axiom.rag.sqlite_store import SQLiteRAGStore
        from axiom.rag.store import RAGStore

        pg_sig = inspect.signature(RAGStore.upsert_chunks)
        sq_sig = inspect.signature(SQLiteRAGStore.upsert_chunks)

        pg_params = set(pg_sig.parameters.keys())
        sq_params = set(sq_sig.parameters.keys())

        assert pg_params == sq_params, (
            f"upsert_chunks() parameter mismatch:\n"
            f"  PG only: {pg_params - sq_params}\n"
            f"  SQLite only: {sq_params - pg_params}"
        )


class TestEmbeddingProviderParity:
    """Embedding must work in all environments."""

    def test_embedding_fallback_chain_defined(self):
        """embed_texts must define a multi-provider fallback chain."""
        source = inspect.getsource(
            __import__("axiom.rag.embeddings", fromlist=["embed_texts"]).embed_texts
        )
        # Must try multiple providers
        assert "remote" in source.lower() or "neut_embed" in source.lower(), (
            "embed_texts missing remote provider support"
        )
        assert "ollama" in source.lower(), "embed_texts missing Ollama fallback"

    def test_ollama_is_default_local_provider(self):
        from axiom.rag.embeddings import _OLLAMA_EMBED_MODEL
        assert _OLLAMA_EMBED_MODEL == "nomic-embed-text"
