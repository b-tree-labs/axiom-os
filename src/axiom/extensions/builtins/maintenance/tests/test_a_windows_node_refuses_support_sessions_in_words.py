# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Level 2 is for Linux and macOS nodes; a Windows node is told so, in words.

A session is a POSIX pseudo-terminal carried both ways, so it cannot run on a
Windows collector. What such a node must still do: refuse ``support open``
with a sentence rather than a traceback, and keep reporting its maintenance
state (status, heartbeat) without needing the POSIX file lock. Level 1 needs
none of this and runs wherever the node runs. These tests simulate the
platform, since CI has no Windows runner: ``os.name`` and the ``fcntl`` module.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from axiom.infra import maintenance as m
from axiom.infra import support_session as ss


def test_support_open_refuses_before_touching_a_relay_or_a_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ss.SessionsUnsupported) as exc:
        ss.run_session(None, duration_s=600, reason="", shell=["sh"], state_dir=tmp_path)  # type: ignore[arg-type]
    assert "Windows" in str(exc.value) and "level 1" in str(exc.value)
    assert not (tmp_path / "support-session.json").exists()
    with pytest.raises(ss.SessionsUnsupported):
        ss.attach(None, "any")  # type: ignore[arg-type]


def test_a_node_without_a_posix_file_lock_still_reports_its_state(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "fcntl", None)  # ``import fcntl`` now raises ImportError
    assert ss.active_session(tmp_path) is None
    (tmp_path / "support-session.json").write_text(json.dumps({"id": "s1", "pid": 1, "expires_at": 0}))
    assert ss.active_session(tmp_path) == {"id": "s1", "pid": 1, "expires_at": 0, "alive": False}
    node = m.MaintenanceNode(site="site-a", level="sessions", wiring={}, state_dir=tmp_path)
    assert node.heartbeat_section()["session"] == {"id": "s1", "expires_at": 0, "alive": False}
    # And the file, held by nobody, is cleared rather than taken for a session.
    assert ss.close_active(None, Path(tmp_path))["alive"] is False
    assert not (tmp_path / "support-session.json").exists()


def test_the_heartbeat_wrapper_keeps_its_promise_never_to_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("AXIOM_NODE_CONFIG", str(tmp_path / "node.toml"))
    monkeypatch.setenv("AXIOM_MAINTENANCE_WIRING", str(tmp_path / "maintenance.toml"))
    (tmp_path / "maintenance.toml").write_text('site = "site-a"\n')
    monkeypatch.setattr(m.MaintenanceNode, "heartbeat_section", lambda self: 1 / 0)
    assert m.heartbeat_section() is None
