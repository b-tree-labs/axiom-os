# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``axi federation`` and ``axi nodes`` CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def identity_dir(tmp_path: Path):
    """Provide a temporary identity directory and patch load/generate to use it."""
    keys_dir = tmp_path / "identity"
    keys_dir.mkdir()
    return keys_dir


@pytest.fixture()
def registry_path(tmp_path: Path):
    """Provide a temporary nodes.yaml path."""
    return tmp_path / "nodes.yaml"


@pytest.fixture()
def _patch_registry(registry_path):
    """Patch NodeRegistry to use tmp_path."""
    from axiom.vega.federation.discovery import NodeRegistry

    original_init = NodeRegistry.__init__

    def patched_init(self, registry_path_arg=None):
        original_init(self, registry_path)

    with patch.object(NodeRegistry, "__init__", patched_init):
        yield


@pytest.fixture()
def _patch_identity(identity_dir):
    """Patch identity functions to use tmp_path."""
    with (
        patch(
            "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
            identity_dir,
        ),
        patch(
            "axiom.vega.federation.identity.load_identity",
            wraps=lambda keys_dir=None: _load_from(identity_dir),
        ),
    ):
        yield


def _load_from(keys_dir):
    from axiom.vega.federation.identity import load_identity as _real_load

    return _real_load(keys_dir)


# ---------------------------------------------------------------------------
# Federation CLI tests
# ---------------------------------------------------------------------------


class TestFederationCLI:
    """Tests for ``axi federation`` subcommands."""

    def test_status_no_identity(self, capsys, identity_dir):
        """Status without identity shows init prompt."""
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            rc = main(["status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Not initialized" in out or "init" in out.lower()

    def test_status_no_identity_json(self, capsys, identity_dir):
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            rc = main(["status", "--json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["initialized"] is False

    def test_init_creates_identity(self, capsys, identity_dir):
        """Init generates keypair and identity.json."""
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            rc = main(["init", "--owner", "test@example.com", "--name", "TestNode"])

        assert rc == 0
        assert (identity_dir / "identity.json").exists()
        assert (identity_dir / "private.pem").exists()
        assert (identity_dir / "public.b64").exists()

    def test_init_creates_identity_json(self, capsys, identity_dir):
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            rc = main(["init", "--owner", "test@example.com", "--json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["initialized"] is True
        assert data["node_id"]

    def test_init_rejects_duplicate(self, capsys, identity_dir):
        """Init fails if identity already exists."""
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            main(["init", "--owner", "test@example.com"])
            rc = main(["init", "--owner", "test@example.com"])

        assert rc == 1

    def test_status_with_identity(self, capsys, identity_dir, _patch_registry):
        """Status shows identity info after init."""
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            main(["init", "--owner", "test@example.com", "--name", "TestNode"])
            rc = main(["status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Node ID" in out

    def test_invite_generates_token(self, capsys, identity_dir, _patch_registry):
        """Invite generates a token with TTL."""
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            main(["init", "--owner", "test@example.com"])
            capsys.readouterr()  # drain init output
            rc = main(["invite", "--json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "token" in data
        assert data["ttl_hours"] == 24
        assert data["expires"]

    def test_invite_custom_ttl(self, capsys, identity_dir, _patch_registry):
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            main(["init", "--owner", "test@example.com"])
            capsys.readouterr()  # drain init output
            rc = main(["invite", "--ttl", "48", "--json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ttl_hours"] == 48

    def test_peers_empty(self, capsys, identity_dir, _patch_registry):
        """Peers shows empty list initially."""
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            main(["init", "--owner", "test@example.com"])
            rc = main(["peers"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "No federated peers" in out

    def test_peers_empty_json(self, capsys, identity_dir, _patch_registry):
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            main(["init", "--owner", "test@example.com"])
            capsys.readouterr()  # drain init output
            rc = main(["peers", "--json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data == []

    def test_resources_json(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.cli import main

        rc = main(["resources", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "resources" in data

    def test_join_no_identity(self, capsys, identity_dir):
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            rc = main(["join", "https://example.com/invite/abc"])
        assert rc == 1

    def test_leave_no_identity(self, capsys, identity_dir):
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.cli import main

            rc = main(["leave"])
        assert rc == 1

    def test_parser_has_all_subcommands(self):
        from axiom.extensions.builtins.federation.cli import build_parser

        parser = build_parser()
        # Access subparsers
        actions = parser._subparsers._group_actions
        choices = set()
        for action in actions:
            choices.update(action.choices.keys())
        expected = {"status", "init", "join", "leave", "invite", "resources", "peers"}
        assert expected.issubset(choices)


# ---------------------------------------------------------------------------
# Nodes CLI tests
# ---------------------------------------------------------------------------


def _mock_ssh_identity_binding(registry_self, node_id, on_key_change="refuse"):
    """Simulate a successful TOFU identity fetch.

    Real ``fetch_identity_ssh`` shells out to the peer via SSH — not hermetic
    for CI (hostnames like ``host-a.local`` don't resolve). This helper
    promotes the DISCOVERED node to VERIFIED by setting public_key +
    fingerprint + owner on the matching registry entry, exactly as the real
    flow would after a successful handshake.
    """
    for node in registry_self.list_all():
        if node.node_id == node_id:
            node.public_key = "ed25519:stubkey-for-test"
            node.fingerprint = "sha256:deadbeefdeadbeefdeadbeefdeadbeef"
            node.owner = "test-owner"
            break
    return True, "identity bound (mocked)"


class TestNodesCLI:
    """Tests for ``axi nodes`` subcommands."""

    def test_add_ssh_creates_entry(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main
        from axiom.vega.federation.discovery import NodeRegistry

        with patch.object(NodeRegistry, "fetch_identity_ssh", _mock_ssh_identity_binding):
            rc = main(["add", "host-a", "ben@host-a.local"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "host-a" in out

    def test_add_ssh_json(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main
        from axiom.vega.federation.discovery import NodeRegistry

        with patch.object(NodeRegistry, "fetch_identity_ssh", _mock_ssh_identity_binding):
            rc = main(["add", "host-a", "ben@host-a.local", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["added"] == "host-a"
        assert data["kind"] == "ssh"

    def test_add_a2a(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main

        rc = main(["add", "remote1", "--url", "https://node.example.com/.well-known/agent.json"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "remote1" in out

    def test_add_a2a_json(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main

        rc = main(["add", "remote1", "--url", "https://node.example.com/agent", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["kind"] == "a2a"

    def test_add_local(self, capsys, _patch_registry, identity_dir):
        with patch("axiom.vega.federation.identity._DEFAULT_KEYS_DIR", identity_dir):
            from axiom.extensions.builtins.federation.nodes_cli import main

            rc = main(["add", "local", "--json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["kind"] == "local"

    def test_add_invalid_ssh_target(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main

        rc = main(["add", "bad", "nope"])
        assert rc == 1

    def test_list_shows_registered(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main
        from axiom.vega.federation.discovery import NodeRegistry

        with patch.object(NodeRegistry, "fetch_identity_ssh", _mock_ssh_identity_binding):
            main(["add", "node1", "user@host1.local"])
        rc = main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "node1" in out

    def test_list_json(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main
        from axiom.vega.federation.discovery import NodeRegistry

        with patch.object(NodeRegistry, "fetch_identity_ssh", _mock_ssh_identity_binding):
            main(["add", "node1", "user@host1.local"])
        capsys.readouterr()  # drain add output
        rc = main(["list", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["display_name"] == "node1"

    def test_list_empty(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main

        rc = main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No nodes" in out

    def test_remove_entry(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main
        from axiom.vega.federation.discovery import NodeRegistry

        with patch.object(NodeRegistry, "fetch_identity_ssh", _mock_ssh_identity_binding):
            main(["add", "node1", "user@host1.local"])
        capsys.readouterr()
        rc = main(["remove", "node1", "--confirm"])
        assert rc == 0
        capsys.readouterr()
        # Verify it's gone
        main(["list", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert data == []

    def test_remove_nonexistent(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main

        rc = main(["remove", "ghost", "--confirm"])
        assert rc == 1

    def test_remove_json(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main
        from axiom.vega.federation.discovery import NodeRegistry

        with patch.object(NodeRegistry, "fetch_identity_ssh", _mock_ssh_identity_binding):
            main(["add", "node1", "user@host1.local"])
        capsys.readouterr()  # drain add output
        rc = main(["remove", "node1", "--json", "--confirm"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["removed"] == "node1"

    def test_status_no_nodes(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main

        rc = main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No nodes registered" in out

    def test_status_no_nodes_json(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main

        rc = main(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["nodes"] == []

    def test_status_with_mock_ssh(self, capsys, _patch_registry):
        """Status runs SSH health check (mocked)."""
        from axiom.extensions.builtins.federation.nodes_cli import main
        from axiom.vega.federation.discovery import NodeRegistry

        with patch.object(NodeRegistry, "fetch_identity_ssh", _mock_ssh_identity_binding):
            main(["add", "host-a", "ben@host-a.local"])
        capsys.readouterr()  # drain add output

        mock_health = {"healthy": True, "findings": [], "status": "healthy"}
        with patch(
            "axiom.extensions.builtins.federation.nodes_cli._ssh_health_check",
            return_value=mock_health,
        ):
            rc = main(["status", "--json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["health"]["status"] == "healthy"

    def test_upgrade_not_found(self, capsys, _patch_registry):
        from axiom.extensions.builtins.federation.nodes_cli import main

        rc = main(["upgrade", "ghost"])
        assert rc == 1

    def test_parser_has_all_subcommands(self):
        from axiom.extensions.builtins.federation.nodes_cli import build_parser

        parser = build_parser()
        actions = parser._subparsers._group_actions
        choices = set()
        for action in actions:
            choices.update(action.choices.keys())
        expected = {"add", "status", "upgrade", "remove", "list"}
        assert expected.issubset(choices)


class TestSshHealthCheck:
    """The SSH probe must invoke the remote command through a login shell so
    the peer's ~/.profile loads (~/.local/bin shim is on PATH)."""

    def test_uses_login_shell_to_pick_up_local_bin(self):
        from unittest.mock import MagicMock, patch

        from axiom.extensions.builtins.federation.nodes_cli import _ssh_health_check

        captured = {}

        def fake_run(cmd, *_args, **_kwargs):
            captured["cmd"] = cmd
            result = MagicMock()
            result.returncode = 0
            result.stdout = '{"status": "healthy", "healthy": true}'
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            out = _ssh_health_check("ben@host-a")

        assert out["status"] == "healthy"
        # Argv list, not shell=True string — and the remote command runs
        # under bash -lc so ~/.profile is sourced.
        cmd = captured["cmd"]
        assert isinstance(cmd, list), "ssh argv must be a list (no shell=True)"
        assert cmd[0] == "ssh"
        assert "ben@host-a" in cmd
        remote = cmd[-1]
        assert remote.startswith("bash -lc"), (
            f"remote command must run via login shell, got: {remote!r}"
        )
        assert "axi hygiene stat health --json" in remote or "neut tidy health --json" in remote

    def test_helpful_error_when_axi_not_on_peer_path(self):
        from unittest.mock import MagicMock, patch

        from axiom.extensions.builtins.federation.nodes_cli import _ssh_health_check

        def fake_run(*_args, **_kwargs):
            result = MagicMock()
            result.returncode = 127  # command not found
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            out = _ssh_health_check("ben@host-a")

        assert out["status"] == "unreachable"
        # The fallback message should hint at install-shim, not just "no output"
        assert "install-shim" in out["error"]
