# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the agent lifecycle framework.

Covers:
- AgentConfig / WatcherDef parsing from axiom-extension.toml
- ROUTINES.md loading into Extension
- agents CLI: start, stop, status, register, unregister
- Service label scoping (workspace hash)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.contracts import (
    parse_manifest,
)

# ---------------------------------------------------------------------------
# Fixture: extension dir with [agent] section
# ---------------------------------------------------------------------------


def _write_manifest(path: Path, content: str) -> Path:
    manifest = path / "axiom-extension.toml"
    manifest.write_text(content, encoding="utf-8")
    return manifest


@pytest.fixture
def agent_ext_dir(tmp_path):
    """Create a minimal agent extension with [agent] section."""
    d = tmp_path / "test-agent"
    d.mkdir()
    _write_manifest(
        d,
        """\
[extension]
name = "test-agent"
version = "0.1.0"
kind = "agent"

[agent]
heartbeat_interval = 30
startup = "daemon"
heartbeat_command = "test health --json"

[[agent.watchers]]
name = "inbox"
enabled = true
interval = 10
path = "runtime/inbox/raw"

[[agent.watchers]]
name = "onedrive"
enabled = false
interval = 30
module = "test_agent.watcher"
function = "run_cycle"
folder = "TestFolder"
""",
    )
    return d


@pytest.fixture
def non_agent_ext_dir(tmp_path):
    """Extension with no [agent] section."""
    d = tmp_path / "tool-ext"
    d.mkdir()
    _write_manifest(d, '[extension]\nname = "tool-ext"\nkind = "tool"\n')
    return d


# ---------------------------------------------------------------------------
# Test: AgentConfig + WatcherDef dataclass parsing
# ---------------------------------------------------------------------------


class TestAgentConfigParsing:
    def test_agent_section_parsed(self, agent_ext_dir):
        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        assert ext.agent is not None
        assert ext.agent.heartbeat_interval == 30
        assert ext.agent.startup == "daemon"

    def test_watchers_parsed(self, agent_ext_dir):
        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        assert len(ext.agent.watchers) == 2

        inbox = ext.agent.watchers[0]
        assert inbox.name == "inbox"
        assert inbox.enabled is True
        assert inbox.interval == 10
        assert inbox.path == "runtime/inbox/raw"

        onedrive = ext.agent.watchers[1]
        assert onedrive.name == "onedrive"
        assert onedrive.enabled is False
        assert onedrive.interval == 30
        assert onedrive.module == "test_agent.watcher"
        assert onedrive.function == "run_cycle"

    def test_no_agent_section_returns_none(self, non_agent_ext_dir):
        ext = parse_manifest(non_agent_ext_dir / "axiom-extension.toml")
        assert ext.agent is None

    def test_agent_section_defaults(self, tmp_path):
        """[agent] with no fields uses defaults."""
        d = tmp_path / "defaults-agent"
        d.mkdir()
        _write_manifest(
            d,
            """\
[extension]
name = "defaults-agent"
kind = "agent"

[agent]
""",
        )
        ext = parse_manifest(d / "axiom-extension.toml")
        assert ext.agent is not None
        assert ext.agent.heartbeat_interval == 300
        assert ext.agent.startup == "lazy"
        assert ext.agent.watchers == []

    def test_is_always_on(self, agent_ext_dir):
        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        assert ext.agent.is_always_on is True

    def test_lazy_not_always_on(self, tmp_path):
        d = tmp_path / "lazy-agent"
        d.mkdir()
        _write_manifest(
            d,
            '[extension]\nname = "lazy"\nkind = "agent"\n\n[agent]\nstartup = "lazy"\n',
        )
        ext = parse_manifest(d / "axiom-extension.toml")
        assert ext.agent.is_always_on is False


# ---------------------------------------------------------------------------
# Test: ROUTINES.md loading
# ---------------------------------------------------------------------------


class TestRoutinesMd:
    def test_routines_md_loaded(self, agent_ext_dir):
        """ROUTINES.md content is loaded into agent config."""
        (agent_ext_dir / "ROUTINES.md").write_text(
            "# Routines\n\nRun health checks every 5 minutes.\n"
        )
        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        assert ext.agent.routines_md is not None
        assert "health checks" in ext.agent.routines_md

    def test_no_routines_md_is_none(self, agent_ext_dir):
        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        assert ext.agent.routines_md is None


# ---------------------------------------------------------------------------
# Test: Service label generation
# ---------------------------------------------------------------------------


class TestServiceLabel:
    def test_service_label_contains_agent_name(self, agent_ext_dir):
        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        label = ext.agent.service_label(ext.name)
        assert "test-agent" in label
        assert label.startswith("com.axiom.")

    def test_service_label_stable(self, agent_ext_dir):
        """Same extension always produces same label."""
        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        l1 = ext.agent.service_label(ext.name)
        l2 = ext.agent.service_label(ext.name)
        assert l1 == l2


# ---------------------------------------------------------------------------
# Test: agents CLI module
# ---------------------------------------------------------------------------


class TestAgentsCli:
    """Test the `axi agents` command handlers."""

    @pytest.fixture(autouse=True)
    def _autonomy_on(self, monkeypatch):
        # Registration is gated by the master autonomy switch (default OFF).
        # These tests exercise the on-path; the OFF short-circuit lives in
        # tests/agents/test_autonomy_gate.py.
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.autonomy_enabled", lambda: True
        )

    def test_status_no_agents(self):
        """Status with no always-on agents prints clean output."""
        from axiom.extensions.builtins.agents.cli import main as agents_main

        # No agents discovered — should exit 0
        with patch(
            "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
            return_value=[],
        ):
            rc = agents_main(["status"])
        assert rc == 0

    def test_status_with_agents(self, agent_ext_dir):
        """Status shows discovered agents."""
        from axiom.extensions.builtins.agents.cli import main as agents_main

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
            ),
            patch(
                "axiom.extensions.builtins.agents.cli._get_service_status",
                return_value="not_installed",
            ),
        ):
            rc = agents_main(["status"])
        assert rc == 0

    def test_start_registers_and_starts(self, agent_ext_dir):
        """`axi agents start` registers the per-slot Background Service.

        Post-0.12.0: there is exactly one Background Service per slot.
        The optional name argument is accepted (with a one-line note)
        but does not change behavior — start always operates on the
        single Background Service.
        """
        from axiom.extensions.builtins.agents.cli import main as agents_main

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.install.return_value = True
        mock_mgr.start.return_value = True
        mock_mgr.provider_name = "test"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
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
            rc = agents_main(["start", "test-agent"])
        assert rc == 0
        mock_mgr.install.assert_called_once()
        mock_mgr.start.assert_called_once()

    def test_stop_agent(self, agent_ext_dir):
        """`axi agents stop` stops the per-slot Background Service."""
        from axiom.extensions.builtins.agents.cli import main as agents_main

        mock_mgr = MagicMock()
        mock_mgr.stop.return_value = True

        with patch(
            "axiom.extensions.builtins.agents.cli._make_background_service_manager",
            return_value=mock_mgr,
        ):
            rc = agents_main(["stop", "test-agent"])
        assert rc == 0
        mock_mgr.stop.assert_called_once()

    def test_unregister_agent(self, agent_ext_dir):
        """`axi agents unregister` cleans up legacy units + uninstalls Background Service."""
        from axiom.extensions.builtins.agents.cli import main as agents_main

        mock_mgr = MagicMock()
        mock_mgr.uninstall.return_value = True

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._cleanup_legacy_per_agent_services",
                return_value=[],
            ),
            patch(
                "axiom.extensions.builtins.agents.cli._make_background_service_manager",
                return_value=mock_mgr,
            ),
        ):
            rc = agents_main(["unregister", "test-agent"])
        assert rc == 0
        mock_mgr.uninstall.assert_called_once()

    def test_start_unknown_agent(self):
        """Post-0.12.0 a name arg is informational; start always succeeds if BS does."""
        from axiom.extensions.builtins.agents.cli import main as agents_main

        mock_mgr = MagicMock()
        mock_mgr.install.return_value = True
        mock_mgr.start.return_value = True
        mock_mgr.provider_name = "test"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[],
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
            # No daemon agents → nothing to register; rc=0 is fine
            rc = agents_main(["start", "nonexistent"])
        assert rc == 0

    def test_register_all(self, agent_ext_dir):
        """register (no name) installs the Background Service for the slot."""
        from axiom.extensions.builtins.agents.cli import main as agents_main

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.install.return_value = True
        mock_mgr.start.return_value = True
        mock_mgr.provider_name = "test"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
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
            rc = agents_main(["start"])
        assert rc == 0
        mock_mgr.install.assert_called()
        mock_mgr.start.assert_called()


# ---------------------------------------------------------------------------
# Test: Extension.capabilities includes agent info
# ---------------------------------------------------------------------------


class TestRegisterAllDaemonAgents:
    """Tests for the shared register_all_daemon_agents() helper.

    This is the one code path used by the wizard, the installer finalize
    phase, and the CLI self-heal hook. Must never raise.
    """

    @pytest.fixture(autouse=True)
    def _autonomy_on(self, monkeypatch):
        # Registration is gated by the master autonomy switch (default OFF).
        # These tests exercise the on-path mechanics; the OFF short-circuit is
        # covered in tests/agents/test_autonomy_gate.py.
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.autonomy_enabled", lambda: True
        )

    def test_returns_result_per_daemon_agent(self, agent_ext_dir):
        """Post-0.12.0: register returns a coordinator result + cleanup results.

        The contract changed: instead of one result per daemon agent, there's
        exactly one result for the Background Service registration (plus zero
        or more legacy-cleanup results).
        """
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.install.return_value = True
        mock_mgr.start.return_value = True
        mock_mgr.provider_name = "systemd"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
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

        bg_results = [r for r in results if r.agent_name == "background-service"]
        assert len(bg_results) == 1
        assert bg_results[0].ok is True
        assert bg_results[0].provider == "systemd"

    def test_skips_non_daemon_agents(self, tmp_path):
        """Agents with startup='lazy' are not registered as services."""
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents

        d = tmp_path / "lazy-agent"
        d.mkdir()
        _write_manifest(
            d,
            '[extension]\nname = "lazy"\nkind = "agent"\n\n[agent]\nstartup = "lazy"\n',
        )
        ext = parse_manifest(d / "axiom-extension.toml")
        with patch(
            "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
            return_value=[ext],
        ):
            results = register_all_daemon_agents()
        assert results == []

    def test_soft_fail_on_install_returning_false(self, agent_ext_dir):
        """If Background Service install returns False, the result is recorded as failed."""
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.install.return_value = False
        mock_mgr.provider_name = "systemd"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
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

        bg_results = [r for r in results if r.agent_name == "background-service"]
        assert len(bg_results) == 1
        assert bg_results[0].ok is False
        assert bg_results[0].registered is False
        mock_mgr.start.assert_not_called()

    def test_unchanged_flag_when_service_already_running(self, agent_ext_dir):
        """Idempotent re-run path: service was running before install,
        install + start succeed → result.unchanged is True (so the CLI
        can suppress "✓ registered" noise per #208)."""
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents
        from axiom.infra.services import ServiceInfo, ServiceStatus

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.status.return_value = ServiceInfo(
            name="background-service",
            status=ServiceStatus.RUNNING,
            provider="launchd",
        )
        mock_mgr.install.return_value = True
        mock_mgr.start.return_value = True
        mock_mgr.provider_name = "launchd"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
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

        bg = [r for r in results if r.agent_name == "background-service"]
        assert len(bg) == 1
        assert bg[0].ok is True
        assert bg[0].unchanged is True

    def test_unchanged_false_for_fresh_install(self, agent_ext_dir):
        """Fresh install path: service NOT_INSTALLED prior → unchanged=False."""
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents
        from axiom.infra.services import ServiceInfo, ServiceStatus

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.status.return_value = ServiceInfo(
            name="background-service",
            status=ServiceStatus.NOT_INSTALLED,
            provider="launchd",
        )
        mock_mgr.install.return_value = True
        mock_mgr.start.return_value = True
        mock_mgr.provider_name = "launchd"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
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

        bg = [r for r in results if r.agent_name == "background-service"]
        assert bg[0].unchanged is False

    def test_exception_recorded_not_raised(self, agent_ext_dir):
        """An exception in the Background Service install does not kill the call."""
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.install.side_effect = RuntimeError("boom")
        mock_mgr.provider_name = "systemd"

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
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

        bg_results = [r for r in results if r.agent_name == "background-service"]
        assert len(bg_results) == 1
        assert bg_results[0].ok is False
        assert "boom" in bg_results[0].error


class TestMissingDaemonAgents:
    def test_flags_not_installed(self, agent_ext_dir):
        from axiom.extensions.builtins.agents.cli import missing_daemon_agents

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.status.return_value = MagicMock(status="not_installed")

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
            ),
            patch(
                "axiom.extensions.builtins.agents.cli._make_service_manager",
                return_value=mock_mgr,
            ),
        ):
            missing = missing_daemon_agents()
        assert missing == ["test-agent"]

    def test_ignores_running(self, agent_ext_dir):
        from axiom.extensions.builtins.agents.cli import missing_daemon_agents

        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        mock_mgr = MagicMock()
        mock_mgr.status.return_value = MagicMock(status="running")

        with (
            patch(
                "axiom.extensions.builtins.agents.cli._discover_agent_extensions",
                return_value=[ext],
            ),
            patch(
                "axiom.extensions.builtins.agents.cli._make_service_manager",
                return_value=mock_mgr,
            ),
        ):
            missing = missing_daemon_agents()
        assert missing == []


class TestSystemdLinger:
    """Tests for the linger-enable logic in SystemdProvider.

    Linger is required for user systemd services to survive logout and
    reboot. Without it, even an enabled unit will not come back on boot.
    """

    def test_linger_check_uses_loginctl(self, monkeypatch):
        from axiom.infra.services import SystemdProvider

        monkeypatch.setenv("USER", "testuser")
        prov = SystemdProvider()
        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value = MagicMock(stdout="yes\n", returncode=0)
            assert prov._linger_enabled() is True
            call_args = run.call_args[0][0]
            assert call_args[0] == "loginctl"
            assert "testuser" in call_args

    def test_linger_disabled_returns_false(self, monkeypatch):
        from axiom.infra.services import SystemdProvider

        monkeypatch.setenv("USER", "testuser")
        prov = SystemdProvider()
        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value = MagicMock(stdout="no\n", returncode=0)
            assert prov._linger_enabled() is False

    def test_try_enable_linger_uses_passwordless_sudo(self, monkeypatch):
        from axiom.infra.services import SystemdProvider

        monkeypatch.setenv("USER", "testuser")
        prov = SystemdProvider()
        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            ok, msg = prov._try_enable_linger()
            assert ok is True
            cmd = run.call_args[0][0]
            # Must use sudo -n (non-interactive) to avoid password prompts on laptops.
            assert cmd[:2] == ["sudo", "-n"]
            assert "enable-linger" in cmd
            assert "testuser" in cmd

    def test_try_enable_linger_soft_fails_with_remediation(self, monkeypatch):
        from axiom.infra.services import SystemdProvider

        monkeypatch.setenv("USER", "testuser")
        prov = SystemdProvider()
        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stderr="sudo: a password is required")
            ok, msg = prov._try_enable_linger()
            assert ok is False
            # Remediation must include the exact command to run.
            assert "sudo loginctl enable-linger testuser" in msg


class TestExtensionCapabilities:
    def test_capabilities_includes_agent(self, agent_ext_dir):
        ext = parse_manifest(agent_ext_dir / "axiom-extension.toml")
        caps = ext.capabilities
        assert any("daemon" in c.lower() or "agent" in c.lower() for c in caps)

    def test_capabilities_no_agent(self, non_agent_ext_dir):
        ext = parse_manifest(non_agent_ext_dir / "axiom-extension.toml")
        caps = ext.capabilities
        assert not any("daemon" in c.lower() or "agent" in c.lower() for c in caps)
