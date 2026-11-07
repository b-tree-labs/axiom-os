# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi update` must honor the agent-registration consent gate (ADR-048 / the
2026-05-28 silent-install incident).

The update flow re-registers daemon agents so an upgrade keeps their services
pointing at the new binary. But it must NOT install host services the operator
never consented to — that would route around the consent gate via the back
door. So `_register_agents` registers only the operator's approved set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.update.cli import Updater


def _agent(name: str):
    return SimpleNamespace(name=name, agent=SimpleNamespace(is_always_on=True))


@pytest.fixture
def installed(monkeypatch):
    """Discovery returns three daemon agents; record which ones get install()'d."""
    calls: list[str] = []

    def fake_mgr(ext):
        m = MagicMock()
        m.install.side_effect = lambda: (calls.append(ext.name), True)[1]
        m.start.return_value = True
        return m

    from axiom.extensions.builtins.agents import cli as acli

    monkeypatch.setattr(
        acli,
        "_discover_agent_extensions",
        lambda: [_agent("diagnostics"), _agent("hygiene"), _agent("release")],
    )
    monkeypatch.setattr(acli, "_make_service_manager", fake_mgr)
    return calls


def _consent(monkeypatch, **kw):
    from axiom.extensions.builtins.agents import consent as C

    monkeypatch.setattr(C, "load_consent", lambda: C.AgentConsent(**kw))


class TestUpdateRespectsConsent:
    def test_opted_out_registers_nothing(self, installed, monkeypatch):
        _consent(monkeypatch, decided=True, opted_out=True)
        Updater()._register_agents()
        assert installed == []

    def test_undecided_registers_nothing(self, installed, monkeypatch):
        # An upgrade must not be the moment we first install services unasked.
        _consent(monkeypatch, decided=False)
        Updater()._register_agents()
        assert installed == []

    def test_registers_only_the_approved_subset(self, installed, monkeypatch):
        _consent(monkeypatch, decided=True, enabled=["hygiene"])
        Updater()._register_agents()
        assert installed == ["hygiene"]

    def test_reports_skip_when_consent_withholds(self, installed, monkeypatch):
        _consent(monkeypatch, decided=True, opted_out=True)
        u = Updater()
        u._register_agents()
        agent_results = [r for r in u.results if r.step == "agents"]
        assert agent_results and agent_results[-1].success
        assert not agent_results[-1].changed  # nothing installed
