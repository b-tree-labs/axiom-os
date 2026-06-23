# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-slot background service (axiom.agents.background_service).

Replaces the pre-0.11.1 per-agent launchd/systemd registration with a
single background-service entry per slot. Tests validate:
  - Last-run state persists atomically and survives corruption.
  - Due-agent dispatch fires exactly the agents whose interval elapsed.
  - One bad agent doesn't block the others.
  - Tick log is appended for observability.
  - The background-service main returns 0 even when no agents are due.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from axiom.agents.background_service import (
    StateStore,
    background_service_main,
    dispatch_due_agents,
    is_due,
)


@dataclass
class _FakeAgentConfig:
    heartbeat_interval: int
    heartbeat_command: str
    startup: str = "daemon"

    @property
    def is_always_on(self) -> bool:
        return self.startup in ("daemon", "eager")

    @property
    def is_registrable(self) -> bool:
        return self.is_always_on and bool(self.heartbeat_command.strip())


@dataclass
class _FakeExt:
    name: str
    agent: _FakeAgentConfig | None


# ---------------------------------------------------------------------------
# is_due
# ---------------------------------------------------------------------------


class TestIsDue:
    def test_zero_last_run_strict_compare(self):
        # is_due is a pure compare; first-run semantics are handled by
        # dispatch_due_agents via state.get / `name not in state` logic.
        assert is_due(0.0, 300, now=10.0) is False
        assert is_due(0.0, 300, now=400.0) is True

    def test_within_interval_not_due(self):
        assert is_due(100.0, 300, now=200.0) is False

    def test_exact_interval_is_due(self):
        assert is_due(100.0, 300, now=400.0) is True

    def test_well_past_interval(self):
        assert is_due(100.0, 300, now=10000.0) is True


# ---------------------------------------------------------------------------
# StateStore atomicity + corruption recovery
# ---------------------------------------------------------------------------


class TestStateStore:
    def test_load_missing_returns_empty_dict(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        assert store.load() == {}

    def test_save_then_load_roundtrips(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        store.save({"tidy": 1234.5, "rivet": 9876.5})
        assert store.load() == {"tidy": 1234.5, "rivet": 9876.5}

    def test_corrupt_state_returns_empty_dict(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("not-json{{{", encoding="utf-8")
        store = StateStore(path)
        assert store.load() == {}

    def test_save_is_atomic_write_then_rename(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        store.save({"tidy": 1.0})
        # No leftover .tmp file — atomic write completed
        assert not (tmp_path / "state.json.tmp").exists()


# ---------------------------------------------------------------------------
# dispatch_due_agents
# ---------------------------------------------------------------------------


def _make_ext(name: str, interval: int, command: str = ""):
    return _FakeExt(
        name=name,
        agent=_FakeAgentConfig(
            heartbeat_interval=interval,
            heartbeat_command=command or f"{name} heartbeat",
        ),
    )


class TestDispatchDueAgents:
    def test_empty_extensions(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            dispatched = dispatch_due_agents([], store, "axi", now=1000.0)
        assert dispatched == []
        assert run.call_count == 0

    def test_first_run_dispatches_all(self, tmp_path):
        exts = [_make_ext("tidy", 300), _make_ext("rivet", 300)]
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            run.return_value.returncode = 0
            dispatched = dispatch_due_agents(exts, store, "axi", now=1000.0)
        assert set(dispatched) == {"tidy", "rivet"}
        assert run.call_count == 2

    def test_within_interval_skips(self, tmp_path):
        exts = [_make_ext("tidy", 300)]
        store = StateStore(tmp_path / "state.json")
        store.save({"tidy": 1000.0})
        with patch("axiom.agents.background_service.subprocess.run") as run:
            dispatched = dispatch_due_agents(exts, store, "axi", now=1100.0)
        assert dispatched == []
        assert run.call_count == 0

    def test_due_dispatches_and_persists(self, tmp_path):
        exts = [_make_ext("tidy", 300)]
        store = StateStore(tmp_path / "state.json")
        store.save({"tidy": 1000.0})
        with patch("axiom.agents.background_service.subprocess.run") as run:
            run.return_value.returncode = 0
            dispatched = dispatch_due_agents(exts, store, "axi", now=1500.0)
        assert dispatched == ["tidy"]
        assert store.load() == {"tidy": 1500.0}

    def test_failing_subprocess_does_not_block_others(self, tmp_path):
        exts = [_make_ext("tidy", 300), _make_ext("rivet", 300)]
        store = StateStore(tmp_path / "state.json")

        def fake_run(cmd, **kwargs):
            if cmd[1] == "tidy":
                raise OSError("tidy binary not found")

            class R:
                returncode = 0

            return R()

        with patch("axiom.agents.background_service.subprocess.run", side_effect=fake_run):
            dispatched = dispatch_due_agents(exts, store, "axi", now=1000.0)

        # tidy's exception is swallowed; rivet still ran
        assert "rivet" in dispatched
        assert "tidy" not in dispatched
        # tidy's last_run NOT updated (so retry on next tick)
        assert "tidy" not in store.load()
        # rivet's last_run IS updated
        assert "rivet" in store.load()

    def test_command_argv_includes_cli_binary_first(self, tmp_path):
        exts = [_make_ext("tidy", 300, command="tidy health --json")]
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            run.return_value.returncode = 0
            dispatch_due_agents(exts, store, "axi", now=1000.0)

        argv = run.call_args[0][0]
        assert argv == ["axi", "tidy", "health", "--json"]

    def test_extensions_without_heartbeat_command_skipped(self, tmp_path):
        # An extension with [agent] but no heartbeat_command should be filtered
        ext = _FakeExt(
            name="lazy-agent",
            agent=_FakeAgentConfig(
                heartbeat_interval=300, heartbeat_command="", startup="daemon"
            ),
        )
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            dispatched = dispatch_due_agents([ext], store, "axi", now=1000.0)
        assert dispatched == []
        assert run.call_count == 0


class TestDispatchConsentFilter:
    """À-la-carte consent: dispatch only the agents the operator approved."""

    def test_enabled_subset_dispatches_only_approved(self, tmp_path):
        exts = [_make_ext("tidy", 300), _make_ext("rivet", 300)]
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            run.return_value.returncode = 0
            dispatched = dispatch_due_agents(
                exts, store, "axi", now=1000.0, enabled={"tidy"}
            )
        assert dispatched == ["tidy"]
        assert run.call_count == 1

    def test_enabled_none_dispatches_all(self, tmp_path):
        # None == no recorded à-la-carte choice (pre-consent install): keep
        # dispatching everything so an upgrade never silently neuters agents.
        exts = [_make_ext("tidy", 300), _make_ext("rivet", 300)]
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            run.return_value.returncode = 0
            dispatched = dispatch_due_agents(
                exts, store, "axi", now=1000.0, enabled=None
            )
        assert set(dispatched) == {"tidy", "rivet"}

    def test_empty_enabled_set_dispatches_nothing(self, tmp_path):
        # Empty set == decided-but-approved-none (opted out): dispatch nothing.
        exts = [_make_ext("tidy", 300)]
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            dispatched = dispatch_due_agents(
                exts, store, "axi", now=1000.0, enabled=set()
            )
        assert dispatched == []
        assert run.call_count == 0

    def test_main_passes_opted_out_as_empty_set(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.agents.consent import AgentConsent

        monkeypatch.setattr(
            "axiom.agents.background_service.get_user_state_dir", lambda: tmp_path
        )
        monkeypatch.setattr(
            "axiom.agents.background_service._discover_daemon_extensions",
            lambda: [_make_ext("tidy", 300)],
        )
        monkeypatch.setattr(
            "axiom.agents.background_service.load_consent",
            lambda: AgentConsent(decided=True, opted_out=True),
        )
        with patch("axiom.agents.background_service.subprocess.run") as run:
            rc = background_service_main([])
        assert rc == 0
        assert run.call_count == 0  # opted out -> nothing dispatched


# ---------------------------------------------------------------------------
# background_service_main — the console entry point
# ---------------------------------------------------------------------------


class TestCoordinatorMain:
    def test_no_due_agents_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.agents.background_service.get_user_state_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "axiom.agents.background_service._discover_daemon_extensions",
            lambda: [],
        )
        rc = background_service_main([])
        assert rc == 0

    def test_writes_tick_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.agents.background_service.get_user_state_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "axiom.agents.background_service._discover_daemon_extensions",
            lambda: [_make_ext("tidy", 300)],
        )
        # Hermetic: don't read the developer's real à-la-carte consent file
        # (which may have opted out of, or not enabled, tidy → dispatched==[]).
        # Undecided consent is the post-install default: dispatch all.
        monkeypatch.setattr(
            "axiom.agents.background_service.load_consent",
            lambda: SimpleNamespace(opted_out=False, decided=False, enabled=()),
        )
        with patch("axiom.agents.background_service.subprocess.run") as run:
            run.return_value.returncode = 0
            rc = background_service_main([])
        assert rc == 0
        log = tmp_path / "agents" / ".background-service" / "ticks.jsonl"
        assert log.exists()
        entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        assert entries[0]["agent_count"] == 1
        assert entries[0]["dispatched"] == ["tidy"]

    def test_discovery_crash_returns_2(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.agents.background_service.get_user_state_dir",
            lambda: tmp_path,
        )

        def bad_discover():
            raise RuntimeError("discovery exploded")

        monkeypatch.setattr(
            "axiom.agents.background_service._discover_daemon_extensions", bad_discover
        )
        rc = background_service_main([])
        assert rc == 2
        log = tmp_path / "agents" / ".background-service" / "ticks.jsonl"
        assert log.exists()
        entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert "error" in entries[-1]
