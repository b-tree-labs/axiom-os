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
from unittest.mock import MagicMock, patch

import pytest

from axiom.agents.background_service import (
    StateStore,
    background_service_main,
    dispatch_due_agents,
    is_due,
)


# --- the sandbox seam, pinned ------------------------------------------------
#
# `dispatch_due_agents` wraps each heartbeat in a transient systemd unit when
# the host can (ADR-036 D10). That rewrites argv, so every test here that
# asserts on the command — or whose fake keys on `cmd[1]` — silently asserted
# "this machine is not Linux" instead of what it meant to assert. Two of them
# passed on macOS and failed on CI for exactly that reason.
#
# These tests are about dispatch, consent and budget. The sandbox has its own
# tests. Pinning the seam to a pass-through makes them say the same thing on
# every platform; the tests that are about sandboxing opt out by patching
# `sandbox_command` themselves, which takes precedence over this fixture.


@pytest.fixture(autouse=True)
def _unsandboxed(monkeypatch):
    monkeypatch.setattr(
        "axiom.agents.background_service.sandbox_command",
        lambda cmd, sandbox, **kw: (list(cmd), "test:pinned"),
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
            agent=_FakeAgentConfig(heartbeat_interval=300, heartbeat_command="", startup="daemon"),
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
            dispatched = dispatch_due_agents(exts, store, "axi", now=1000.0, enabled={"tidy"})
        assert dispatched == ["tidy"]
        assert run.call_count == 1

    def test_enabled_none_dispatches_all(self, tmp_path):
        # None == no recorded à-la-carte choice (pre-consent install): keep
        # dispatching everything so an upgrade never silently neuters agents.
        exts = [_make_ext("tidy", 300), _make_ext("rivet", 300)]
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            run.return_value.returncode = 0
            dispatched = dispatch_due_agents(exts, store, "axi", now=1000.0, enabled=None)
        assert set(dispatched) == {"tidy", "rivet"}

    def test_empty_enabled_set_dispatches_nothing(self, tmp_path):
        # Empty set == decided-but-approved-none (opted out): dispatch nothing.
        exts = [_make_ext("tidy", 300)]
        store = StateStore(tmp_path / "state.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            dispatched = dispatch_due_agents(exts, store, "axi", now=1000.0, enabled=set())
        assert dispatched == []
        assert run.call_count == 0

    def test_main_passes_opted_out_as_empty_set(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.agents.consent import AgentConsent

        # Autonomy must be ON for consent handling to be exercised at all
        # (the master gate short-circuits before consent when OFF).
        monkeypatch.setattr("axiom.agents.background_service.autonomy_enabled", lambda: True)
        monkeypatch.setattr("axiom.agents.background_service.get_user_state_dir", lambda: tmp_path)
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
    @pytest.fixture(autouse=True)
    def _autonomy_on(self, monkeypatch):
        # These tests exercise the on-path entry point (dispatch/discovery).
        # The master autonomy gate defaults OFF, so turn it on here; the OFF
        # short-circuit is covered in test_autonomy_gate.py.
        monkeypatch.setattr("axiom.agents.background_service.autonomy_enabled", lambda: True)

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


# ---------------------------------------------------------------------------
# per-agent sandbox (ADR-036 §D10)
# ---------------------------------------------------------------------------


@dataclass
class _SandboxedAgentConfig(_FakeAgentConfig):
    """An agent config that carries a sandbox declaration, as a real one does."""

    sandbox: object | None = None


class TestEachDispatchGetsItsOwnSandbox:
    """The dispatcher used to run every heartbeat under ITS unit, so a
    `[agent.sandbox]` relaxation was either inert or applied to all agents.
    Each dispatch now carries the declaration of the agent it is dispatching.
    """

    def _ext(self, name, sandbox=None):
        return _FakeExt(
            name=name,
            agent=_SandboxedAgentConfig(
                heartbeat_interval=60, heartbeat_command=f"{name} heartbeat", sandbox=sandbox
            ),
        )

    def test_the_agents_declaration_reaches_the_argv(self, tmp_path):
        from axiom.extensions.contracts import SandboxConfig

        store = StateStore(tmp_path / "s.json")
        seen = []
        with patch(
            "axiom.agents.background_service.sandbox_command",
            side_effect=lambda cmd, sb, **kw: seen.append((cmd, sb)) or (cmd, "systemd-transient"),
        ):
            with patch("subprocess.run") as run:
                run.return_value = MagicMock(returncode=0)
                dispatch_due_agents(
                    [self._ext("rivet", SandboxConfig(protect_home="tmpfs"))],
                    store,
                    "axi",
                    now=1000.0,
                )
        assert len(seen) == 1
        assert seen[0][1].protect_home == "tmpfs"

    def test_two_agents_are_wrapped_separately(self, tmp_path):
        from axiom.extensions.contracts import SandboxConfig

        store = StateStore(tmp_path / "s.json")
        seen = []
        with patch(
            "axiom.agents.background_service.sandbox_command",
            side_effect=lambda cmd, sb, **kw: seen.append(sb) or (cmd, "systemd-transient"),
        ):
            with patch("subprocess.run") as run:
                run.return_value = MagicMock(returncode=0)
                dispatch_due_agents(
                    [self._ext("a", SandboxConfig(protect_home="off")), self._ext("b")],
                    store,
                    "axi",
                    now=1000.0,
                )
        assert len(seen) == 2
        assert {getattr(s, "protect_home", None) for s in seen} == {"off", None}

    def test_an_agent_config_without_the_field_still_dispatches(self, tmp_path, monkeypatch):
        """A config that predates `sandbox` degrades to the hardened default
        rather than crashing the whole tick for every other agent.

        Deliberately runs against the REAL `sandbox_command`, undoing the
        module's pin: the thing under test is that reading an absent attribute
        does not raise, and a stubbed seam would never read it.
        """
        from axiom.agents.sandbox_spawn import sandbox_command as real

        monkeypatch.setattr("axiom.agents.background_service.sandbox_command", real)
        store = StateStore(tmp_path / "s.json")
        ext = _FakeExt(
            name="legacy",
            agent=_FakeAgentConfig(heartbeat_interval=60, heartbeat_command="legacy heartbeat"),
        )
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            assert dispatch_due_agents([ext], store, "axi", now=1000.0) == ["legacy"]

    def test_an_invalid_declaration_skips_that_agent_and_not_the_others(self, tmp_path, monkeypatch):
        """A manifest claiming a sandbox it cannot get is refused — running it
        wide open is the failure D10 exists to prevent. The refusal is scoped
        to that agent: one bad manifest must not stop the slot."""
        # The real resolver, not the module pin: refusing an invalid
        # declaration is exactly what a stubbed seam cannot do.
        from axiom.agents.sandbox_spawn import sandbox_command as real

        monkeypatch.setattr("axiom.agents.background_service.sandbox_command", real)
        from axiom.extensions.contracts import SandboxConfig

        store = StateStore(tmp_path / "s.json")
        bad = self._ext("bad", SandboxConfig(read_write_paths=["/"]))
        good = self._ext("good")
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            dispatched = dispatch_due_agents([bad, good], store, "axi", now=1000.0)
        assert dispatched == ["good"]

    def test_a_refused_agent_is_not_recorded_as_having_run(self, tmp_path, monkeypatch):
        """Recording it would make the next tick think it had a turn, so a
        broken manifest would look like a quiet agent instead of a refused one."""
        # The real resolver, not the module pin: refusing an invalid
        # declaration is exactly what a stubbed seam cannot do.
        from axiom.agents.sandbox_spawn import sandbox_command as real

        monkeypatch.setattr("axiom.agents.background_service.sandbox_command", real)
        from axiom.extensions.contracts import SandboxConfig

        store = StateStore(tmp_path / "s.json")
        bad = self._ext("bad", SandboxConfig(protect_home="sideways"))
        with patch("subprocess.run"):
            dispatch_due_agents([bad], store, "axi", now=1000.0)
        assert "bad" not in store.load()

    def test_on_a_linux_host_the_dispatched_argv_is_wrapped(self, tmp_path, monkeypatch):
        """The regression this file earned on 2026-10-01.

        `dispatch_due_agents` wraps each heartbeat in a transient unit where
        the host can. Two tests here asserted the UNWRAPPED argv — one by
        comparing it outright, one by keying its fake on `cmd[1]` — so both
        passed on macOS, where no wrapping happens, and failed on CI, where it
        does. Nothing a developer could run on a Mac would have caught it.

        So the wrapping is asserted against a SIMULATED Linux host rather than
        against whichever machine runs the suite, and the agent's own command
        is checked to survive intact after the `--` separator.
        """
        from axiom.agents.sandbox_spawn import sandbox_command as real

        monkeypatch.setattr(
            "axiom.agents.background_service.sandbox_command",
            lambda cmd, sandbox, **kw: real(
                cmd,
                sandbox,
                platform="linux",
                which=lambda n: "/usr/bin/" + n,
                env={"XDG_RUNTIME_DIR": "/run/user/1000"},
                **kw,
            ),
        )
        store = StateStore(tmp_path / "s.json")
        with patch("axiom.agents.background_service.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            dispatch_due_agents(
                [_make_ext("tidy", 300, command="tidy health --json")], store, "axi", now=1000.0
            )
        argv = run.call_args[0][0]
        assert argv[0] == "systemd-run"
        assert argv[argv.index("--") + 1 :] == ["axi", "tidy", "health", "--json"]
