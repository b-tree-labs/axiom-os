# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for TIDY Layer 1: MoManager, Manifest, paths."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.hygiene.manager import MoManager
from axiom.extensions.builtins.hygiene.manifest import Manifest, ScratchEntry
from axiom.extensions.builtins.hygiene.paths import resolve_base_dir

# ---------------------------------------------------------------------------
# paths.resolve_base_dir
# ---------------------------------------------------------------------------

class TestResolveBaseDir:
    def test_env_override(self, tmp_path):
        with patch.dict(os.environ, {"NEUT_SCRATCH_DIR": str(tmp_path / "custom")}):
            assert resolve_base_dir() == tmp_path / "custom"

    def test_macos_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEUT_SCRATCH_DIR", None)
            with patch("axiom.extensions.builtins.hygiene.paths.sys") as mock_sys:
                mock_sys.platform = "darwin"
                result = resolve_base_dir()
                assert "Library/Caches/axi/tidy" in str(result)

    def test_linux_xdg(self, tmp_path):
        with patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(tmp_path / "xdg"),
        }):
            os.environ.pop("NEUT_SCRATCH_DIR", None)
            with patch("axiom.extensions.builtins.hygiene.paths.sys") as mock_sys:
                mock_sys.platform = "linux"
                result = resolve_base_dir()
                assert "axi/tidy" in str(result)


# ---------------------------------------------------------------------------
# ScratchEntry
# ---------------------------------------------------------------------------

class TestScratchEntry:
    def test_defaults(self):
        e = ScratchEntry(path="/tmp/test", owner="test")
        assert e.id  # auto-generated
        assert e.pid == os.getpid()
        assert e.created_at  # auto-generated
        assert e.retention == "session"
        assert e.size_bytes == 0

    def test_roundtrip(self):
        e = ScratchEntry(path="/tmp/test", owner="test", purpose="unit test")
        d = e.to_dict()
        e2 = ScratchEntry.from_dict(d)
        assert e2.path == e.path
        assert e2.owner == e.owner
        assert e2.id == e.id


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    def test_add_and_retrieve(self, tmp_path):
        m = Manifest(tmp_path / "manifest.json")
        entry = ScratchEntry(path="/tmp/a", owner="test")
        m.add(entry)
        assert entry.id in m.entries
        assert (tmp_path / "manifest.json").exists()

    def test_remove(self, tmp_path):
        m = Manifest(tmp_path / "manifest.json")
        entry = ScratchEntry(path="/tmp/a", owner="test")
        m.add(entry)
        removed = m.remove(entry.id)
        assert removed is not None
        assert entry.id not in m.entries

    def test_remove_by_path(self, tmp_path):
        m = Manifest(tmp_path / "manifest.json")
        entry = ScratchEntry(path="/tmp/specific", owner="test")
        m.add(entry)
        removed = m.remove_by_path("/tmp/specific")
        assert removed is not None
        assert len(m.entries) == 0

    def test_persistence(self, tmp_path):
        path = tmp_path / "manifest.json"
        m1 = Manifest(path)
        entry = ScratchEntry(path="/tmp/persist", owner="test")
        m1.add(entry)

        # Load from disk
        m2 = Manifest(path)
        assert entry.id in m2.entries

    def test_filter_by_owner(self, tmp_path):
        m = Manifest(tmp_path / "manifest.json")
        m.add(ScratchEntry(path="/tmp/a", owner="alpha"))
        m.add(ScratchEntry(path="/tmp/b", owner="beta"))
        m.add(ScratchEntry(path="/tmp/c", owner="alpha"))
        assert len(m.get_by_owner("alpha")) == 2
        assert len(m.get_by_owner("beta")) == 1

    def test_filter_by_retention(self, tmp_path):
        m = Manifest(tmp_path / "manifest.json")
        m.add(ScratchEntry(path="/tmp/a", owner="x", retention="session"))
        m.add(ScratchEntry(path="/tmp/b", owner="x", retention="transient"))
        assert len(m.get_by_retention("session")) == 1
        assert len(m.get_by_retention("transient")) == 1


# ---------------------------------------------------------------------------
# MoManager
# ---------------------------------------------------------------------------

class TestMoManager:
    def test_init_creates_base_dir(self, tmp_path):
        base = tmp_path / "tidy"
        mgr = MoManager(base_dir=base)
        assert mgr.writable
        assert base.is_dir()

    @pytest.mark.skipif(os.getuid() == 0, reason="root can write to any path; test assumes non-root")
    def test_init_unwritable(self):
        mgr = MoManager(base_dir=Path("/nonexistent/impossible/path"))
        assert not mgr.writable

    def test_acquire_file(self, tmp_path):
        mgr = MoManager(base_dir=tmp_path / "tidy")
        path = mgr.acquire_file("test.owner", suffix=".txt", purpose="unit test")
        assert path is not None
        assert path.exists()
        assert path.suffix == ".txt"
        assert "test/owner" in str(path)

    def test_acquire_dir(self, tmp_path):
        mgr = MoManager(base_dir=tmp_path / "tidy")
        path = mgr.acquire_dir("test.dirs", purpose="unit test")
        assert path is not None
        assert path.is_dir()

    @pytest.mark.skipif(os.getuid() == 0, reason="root can write to any path; test assumes non-root")
    def test_acquire_returns_none_when_unwritable(self):
        mgr = MoManager(base_dir=Path("/nonexistent/impossible"))
        assert mgr.acquire_file("test") is None
        assert mgr.acquire_dir("test") is None

    def test_release(self, tmp_path):
        mgr = MoManager(base_dir=tmp_path / "tidy")
        path = mgr.acquire_file("test")
        assert path is not None
        assert path.exists()

        released = mgr.release(path)
        assert released
        assert not path.exists()

    def test_release_dir(self, tmp_path):
        mgr = MoManager(base_dir=tmp_path / "tidy")
        path = mgr.acquire_dir("test")
        assert path is not None
        # Put a file inside
        (path / "data.txt").write_text("hello")
        assert path.is_dir()

        released = mgr.release(path)
        assert released
        assert not path.exists()

    def test_status(self, tmp_path):
        mgr = MoManager(base_dir=tmp_path / "tidy")
        mgr.acquire_file("a")
        mgr.acquire_dir("b")
        info = mgr.status()
        assert info["writable"]
        assert info["active_entries"] == 2
        assert "a" in info["entries_by_owner"]
        assert "b" in info["entries_by_owner"]
        assert info["disk_free_bytes"] > 0

    def test_all_entries(self, tmp_path):
        mgr = MoManager(base_dir=tmp_path / "tidy")
        mgr.acquire_file("x")
        mgr.acquire_file("y")
        entries = mgr.all_entries()
        assert len(entries) == 2
        owners = {e.owner for e in entries}
        assert owners == {"x", "y"}

    def test_sweep_cleans_expired(self, tmp_path):
        mgr = MoManager(base_dir=tmp_path / "tidy")
        path = mgr.acquire_file("test", retention="hour")
        assert path is not None

        # Manually backdate the entry
        for e in mgr.all_entries():
            if e.path == str(path):
                e.created_at = "2020-01-01T00:00:00+00:00"
                assert mgr._manifest is not None
                mgr._manifest._entries[e.id] = e
                mgr._manifest._save()

        result = mgr.sweep()
        assert result["expired"] >= 1
        assert not path.exists()

    def test_sweep_cleans_orphaned_paths(self, tmp_path):
        base = tmp_path / "tidy"
        mgr = MoManager(base_dir=base)

        # Create an untracked file in the base dir
        orphan_dir = base / "stale_owner"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "orphan.txt").write_text("stale")

        result = mgr.sweep()
        assert result["orphaned"] >= 1

    def test_purge(self, tmp_path):
        mgr = MoManager(base_dir=tmp_path / "tidy")
        mgr.acquire_file("a")
        mgr.acquire_dir("b")
        assert len(mgr.all_entries()) == 2

        result = mgr.purge()
        assert result["deleted"] >= 2
        assert len(mgr.all_entries()) == 0

    def test_bus_events(self, tmp_path):
        events = []

        class FakeBus:
            def publish(self, topic, data=None, source=""):
                events.append((topic, data))

        bus = FakeBus()
        mgr = MoManager(base_dir=tmp_path / "tidy", bus=bus)
        path = mgr.acquire_file("test")
        assert any(t == "tidy.acquired" for t, _ in events)

        events.clear()
        assert path is not None
        mgr.release(path)
        assert any(t == "tidy.released" for t, _ in events)


# ---------------------------------------------------------------------------
# Public API (tools.tidy)
# ---------------------------------------------------------------------------

class TestPublicAPI:
    def test_acquire_and_manager(self, tmp_path):
        import axiom.extensions.builtins.hygiene as tidy
        # Reset singleton
        tidy._instance = None

        with patch("axiom.extensions.builtins.hygiene.MoManager") as MockMgr:
            instance = MockMgr.return_value
            instance.acquire_file.return_value = tmp_path / "test.txt"
            instance.acquire_dir.return_value = tmp_path / "testdir"

            result = tidy.acquire("test", suffix=".txt")
            assert result == tmp_path / "test.txt"

            result = tidy.acquire_dir("test")
            assert result == tmp_path / "testdir"

        # Reset singleton for other tests
        tidy._instance = None

    def test_scratch_file_context_manager(self, tmp_path):
        import axiom.extensions.builtins.hygiene as tidy
        tidy._instance = None

        with patch("axiom.extensions.builtins.hygiene.MoManager") as MockMgr:
            instance = MockMgr.return_value
            test_path = tmp_path / "ctx.txt"
            instance.acquire_file.return_value = test_path
            instance.release.return_value = True

            with tidy.scratch_file("test") as p:
                assert p == test_path

            instance.release.assert_called_once_with(test_path)

        tidy._instance = None

    def test_scratch_dir_context_manager(self, tmp_path):
        import axiom.extensions.builtins.hygiene as tidy
        tidy._instance = None

        with patch("axiom.extensions.builtins.hygiene.MoManager") as MockMgr:
            instance = MockMgr.return_value
            test_path = tmp_path / "ctxdir"
            instance.acquire_dir.return_value = test_path
            instance.release.return_value = True

            with tidy.scratch_dir("test") as p:
                assert p == test_path

            instance.release.assert_called_once_with(test_path)

        tidy._instance = None
