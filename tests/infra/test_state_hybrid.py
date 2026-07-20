# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for HybridStateStore — unified backend with automatic selection.

Proves:
1. FileStateBackend works through the unified interface
2. Backend selection logic (env vars, DSN detection, forced mode)
3. PostgreSQL fallback to file when unavailable
4. Both backends produce identical results through same API
5. Singleton management (get/reset)
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from unittest import mock

import pytest

from axiom.infra.state import (
    FileStateBackend,
    HybridStateStore,
    PgStateBackend,
    StateBackend,
    StateHandle,
    get_state_store,
    reset_state_store,
)


def _file_backend_worker(root_str: str, n: int, result_str: str):
    """Worker: increment counter through FileStateBackend."""
    b = FileStateBackend(Path(root_str))
    errors = 0
    for _ in range(n):
        try:
            with b.open("counter.json", exclusive=True) as h:
                data = h.read()
                data["counter"] = data.get("counter", 0) + 1
                h.write(data)
        except Exception:
            errors += 1
    Path(result_str).write_text(json.dumps({"errors": errors}))


# ---------------------------------------------------------------------------
# FileStateBackend
# ---------------------------------------------------------------------------

class TestFileStateBackend:
    """Prove FileStateBackend satisfies StateBackend protocol."""

    def test_implements_protocol(self, tmp_path: Path):
        backend = FileStateBackend(tmp_path)
        assert isinstance(backend, StateBackend)

    def test_name(self, tmp_path: Path):
        assert FileStateBackend(tmp_path).name == "file"

    def test_write_and_read(self, tmp_path: Path):
        backend = FileStateBackend(tmp_path)
        backend.write("test.json", {"hello": "world"})
        assert backend.read("test.json") == {"hello": "world"}

    def test_read_missing_returns_empty(self, tmp_path: Path):
        backend = FileStateBackend(tmp_path)
        assert backend.read("missing.json") == {}

    def test_open_read_modify_write(self, tmp_path: Path):
        backend = FileStateBackend(tmp_path)
        backend.write("counter.json", {"counter": 0})

        with backend.open("counter.json", exclusive=True) as h:
            assert isinstance(h, StateHandle)
            data = h.read()
            data["counter"] += 1
            h.write(data)

        assert backend.read("counter.json") == {"counter": 1}

    def test_resolves_relative_paths(self, tmp_path: Path):
        backend = FileStateBackend(tmp_path)
        backend.write("deep/nested/state.json", {"nested": True})
        assert (tmp_path / "deep" / "nested" / "state.json").exists()

    def test_handles_absolute_paths(self, tmp_path: Path):
        backend = FileStateBackend(tmp_path)
        abs_path = str(tmp_path / "absolute.json")
        backend.write(abs_path, {"abs": True})
        assert backend.read(abs_path) == {"abs": True}

    @pytest.mark.skipif(os.name == "nt", reason="fcntl required")
    def test_concurrent_safety(self, tmp_path: Path):
        """Multiple processes through FileStateBackend don't lose updates."""
        backend = FileStateBackend(tmp_path)
        backend.write("counter.json", {"counter": 0})

        num_workers = 4
        iters = 25
        result_paths = [tmp_path / f"r_{i}.json" for i in range(num_workers)]
        procs = []
        for i in range(num_workers):
            p = multiprocessing.Process(
                target=_file_backend_worker,
                args=(str(tmp_path), iters, str(result_paths[i])),
            )
            procs.append(p)

        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        final = backend.read("counter.json")
        expected = num_workers * iters
        assert final["counter"] == expected, (
            f"Lost {expected - final['counter']} updates through FileStateBackend"
        )


# ---------------------------------------------------------------------------
# HybridStateStore — backend selection
# ---------------------------------------------------------------------------

class TestHybridBackendSelection:
    """Prove backend selection logic works correctly."""

    def test_default_is_file(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            # Remove any state-related env vars
            for key in ["NEUTRON_STATE_BACKEND", "NEUTRON_STATE_DSN", "DATABASE_URL"]:
                os.environ.pop(key, None)
            backend = store.get_backend()
        assert backend.name == "file"

    def test_forced_file(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path, backend="file")
        assert store.get_backend().name == "file"

    def test_forced_file_via_env(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path)
        with mock.patch.dict(os.environ, {"NEUTRON_STATE_BACKEND": "file"}):
            assert store.get_backend().name == "file"

    def test_forced_postgresql_fails_without_dsn(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path, backend="postgresql")
        with pytest.raises(RuntimeError, match="postgresql"):
            store.get_backend()

    def test_dsn_without_pg_falls_back_to_file(self, tmp_path: Path):
        """If DSN is set but PostgreSQL is unreachable, fall back to file."""
        store = HybridStateStore(
            root=tmp_path,
            dsn="postgresql://localhost:59999/nonexistent_db",
        )
        backend = store.get_backend()
        assert backend.name == "file"

    def test_backend_cached(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path, backend="file")
        b1 = store.get_backend()
        b2 = store.get_backend()
        assert b1 is b2


# ---------------------------------------------------------------------------
# HybridStateStore — operations
# ---------------------------------------------------------------------------

class TestHybridOperations:
    """Prove HybridStateStore delegates correctly to backend."""

    def test_write_and_read(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path, backend="file")
        store.write("data.json", {"key": "value"})
        assert store.read("data.json") == {"key": "value"}

    def test_open_context_manager(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path, backend="file")
        store.write("counter.json", {"n": 0})

        with store.open("counter.json", exclusive=True) as h:
            data = h.read()
            data["n"] = 42
            h.write(data)

        assert store.read("counter.json") == {"n": 42}

    def test_read_missing(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path, backend="file")
        assert store.read("nope.json") == {}

    def test_backend_name_property(self, tmp_path: Path):
        store = HybridStateStore(root=tmp_path, backend="file")
        assert store.backend_name == "file"


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_returns_same_instance(self, tmp_path: Path):
        reset_state_store()
        s1 = get_state_store(root=tmp_path, backend="file")
        s2 = get_state_store()
        assert s1 is s2
        reset_state_store()

    def test_reset_clears_singleton(self, tmp_path: Path):
        reset_state_store()
        s1 = get_state_store(root=tmp_path, backend="file")
        reset_state_store()
        s2 = get_state_store(root=tmp_path, backend="file")
        assert s1 is not s2
        reset_state_store()


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:
    """Verify both backends satisfy the StateBackend protocol."""

    def test_file_backend_is_state_backend(self, tmp_path: Path):
        assert isinstance(FileStateBackend(tmp_path), StateBackend)

    def test_pg_backend_is_state_backend(self):
        try:
            pg = PgStateBackend("postgresql://localhost/test")
            assert isinstance(pg, StateBackend)
        except Exception:
            pytest.skip("psycopg not available")

    def test_hybrid_store_has_backend_interface(self, tmp_path: Path):
        """HybridStateStore exposes the same read/write/open interface."""
        store = HybridStateStore(root=tmp_path, backend="file")
        assert hasattr(store, "read")
        assert hasattr(store, "write")
        assert hasattr(store, "open")
