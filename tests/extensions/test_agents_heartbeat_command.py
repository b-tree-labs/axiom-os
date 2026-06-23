# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for heartbeat_command-based registration.

Guards the v0.9.0–v0.10.2 regression class: daemon agents must not be
registered as services if they have no declared heartbeat_command. Doing
so would invoke a nonexistent subcommand and crash-loop.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.contracts import parse_manifest


def _write_manifest(dir_: Path, content: str) -> None:
    (dir_ / "axiom-extension.toml").write_text(content, encoding="utf-8")


@pytest.fixture
def agent_with_heartbeat(tmp_path):
    d = tmp_path / "runnable-agent"
    d.mkdir()
    _write_manifest(
        d,
        """\
[extension]
name = "runnable-agent"
kind = "agent"

[[cli.commands]]
noun = "runnable"
module = "fake.cli"

[agent]
heartbeat_interval = 60
startup = "daemon"
heartbeat_command = "runnable status --json"
""",
    )
    return parse_manifest(d / "axiom-extension.toml")


@pytest.fixture
def agent_without_heartbeat(tmp_path):
    d = tmp_path / "incomplete-agent"
    d.mkdir()
    _write_manifest(
        d,
        """\
[extension]
name = "incomplete-agent"
kind = "agent"

[[cli.commands]]
noun = "incomplete"
module = "fake.cli"

[agent]
heartbeat_interval = 60
startup = "daemon"
""",
    )
    return parse_manifest(d / "axiom-extension.toml")


class TestIsRegistrable:
    def test_with_heartbeat_is_registrable(self, agent_with_heartbeat):
        assert agent_with_heartbeat.agent.is_registrable is True

    def test_without_heartbeat_is_not_registrable(self, agent_without_heartbeat):
        assert agent_without_heartbeat.agent.is_always_on is True
        assert agent_without_heartbeat.agent.is_registrable is False

    def test_lazy_is_not_registrable(self, tmp_path):
        d = tmp_path / "lazy-agent"
        d.mkdir()
        _write_manifest(
            d,
            '[extension]\nname = "lazy"\nkind = "agent"\n\n[agent]\nstartup = "lazy"\n'
            'heartbeat_command = "anything"\n',
        )
        ext = parse_manifest(d / "axiom-extension.toml")
        assert ext.agent.is_registrable is False


class TestAgentServiceArgs:
    def test_uses_heartbeat_command_not_noun_heartbeat(self, agent_with_heartbeat):
        from axiom.extensions.builtins.agents.cli import _agent_service_args

        args = _agent_service_args(agent_with_heartbeat)
        # The critical regression was emitting `<noun> heartbeat`. We must
        # now use the declared heartbeat_command verbatim.
        assert args == ["runnable", "status", "--json"]
        assert "heartbeat" not in args, "legacy fake-subcommand must not reappear"


class TestRegisterRefusesUnsafe:
    def test_agent_without_heartbeat_is_skipped_not_crashed(self, agent_without_heartbeat):
        """Post-0.12.0: agents without heartbeat_command are silently skipped.

        The Background Service pattern filters at discovery time via
        is_registrable. With no registrable daemons, no Background
        Service is installed (nothing to dispatch) — no crash, no
        crash-looping unit.
        """
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[agent_without_heartbeat],
            ),
            patch(
                "axiom.extensions.builtins.agents.cli._cleanup_legacy_per_agent_services",
                return_value=[],
            ),
            patch(
                "axiom.extensions.builtins.agents.cli._make_background_service_manager",
            ) as mgr_factory,
        ):
            results = register_all_daemon_agents()

        # Background Service manager is NEVER built when there are no registrable daemons
        mgr_factory.assert_not_called()
        # No Background Service result returned (nothing to dispatch -> nothing to install)
        bg_results = [r for r in results if r.agent_name == "background-service"]
        assert bg_results == []

    def test_agent_with_heartbeat_is_registered(self, agent_with_heartbeat):
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents

        mock_mgr = MagicMock()
        mock_mgr.install.return_value = True
        mock_mgr.start.return_value = True
        mock_mgr.provider_name = "systemd"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[agent_with_heartbeat],
            ),
            patch(
                "axiom.extensions.builtins.agents.cli._cleanup_legacy_per_agent_services",
                return_value=[],
            ),
            patch(
                "axiom.extensions.builtins.agents.cli._make_background_service_manager",
                return_value=mock_mgr,
            ),
        ):
            results = register_all_daemon_agents()

        assert len(results) == 1
        assert results[0].ok is True
