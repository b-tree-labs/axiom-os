# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi agents register` consent UX: one-click all / opt-out / à-la-carte, plus
the informed-consent surface (`agents info`, `?N` drill-in). The operator must
never have a host service installed without an explicit choice."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.agents import cli as A
from axiom.extensions.builtins.agents import consent as C


@dataclass
class _Agent:
    heartbeat_interval: int = 900
    heartbeat_command: str = "triage heartbeat"
    startup: str = "daemon"

    @property
    def is_registrable(self) -> bool:
        return self.startup in ("daemon", "eager") and bool(self.heartbeat_command)


@dataclass
class _Ext:
    name: str
    description: str = ""
    agent: _Agent | None = field(default_factory=_Agent)
    root: object = None


CANDS = [
    _Ext("diagnostics", "System-health checks — houses TRIAGE", _Agent(900, "triage heartbeat")),
    _Ext("hygiene", "Resource stewardship — houses TIDY", _Agent(3600, "tidy health --json")),
]


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    # Consent file -> tmp; discovery -> our fake candidates; no real installs.
    monkeypatch.setattr(C, "get_user_state_dir", lambda: tmp_path)
    monkeypatch.setattr(A, "_discover_agent_extensions", lambda: list(CANDS))
    return tmp_path


def _args(**kw):
    base = {"all": False, "none": False, "agents": None}
    base.update(kw)
    return SimpleNamespace(**base)


class TestNonInteractiveFlags:
    def test_all_records_every_agent_and_registers(self, monkeypatch):
        monkeypatch.setattr(A, "register_all_daemon_agents", lambda: [])
        rc = A._cmd_register(_args(all=True))
        assert rc == 0
        c = C.load_consent()
        assert c.decided and not c.opted_out
        assert c.enabled == ["diagnostics", "hygiene"]

    def test_none_opts_out_and_uninstalls(self, monkeypatch):
        mgr = MagicMock()
        monkeypatch.setattr(A, "_make_background_service_manager", lambda: mgr)
        reg = MagicMock()
        monkeypatch.setattr(A, "register_all_daemon_agents", reg)
        rc = A._cmd_register(_args(none=True))
        assert rc == 0
        assert C.load_consent().opted_out is True
        reg.assert_not_called()          # opt-out never installs
        mgr.uninstall.assert_called_once()

    def test_agents_subset_records_only_those(self, monkeypatch):
        monkeypatch.setattr(A, "register_all_daemon_agents", lambda: [])
        rc = A._cmd_register(_args(agents="hygiene"))
        assert rc == 0
        assert C.load_consent().enabled == ["hygiene"]

    def test_unknown_agent_errors_without_recording(self, monkeypatch):
        monkeypatch.setattr(A, "register_all_daemon_agents", lambda: [])
        rc = A._cmd_register(_args(agents="hygiene,bogus"))
        assert rc == 1
        assert C.load_consent().decided is False  # nothing recorded


class TestNonInteractiveNoFlag:
    def test_lists_and_instructs_but_installs_nothing(self, monkeypatch, capsys):
        # No TTY, no flag -> detect + instruct, never install.
        monkeypatch.setattr(A.sys.stdin, "isatty", lambda: False)
        reg = MagicMock()
        monkeypatch.setattr(A, "register_all_daemon_agents", reg)
        rc = A._cmd_register(_args())
        out = capsys.readouterr().out
        assert rc == 0
        reg.assert_not_called()
        assert "diagnostics" in out and "hygiene" in out
        assert "register --all" in out
        assert C.load_consent().decided is False


class TestInfoSurface:
    def test_brief_line_has_description_and_cadence(self):
        line = A._agent_brief(1, CANDS[1])
        assert "hygiene" in line and "TIDY" in line and "every 1h" in line

    def test_info_all_shows_each_agents_tick_command(self, capsys):
        rc = A._cmd_info(SimpleNamespace(name=None))
        out = capsys.readouterr().out
        assert rc == 0
        assert "triage heartbeat" in out and "tidy health --json" in out

    def test_info_unknown_name_errors(self, capsys):
        rc = A._cmd_info(SimpleNamespace(name="nope"))
        assert rc == 1
        assert "Unknown agent" in capsys.readouterr().out

    def test_humanize_interval(self):
        assert A._humanize_interval(10) == "every 10s"
        assert A._humanize_interval(900) == "every 15m"
        assert A._humanize_interval(7200) == "every 2h"


class TestConsentStatusLine:
    def test_undecided(self):
        line = A._consent_status_line(C.AgentConsent())
        assert "not yet decided" in line and "agents register" in line

    def test_opted_out(self):
        line = A._consent_status_line(C.AgentConsent(decided=True, opted_out=True))
        assert "opted out" in line

    def test_enabled_lists_agents_and_version(self):
        c = C.AgentConsent(decided=True, enabled=["hygiene"], decided_version="0.22.0")
        line = A._consent_status_line(c)
        assert "enabled: hygiene" in line and "0.22.0" in line
