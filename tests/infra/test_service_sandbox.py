# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the sandbox-by-default contract per ADR-036 §D10.

Platform-managed services run under hardened defaults so a daemon polling
external APIs and running heuristic LLM diagnosis cannot read arbitrary
user secrets (`~/.ssh`, `~/.aws`, etc.).

Phase 0 ships:
- Linux/systemd: NoNewPrivileges, PrivateTmp, ProtectSystem=strict,
  ProtectHome=read-only by default, RestrictAddressFamilies=AF_UNIX/INET/INET6,
  RestrictNamespaces=true, LockPersonality=true, MemoryDenyWriteExecute=true.
- macOS/launchd: ProcessType=Background. Sandbox-exec hook with permissive
  default profile (Phase 0); tightened in Phase 2/3.
- Windows: TODO; AppContainer integration tracked as follow-on.

Manifests may relax via [agent.sandbox] but cannot escape at runtime.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from axiom.extensions.contracts import SandboxConfig
from axiom.infra.paths import get_user_state_dir
from axiom.infra.services import LaunchdProvider, ServiceDef, SystemdProvider

# Absolute, always-resolvable binary. The provider install guards refuse a
# unit/plist whose launch target can't run, and some of these tests restrict
# PATH to exercise env-bounding — a bare name ("axi") would not resolve under
# a patched PATH. sys.executable is a real interpreter, resolvable regardless.
# These tests assert sandbox/hardening directives, not ExecStart content, so
# the specific binary is immaterial.


def _systemd_svc(env=None, interval_secs=300):
    return ServiceDef(
        name="tidy-agent",
        binary=sys.executable,
        args=["tidy", "health", "--json"],
        env=env or {},
        interval_secs=interval_secs,
    )


def _launchd_svc(env=None, interval_secs=300):
    return ServiceDef(
        name="release-agent",
        binary=sys.executable,
        args=["rivet", "heartbeat"],
        env=env or {},
        interval_secs=interval_secs,
    )


# ---------------------------------------------------------------------------
# Systemd hardening directives (ADR-036 §D10)
# ---------------------------------------------------------------------------


class TestSystemdSandboxDefaults:
    def _install_and_read(self, tmp_path, monkeypatch, svc):
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)
        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            prov.install(svc)
        return (tmp_path / "neut-tidy-agent.service").read_text()

    def test_no_new_privileges(self, tmp_path, monkeypatch):
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        assert "NoNewPrivileges=true" in unit

    def test_private_tmp(self, tmp_path, monkeypatch):
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        assert "PrivateTmp=true" in unit

    def test_protect_system_strict(self, tmp_path, monkeypatch):
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        assert "ProtectSystem=strict" in unit

    def test_protect_home_read_only_default(self, tmp_path, monkeypatch):
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        assert "ProtectHome=read-only" in unit

    def test_restrict_address_families(self, tmp_path, monkeypatch):
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit

    def test_restrict_namespaces(self, tmp_path, monkeypatch):
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        assert "RestrictNamespaces=true" in unit

    def test_lock_personality(self, tmp_path, monkeypatch):
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        assert "LockPersonality=true" in unit

    def test_memory_deny_write_execute(self, tmp_path, monkeypatch):
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        assert "MemoryDenyWriteExecute=true" in unit

    def test_state_dir_writable_via_read_write_paths(self, tmp_path, monkeypatch):
        """The slot's state dir must be in ReadWritePaths so the agent can
        actually write its persistent state. Without this, ProtectHome=read-only
        would block all writes."""
        unit = self._install_and_read(tmp_path, monkeypatch, _systemd_svc())
        # The slot state dir (~/.axi/ on default slot) must be explicitly writable
        assert "ReadWritePaths=" in unit


# ---------------------------------------------------------------------------
# Launchd ProcessType + sandbox hook (ADR-036 §D10)
# ---------------------------------------------------------------------------


class TestLaunchdSandboxDefaults:
    def _install_and_read(self, tmp_path, monkeypatch, svc):
        prov = LaunchdProvider()
        monkeypatch.setattr(
            prov, "_plist_path", lambda svc: tmp_path / f"{svc.service_id}.plist"
        )
        monkeypatch.setattr("axiom.infra.services._get_services_dir", lambda: tmp_path / "logs")
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False):
            with patch("axiom.infra.services.subprocess.run"):
                prov.install(svc)
        return (tmp_path / "com.axiom-os-lm.release-agent.plist").read_text()

    def test_process_type_background(self, tmp_path, monkeypatch):
        plist = self._install_and_read(tmp_path, monkeypatch, _launchd_svc())
        assert "<key>ProcessType</key>" in plist
        assert "<string>Background</string>" in plist

    def test_low_priority_io(self, tmp_path, monkeypatch):
        """Background services should default to low-priority IO so they don't
        contend with the user's interactive workload."""
        plist = self._install_and_read(tmp_path, monkeypatch, _launchd_svc())
        assert "<key>LowPriorityIO</key>" in plist


# ---------------------------------------------------------------------------
# Per-agent relaxation — ADR-036 §D10, "manifests may relax (but not remove)"
#
# Phase 0 shipped one uniform profile for every agent, which is safe and
# unusable: an agent that legitimately needs one path outside its state dir
# has no sanctioned way to ask, so the only available move is loosening the
# default for everybody. These tests fix the shape of the narrow door.
# ---------------------------------------------------------------------------



def _svc_with_sandbox(sandbox):
    return ServiceDef(
        name="tidy-agent",
        binary=sys.executable,
        args=["-c", "pass"],
        env={},
        interval_secs=300,
        sandbox=sandbox,
    )


class TestRelaxationReachesTheUnit:
    def test_protect_home_can_be_widened(self):
        from axiom.infra.services import _systemd_sandbox_directives

        out = _systemd_sandbox_directives(_svc_with_sandbox(SandboxConfig(protect_home="tmpfs")))
        assert "ProtectHome=tmpfs" in out
        assert "ProtectHome=read-only" not in out

    def test_an_extra_writable_path_is_added_not_substituted(self):
        # The state dir must survive: an agent that gains ~/.config/gh and
        # loses its own state directory cannot run at all.
        from axiom.infra.services import _systemd_sandbox_directives

        out = _systemd_sandbox_directives(
            _svc_with_sandbox(SandboxConfig(read_write_paths=["/tmp/agent-scratch"]))
        )
        rw = [ln for ln in out.splitlines() if ln.startswith("ReadWritePaths=")]
        assert len(rw) == 1, "one directive, not two — systemd takes the last"
        assert "/tmp/agent-scratch" in rw[0]
        assert str(get_user_state_dir()) in rw[0]

    def test_no_sandbox_block_is_the_phase_0_default_exactly(self):
        from axiom.infra.services import _systemd_sandbox_directives

        assert _systemd_sandbox_directives(_svc_with_sandbox(None)) == (
            _systemd_sandbox_directives(_systemd_svc())
        )


class TestRelaxationCannotBecomeRemoval:
    """The invariant the whole block rests on."""

    @pytest.mark.parametrize(
        "directive",
        [
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "RestrictNamespaces=true",
            "LockPersonality=true",
            "MemoryDenyWriteExecute=true",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        ],
    )
    def test_no_relaxation_removes_a_hardening_directive(self, directive):
        from axiom.infra.services import _systemd_sandbox_directives

        # Every knob at its most permissive at once.
        out = _systemd_sandbox_directives(
            _svc_with_sandbox(
                SandboxConfig(protect_home="off", read_write_paths=["/tmp/a", "/tmp/b"])
            )
        )
        assert directive in out

    @pytest.mark.parametrize("greedy", ["/", "/usr", "/etc", "//", "/../"])
    def test_a_writable_path_that_grants_everything_is_refused(self, greedy):
        # A relaxation wide enough to defeat ProtectSystem is a removal wearing
        # a relaxation's clothes. Refused loudly at generation time rather than
        # written into a unit that survives reboots.
        from axiom.infra.services import _systemd_sandbox_directives

        with pytest.raises(ValueError, match="too broad|refused"):
            _systemd_sandbox_directives(_svc_with_sandbox(SandboxConfig(read_write_paths=[greedy])))

    def test_an_unknown_protect_home_value_is_refused_not_ignored(self):
        # Ignoring it would silently apply the default while the manifest
        # claims otherwise, which is the worst of both.
        from axiom.infra.services import _systemd_sandbox_directives

        with pytest.raises(ValueError, match="protect_home"):
            _systemd_sandbox_directives(_svc_with_sandbox(SandboxConfig(protect_home="nope")))
