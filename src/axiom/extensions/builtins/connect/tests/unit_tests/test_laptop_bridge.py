# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""A3 — laptop presence + read-only pull, with the forward-compat policy seam."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.connect.laptop_bridge import (
    LaptopBridge,
    LaptopOffline,
    NotDeclared,
)
from axiom.infra.host_exec import ExecResult
from axiom.vega.federation.discovery import KnownNode, NodeRegistry, NodeState


class _FakeExec:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, timeout=60.0):
        self.calls.append(argv)
        return ExecResult(rc=0, stdout=f"out:{' '.join(argv)}", stderr="")


@pytest.fixture
def registry(tmp_path):
    return NodeRegistry(registry_path=tmp_path / "reg.json")


def _bridge(registry, state, ex=None, **kw):
    registry.add(KnownNode(node_id="bens-laptop", display_name="Ben's laptop",
                           url="ssh://bens-mbp", state=state, ssh_host="bens-mbp"))
    return LaptopBridge(
        node_id="bens-laptop", registry=registry, executor=ex or _FakeExec(),
        declared_files=frozenset({"/Users/example/status.md"}),
        declared_signals={"battery": ["pmset", "-g", "batt"]},
        **kw,
    )


def test_offline_when_not_reachable(registry):
    b = _bridge(registry, NodeState.UNKNOWN)
    assert b.is_online() is False
    with pytest.raises(LaptopOffline):
        b.read_declared_file("/Users/example/status.md")


def test_online_reads_declared_file(registry):
    ex = _FakeExec()
    b = _bridge(registry, NodeState.VERIFIED, ex=ex)
    assert b.is_online() is True
    out = b.read_declared_file("/Users/example/status.md")
    assert out.startswith("out:cat") and ex.calls == [["cat", "/Users/example/status.md"]]


def test_undeclared_file_refused(registry):
    b = _bridge(registry, NodeState.VERIFIED)
    with pytest.raises(NotDeclared):
        b.read_declared_file("/etc/passwd")


def test_declared_signal_runs_its_argv(registry):
    ex = _FakeExec()
    b = _bridge(registry, NodeState.VERIFIED, ex=ex)
    assert "pmset" in b.read_declared_signal("battery")
    assert ex.calls == [["pmset", "-g", "batt"]]


def test_undeclared_signal_refused(registry):
    b = _bridge(registry, NodeState.VERIFIED)
    with pytest.raises(NotDeclared):
        b.read_declared_signal("keychain")


def test_policy_gate_can_deny(registry):
    b = _bridge(registry, NodeState.VERIFIED, policy_gate=lambda write: False)
    with pytest.raises(PermissionError):
        b.read_declared_file("/Users/example/status.md")


def test_last_seen_tracks_state_update(registry):
    b = _bridge(registry, NodeState.UNKNOWN)
    registry.update_state("bens-laptop", NodeState.VERIFIED)
    assert b.is_online() and b.last_seen()  # stamped by update_state
