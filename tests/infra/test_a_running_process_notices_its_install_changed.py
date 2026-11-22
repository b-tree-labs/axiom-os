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
