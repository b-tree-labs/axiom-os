# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`axi agents status` says what sandbox each agent actually gets.

ADR-036 §D10 closes with a requirement, not a nicety: "Each relaxation is
logged at install time and surfaced in `axi agents status`. Extensions cannot
escape the sandbox via runtime configuration — only via manifest declaration
that the operator can audit before install." An audit trail nobody can read is
not an audit trail, and a relaxation that only appears in a TOML file nobody
opens is exactly the runtime escape the decision meant to prevent.

The second thing this surface must not do is let a reader assume the
relaxations listed are in force. On a host that cannot sandbox at all, every
agent is unconfined and the declarations are inert — printing them without
saying so is worse than printing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from axiom.agents.sandbox_spawn import APPLIED, NO_SYSTEMD_RUN, NOT_LINUX
from axiom.extensions.builtins.agents.cli import sandbox_status_lines
from axiom.extensions.contracts import SandboxConfig


@dataclass
class _Agent:
    sandbox: object | None = None


@dataclass
class _Ext:
    name: str
    agent: object


def _text(*args, **kwargs) -> str:
    return "\n".join(sandbox_status_lines(*args, **kwargs))


class TestWhatIsDeclared:
    def test_an_agent_with_no_declaration_is_not_listed_as_relaxed(self):
        out = _text([_Ext("rivet", _Agent())], host_state=APPLIED)
        assert "rivet" not in out

    def test_the_hardened_default_is_stated_rather_than_left_blank(self):
        """Silence would read as "no sandbox" as easily as "nothing relaxed"."""
        out = _text([_Ext("rivet", _Agent())], host_state=APPLIED)
        assert "hardened" in out.lower()

    def test_a_relaxed_home_is_named_with_the_agent_that_declared_it(self):
        out = _text(
            [_Ext("rivet", _Agent(SandboxConfig(protect_home="tmpfs")))], host_state=APPLIED
        )
        assert "rivet" in out
        assert "tmpfs" in out

    def test_extra_writable_paths_are_shown_verbatim(self):
        """The operator is auditing the path, so an elided one is useless."""
        out = _text(
            [_Ext("mo", _Agent(SandboxConfig(read_write_paths=["/var/lib/mo", "~/.config/gh"])))],
            host_state=APPLIED,
        )
        assert "/var/lib/mo" in out and "~/.config/gh" in out

    def test_only_the_declaring_agent_is_named(self):
        out = _text(
            [
                _Ext("loose", _Agent(SandboxConfig(protect_home="off"))),
                _Ext("tight", _Agent()),
            ],
            host_state=APPLIED,
        )
        relaxed = [line for line in out.splitlines() if "off" in line]
        assert relaxed and all("tight" not in line for line in relaxed)


class TestWhetherItIsInForce:
    def test_a_host_that_cannot_sandbox_says_so(self):
        out = _text([_Ext("rivet", _Agent())], host_state=NOT_LINUX)
        assert "not" in out.lower()

    def test_declarations_are_marked_inert_when_the_host_cannot_apply_them(self):
        """The failure this prevents: reading a relaxation list as a
        description of what is in force, on a host where nothing is."""
        out = _text(
            [_Ext("rivet", _Agent(SandboxConfig(protect_home="off")))], host_state=NO_SYSTEMD_RUN
        )
        assert "rivet" in out
        low = out.lower()
        assert "not applied" in low or "inert" in low or "unconfined" in low

    def test_the_reason_is_named_not_just_the_outcome(self):
        """"No sandbox" is a conclusion; which absence it is, is actionable."""
        assert "systemd-run" in _text([], host_state=NO_SYSTEMD_RUN)

    def test_an_applying_host_does_not_warn(self):
        out = _text([_Ext("rivet", _Agent())], host_state=APPLIED).lower()
        assert "not applied" not in out and "unconfined" not in out


class TestItNeverBreaksTheStatusCommand:
    def test_an_agent_without_the_field_is_tolerated(self):
        out = _text([_Ext("legacy", object())], host_state=APPLIED)
        assert isinstance(out, str)

    def test_no_agents_at_all_is_fine(self):
        assert isinstance(sandbox_status_lines([], host_state=APPLIED), list)

    def test_an_agentless_extension_is_skipped(self):
        assert isinstance(_text([_Ext("x", None)], host_state=APPLIED), str)
