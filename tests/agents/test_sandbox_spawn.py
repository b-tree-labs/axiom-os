# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Each agent's heartbeat runs in its OWN sandbox (ADR-036 §D10).

Before this, every agent was dispatched with `subprocess.run` from the one
Background Service, so all of them shared the dispatcher's unit. A manifest
that declared `[agent.sandbox]` got either nothing or, if the declaration had
been applied to the dispatcher, a sandbox widened for every other agent on the
slot. Per-agent granularity is what D10 assumes; a transient unit per dispatch
is how it is restored without giving back the single OS-level entry.
"""

from __future__ import annotations

import pytest

from axiom.agents.sandbox_spawn import (
    APPLIED,
    NO_SYSTEMD_RUN,
    NO_USER_MANAGER,
    NOT_LINUX,
    sandbox_command,
    sandbox_state,
)
from axiom.extensions.contracts import SandboxConfig

CMD = ["axi", "rivet", "heartbeat"]
LINUX = {
    "platform": "linux",
    "which": lambda n: "/usr/bin/" + n,
    "env": {"XDG_RUNTIME_DIR": "/run/user/1000"},
}


class TestWhenItCanWrap:
    def test_a_linux_host_with_a_user_manager_wraps(self):
        argv, state = sandbox_command(CMD, None, **LINUX)
        assert state == APPLIED
        assert argv[0] == "systemd-run"
        assert "--user" in argv and "--wait" in argv and "--collect" in argv

    def test_the_agent_command_survives_intact_after_the_separator(self):
        argv, _ = sandbox_command(CMD, None, **LINUX)
        assert argv[argv.index("--") + 1 :] == CMD

    def test_the_hardened_defaults_travel_as_properties(self):
        argv, _ = sandbox_command(CMD, None, **LINUX)
        props = {a.split("=", 1)[1].split("=", 1)[0] for a in argv if a.startswith("--property=")}
        assert {
            "NoNewPrivileges",
            "PrivateTmp",
            "ProtectSystem",
            "ProtectHome",
            "ReadWritePaths",
            "RestrictNamespaces",
            "MemoryDenyWriteExecute",
        } <= props

    def test_this_agents_relaxation_reaches_this_agents_unit(self):
        """The whole point: the declaration lands on THIS dispatch."""
        argv, _ = sandbox_command(
            CMD, SandboxConfig(protect_home="tmpfs", read_write_paths=["/var/tmp/agent"]), **LINUX
        )
        joined = " ".join(argv)
        assert "--property=ProtectHome=tmpfs" in joined
        assert "/var/tmp/agent" in joined

    def test_one_agents_relaxation_does_not_reach_another(self):
        """The defect this replaces: a shared unit meant one manifest set the
        sandbox for everybody. Two dispatches, two different sandboxes."""
        relaxed, _ = sandbox_command(CMD, SandboxConfig(protect_home="off"), **LINUX)
        default, _ = sandbox_command(CMD, None, **LINUX)
        assert "--property=ProtectHome=off" in relaxed
        assert "--property=ProtectHome=off" not in default
        assert "--property=ProtectHome=read-only" in default

    def test_the_description_names_the_agent(self):
        argv, _ = sandbox_command(CMD, None, description="Axiom agent heartbeat: rivet", **LINUX)
        assert "--description=Axiom agent heartbeat: rivet" in argv

    def test_no_stable_unit_name_is_pinned(self):
        """A fixed --unit collides with a dispatch that has not finished,
        turning a slow heartbeat into a failed one."""
        argv, _ = sandbox_command(CMD, None, **LINUX)
        assert not any(a.startswith("--unit=") for a in argv)


class TestWhenItCannot:
    """Absence has kinds. An agent running unsandboxed because the host has
    no systemd is a different fact from one running unsandboxed by
    declaration, and the caller must be able to tell them apart."""

    def test_macos_is_named_not_guessed(self):
        argv, state = sandbox_command(
            CMD, None, platform="darwin", which=lambda n: "/usr/bin/" + n, env={}
        )
        assert state == NOT_LINUX
        assert argv == CMD

    def test_a_linux_host_without_systemd_run_says_so(self):
        argv, state = sandbox_command(
            CMD, None, platform="linux", which=lambda n: None, env={"XDG_RUNTIME_DIR": "/x"}
        )
        assert state == NO_SYSTEMD_RUN
        assert argv == CMD

    def test_a_container_without_a_user_manager_says_so(self):
        argv, state = sandbox_command(
            CMD, None, platform="linux", which=lambda n: "/usr/bin/" + n, env={}
        )
        assert state == NO_USER_MANAGER
        assert argv == CMD

    def test_the_command_is_never_refused_for_lack_of_a_sandbox(self):
        """Refusing would make hardening an outage on every macOS laptop."""
        for kwargs in (
            {"platform": "darwin", "which": lambda n: None, "env": {}},
            {"platform": "linux", "which": lambda n: None, "env": {}},
        ):
            argv, _ = sandbox_command(CMD, None, **kwargs)
            assert argv == CMD

    def test_state_can_be_asked_without_a_command(self):
        assert sandbox_state(**LINUX) == APPLIED
        assert sandbox_state(platform="darwin", which=lambda n: None, env={}) == NOT_LINUX


class TestAnInvalidDeclarationIsRefused:
    """Unlike a missing sandbox, a WRONG one is not tolerated: a manifest
    claiming a sandbox it never gets is exactly what D10 exists to prevent."""

    def test_an_unknown_protect_home_raises(self):
        with pytest.raises(ValueError, match="protect_home"):
            sandbox_command(CMD, SandboxConfig(protect_home="sideways"), **LINUX)

    def test_a_relaxation_wide_enough_to_be_a_removal_raises(self):
        with pytest.raises(ValueError, match="too broad"):
            sandbox_command(CMD, SandboxConfig(read_write_paths=["/"]), **LINUX)

    def test_it_is_refused_on_a_host_that_could_not_apply_it_anyway(self):
        """The check belongs to the MANIFEST, not the machine.

        Validating only where the sandbox can be applied is how a broken
        `[agent.sandbox]` sails through every developer's Mac and fails first
        on a Linux node, which is the furthest possible place from the person
        who wrote it. This test failed when the early return for a
        non-applying host sat in front of the validation.
        """
        for kwargs in (
            {"platform": "darwin", "which": lambda n: None, "env": {}},
            {"platform": "linux", "which": lambda n: None, "env": {}},
            {"platform": "win32", "which": lambda n: None, "env": {}},
        ):
            with pytest.raises(ValueError):
                sandbox_command(CMD, SandboxConfig(protect_home="sideways"), **kwargs)
