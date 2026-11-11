# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Re-enabling autonomy must actually restore heartbeats.

The master gate is checked at TWO choke points — runtime dispatch and OS-timer
registration. Turning autonomy off neuters a surviving timer at runtime, which
is idempotent. Turning it back on was NOT: registration never re-ran, so an
operator who declined at install and later opted in got a setting that read
``true``, no OS timer, no dispatch, and no explanation.
"""

from __future__ import annotations

import pytest


def test_reconcile_registers_when_autonomy_turned_on(monkeypatch):
    from axiom.extensions.builtins.settings import reconcile as rec

    called: list[bool] = []
    monkeypatch.setattr(rec, "_register_daemon_agents", lambda: called.append(True) or [])

    rec.reconcile_autonomy_registration(True)

    assert called == [True], "turning autonomy on must re-run OS-timer registration"


def test_reconcile_is_a_noop_when_autonomy_turned_off(monkeypatch):
    from axiom.extensions.builtins.settings import reconcile as rec

    called: list[bool] = []
    monkeypatch.setattr(rec, "_register_daemon_agents", lambda: called.append(True) or [])

    rec.reconcile_autonomy_registration(False)

    assert called == [], "turning autonomy off is handled by the runtime gate; do not register"


def test_reconcile_never_raises_when_agents_unavailable(monkeypatch):
    """A settings write must not fail because the agents extension is absent."""
    from axiom.extensions.builtins.settings import reconcile as rec

    def boom():
        raise ImportError("agents extension not installed")

    monkeypatch.setattr(rec, "_register_daemon_agents", boom)

    assert rec.reconcile_autonomy_registration(True) is None


# --- the silent-no-op warning ---------------------------------------------


@pytest.mark.parametrize(
    "autonomy_on,service_status,expect_warning",
    [
        (True, "not_installed", True),  # the broken state: on, but nothing registered
        (True, "running", False),
        (False, "not_installed", False),  # off + no timer is the coherent default
        (False, "running", False),
    ],
)
def test_status_warns_only_when_enabled_but_unregistered(
    autonomy_on, service_status, expect_warning
):
    from axiom.extensions.builtins.agents.cli import autonomy_registration_warning

    warning = autonomy_registration_warning(autonomy_on, service_status)

    assert bool(warning) is expect_warning
    if warning:
        assert "agents register" in warning, "the warning must name the fix"


# --- first-tick stagger ----------------------------------------------------


def test_first_tick_dispatch_is_budgeted():
    """Re-enabling must not fire every overdue agent in one tick."""
    from axiom.agents.background_service import select_due

    names = [f"agent-{i}" for i in range(6)]
    due = select_due(names, max_per_tick=2)

    assert len(due) == 2, "a dispatch budget bounds the herd on re-enable"
    assert due == names[:2], "selection is stable, so the rest follow on later ticks"


def test_dispatch_budget_allows_all_when_under_budget():
    from axiom.agents.background_service import select_due

    assert select_due(["a"], max_per_tick=2) == ["a"]


# --- the explicit Autonomy line ---------------------------------------------
#
# The warning above fires only in the ON-but-not-installed pairing. The
# OFF-and-silent screen was still possible: `axi agents status` read
# "running", listed consented agents with intervals and last-run times —
# every line true, the whole implying dispatch was happening. The master
# gate must be STATED, first, in both states, not only shouted about in
# the one incoherent pairing.


def test_status_leads_with_autonomy_on_line():
    from axiom.extensions.builtins.agents.cli import autonomy_status_lines

    lines = autonomy_status_lines(True)

    assert len(lines) == 1
    assert "Autonomy:" in lines[0]
    assert "ON" in lines[0]


def test_status_states_autonomy_off_and_names_the_fix():
    from axiom.extensions.builtins.agents.cli import autonomy_status_lines

    lines = autonomy_status_lines(False)

    assert "Autonomy:" in lines[0] and "OFF" in lines[0]
    assert "nothing dispatches" in lines[0]
    joined = "\n".join(lines)
    assert "settings --global set autonomy.enabled true" in joined, (
        "the OFF line must name the exact command that turns it on"
    )
    assert "global" in joined.lower(), (
        "the fix is global, not project: the service runs with no working directory"
    )


def test_cmd_status_prints_the_autonomy_line(monkeypatch, capsys, tmp_path):
    """The helper is wired into the status screen, in both gate states."""
    from axiom.extensions.builtins.agents import cli as agents_cli

    class _Info:
        status = "running"
        provider = "launchd"

    class _Mgr:
        def status(self):
            return _Info()

    monkeypatch.setattr(agents_cli, "_surfaced_agent_extensions", lambda: [])

    # No agents -> early return before the header; give it one minimal agent.
    class _AgentCfg:
        heartbeat_interval = 60
        startup = "auto"
        watchers = ()
        is_registrable = True

    class _Ext:
        name = "tidy"
        agent = _AgentCfg()
        cli_commands = ()

    monkeypatch.setattr(agents_cli, "_surfaced_agent_extensions", lambda: [_Ext()])
    monkeypatch.setattr(agents_cli, "_make_background_service_manager", lambda: _Mgr())
    monkeypatch.setattr(agents_cli, "_bg_last_runs", lambda: {})

    for state, token in ((True, "ON"), (False, "OFF")):
        monkeypatch.setattr(agents_cli, "autonomy_enabled", lambda s=state: s)
        agents_cli._cmd_status(object())
        out = capsys.readouterr().out
        autonomy_lines = [ln for ln in out.splitlines() if "Autonomy:" in ln]
        assert autonomy_lines, f"status screen must state the master gate ({token})"
        assert token in autonomy_lines[0]
