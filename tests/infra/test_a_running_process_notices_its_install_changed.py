# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A process refuses when its own install changes, and not when a sibling appears."""

from __future__ import annotations

import hashlib
import os

from axiom.infra.loaded_install import (
    files_patched,
    patched_message,
    refusal_against,
    remember,
    reset,
)


def setup_function() -> None:
    reset()


def teardown_function() -> None:
    # Nothing a test remembers may leak into the next test in this worker.
    reset()


def test_an_unchanged_install_is_served(tmp_path):
    marker = tmp_path / "axiom" / "__init__.py"
    marker.parent.mkdir()
    marker.write_text("x")
    remember("0.70.0", marker)
    assert refusal_against("0.70.0", marker) is None


def test_replacing_the_loaded_file_refuses(tmp_path):
    marker = tmp_path / "__init__.py"
    marker.write_text("old")
    remember("0.70.0", marker)
    marker.write_text("new")
    os.utime(marker, ns=(marker.stat().st_mtime_ns + 1_000_000, marker.stat().st_mtime_ns + 1_000_000))
    reason = refusal_against("0.70.0", marker)
    assert reason is not None
    assert "older Axiom" in reason
    assert "supervised switch" in reason


def test_a_sibling_tree_does_not_make_this_process_stale(tmp_path):
    """Blue/green: the other slot's files are not the ones this process loaded."""
    mine = tmp_path / "blue" / "__init__.py"
    mine.parent.mkdir()
    mine.write_text("blue")
    remember("0.70.0", mine)
    other = tmp_path / "green" / "__init__.py"
    other.parent.mkdir()
    other.write_text("green-newer")
    assert refusal_against("0.70.0", mine) is None


def test_a_version_change_of_this_install_refuses(tmp_path):
    marker = tmp_path / "__init__.py"
    marker.write_text("x")
    remember("0.70.0", marker)
    assert refusal_against("0.71.0", marker) is not None


def test_a_patched_wheel_record_is_named(tmp_path):
    body = tmp_path / "axiom" / "agents.py"
    body.parent.mkdir()
    body.write_text("original")
    digest = hashlib.sha256(b"original").hexdigest()
    record = f"axiom/agents.py,sha256={digest},8\n"
    assert files_patched(record, tmp_path) == []
    body.write_text("patched")
    bad = files_patched(record, tmp_path)
    assert bad == ["axiom/agents.py"]
    message = patched_message(bad)
    assert message is not None and "patched in place" in message


def test_a_call_never_captures_the_install_lazily(tmp_path, monkeypatch):
    """The first skill call used to capture whatever install existed at that moment. A test that
    had patched the install then made every later call in its worker "stale" (160 failures on
    main, 2026-11-22). Only a server that starts for real remembers what it loaded."""
    from axiom.infra import loaded_install

    fake = tmp_path / "__init__.py"
    fake.write_text("x")
    monkeypatch.setattr(loaded_install, "_installed_marker", lambda: ("9.9.9", fake))
    assert loaded_install.current_refusal() is None
    assert loaded_install._loaded is None  # nothing was remembered by the call
    monkeypatch.setattr(loaded_install, "_installed_marker", lambda: ("0.70.0", fake))
    assert loaded_install.current_refusal() is None


def test_a_started_server_remembers_what_it_loaded_and_then_judges(tmp_path, monkeypatch):
    from axiom.infra import loaded_install

    marker = tmp_path / "__init__.py"
    marker.write_text("x")
    monkeypatch.setattr(loaded_install, "_installed_marker", lambda: ("0.70.0", marker))
    loaded_install.capture()  # what the real server does once, at start
    assert loaded_install.current_refusal() is None
    monkeypatch.setattr(loaded_install, "_installed_marker", lambda: ("0.71.0", marker))
    assert loaded_install.current_refusal() is not None


def test_the_mcp_server_captures_the_install_when_it_starts():
    import inspect

    from axiom.extensions.builtins.mcp import server

    assert "loaded_install.capture()" in inspect.getsource(server.run)
