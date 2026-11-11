# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for peer version preflight on `axi nodes add`.

Before binding identity, we must check the peer is running a version of
axi that supports `axi federation status --json`. Running against an old
peer gives a cryptic "unknown subcommand" error; we want a guided message.
"""

from __future__ import annotations

import base64
import json

from axiom.vega.federation.discovery import (
    MIN_PEER_VERSION_FOR_IDENTITY_BINDING,
    NodeRegistry,
)


def _registry(tmp_path):
    return NodeRegistry(registry_path=tmp_path / "nodes.yaml")


def _runner_sequence(version_response, status_response=None):
    """Build an ssh_runner that responds to `axi --version` then
    (optionally) `axi federation status --json` from fixed payloads.

    Each entry in the responses is (rc, stdout, stderr).
    """
    calls: list[str] = []

    def _runner(user, host, cmd):
        calls.append(cmd)
        if cmd == "axi --version":
            return version_response
        if cmd == "axi federation status --json":
            return status_response or (0, "", "")
        return 127, "", "unexpected cmd"

    _runner.calls = calls  # type: ignore[attr-defined]
    return _runner


class TestCheckPeerVersion:
    def test_new_enough_version_ok(self, tmp_path):
        reg = _registry(tmp_path)
        runner = _runner_sequence((0, "axi 0.10.5\n", ""))
        ok, version, msg = reg.check_peer_version("user", "host", ssh_runner=runner)
        assert ok is True
        assert version == "0.10.5"

    def test_exact_minimum_version_ok(self, tmp_path):
        reg = _registry(tmp_path)
        runner = _runner_sequence(
            (0, f"axi {MIN_PEER_VERSION_FOR_IDENTITY_BINDING}\n", ""),
        )
        ok, version, msg = reg.check_peer_version("user", "host", ssh_runner=runner)
        assert ok is True
        assert version == MIN_PEER_VERSION_FOR_IDENTITY_BINDING

    def test_old_version_rejected_with_guided_message(self, tmp_path):
        reg = _registry(tmp_path)
        runner = _runner_sequence((0, "axi 0.10.2\n", ""))
        ok, version, msg = reg.check_peer_version("bb", "example-host", ssh_runner=runner)
        assert ok is False
        assert version == "0.10.2"
        # Guided message mentions version, minimum, and the `axi update` fix
        assert "0.10.2" in msg
        assert MIN_PEER_VERSION_FOR_IDENTITY_BINDING in msg
        assert "axi update" in msg
        assert "bb@example-host" in msg

    def test_peer_unreachable_distinct_message(self, tmp_path):
        reg = _registry(tmp_path)
        runner = _runner_sequence((255, "", "ssh: connect: no route to host"))
        ok, version, msg = reg.check_peer_version("u", "h", ssh_runner=runner)
        assert ok is False
        assert version == ""
        assert "unreachable" in msg.lower() or "ssh" in msg.lower()
        # Must not falsely claim version skew
        assert MIN_PEER_VERSION_FOR_IDENTITY_BINDING not in msg

    def test_unparseable_version_distinct_message(self, tmp_path):
        reg = _registry(tmp_path)
        runner = _runner_sequence((0, "something weird\n", ""))
        ok, version, msg = reg.check_peer_version("u", "h", ssh_runner=runner)
        assert ok is False
        assert "parse" in msg.lower() or "unparse" in msg.lower()


class TestFetchIdentityPreflight:
    def test_fetch_identity_short_circuits_on_old_peer(self, tmp_path):
        reg = _registry(tmp_path)
        node = reg.discover_ssh("example-host", "bb", "example-host")
        runner = _runner_sequence((0, "axi 0.10.2\n", ""))
        ok, msg = reg.fetch_identity_ssh(node.node_id, ssh_runner=runner)
        assert ok is False
        assert "0.10.2" in msg
        assert "axi update" in msg
        # Verify we did NOT attempt the federation status call
        assert "axi federation status --json" not in runner.calls  # type: ignore[attr-defined]

    def test_fetch_identity_proceeds_on_new_peer(self, tmp_path):
        reg = _registry(tmp_path)
        node = reg.discover_ssh("example-host", "bb", "example-host")
        pubkey = base64.b64encode(b"\x01" * 32).decode()
        status = {
            "initialized": True,
            "node_id": "peerabcd1234",
            "public_key": pubkey,
            "owner": "bb@example.com",
            "display_name": "example-host",
            "profile": "standard",
        }
        runner = _runner_sequence(
            (0, "axi 0.10.5\n", ""),
            status_response=(0, json.dumps(status), ""),
        )
        ok, msg = reg.fetch_identity_ssh(node.node_id, ssh_runner=runner)
        assert ok is True, msg
        assert "axi --version" in runner.calls  # type: ignore[attr-defined]
        assert "axi federation status --json" in runner.calls  # type: ignore[attr-defined]
