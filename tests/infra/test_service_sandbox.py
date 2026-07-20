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
