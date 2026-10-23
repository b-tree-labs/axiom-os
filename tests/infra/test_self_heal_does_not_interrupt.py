# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A host-modifying question must not interrupt an unrelated command.

Reported from live use. Running a data verb produced this, mid-command:

      6 agent(s) aren't registered as background services yet: diagnostics,
      directory, hygiene, publishing, release, vault
      Registering installs an OS task so their heartbeats survive reboots —
      this modifies your host.
      Set them up now? [y]es (choose which) / [N]o (don't ask again) / [l]ater:

Three things are wrong with that, and they compound.

**It blocks.** The function's own docstring says "never blocks the CLI"
while calling ``input()``. A script, a pipe, or somebody in a hurry stops
dead on a question they did not ask for.

**It interrupts something else.** The person typed a command about their
data. Consent to modify their host is a real question, but asking it here
means it arrives when they have the least context and the least patience —
and "[N]o (don't ask again)" is the cheapest key to press, so the
interruption actively buys a decision it should not be buying.

**It asks the wrong installs.** This fires in a throwaway venv pointed at a
scratch state directory — an install that will be deleted in an hour. There
is no host to keep an OS task alive on, and offering one is noise.

What stays: a consented install still self-heals silently, because that is
repair rather than a question.
"""

from __future__ import annotations

import pytest

from axiom import axiom_cli


@pytest.fixture
def interactive(monkeypatch, capsys):
    """Make the tty checks pass.

    Patches the named seam rather than sys.stdin/sys.stdout: pytest
    replaces both, and a patch applied to them lands on objects the code
    never sees — so the function returns early for a reason that has
    nothing to do with what is being tested.
    """
    monkeypatch.setattr(axiom_cli, "_is_interactive", lambda: True)
    monkeypatch.delenv("AXIOM_DISABLE_SELF_HEAL", raising=False)


@pytest.fixture
def home_state(tmp_path, monkeypatch):
    """A normal install: state in the default place."""
    monkeypatch.delenv("AXI_STATE_DIR", raising=False)
    monkeypatch.setattr(
        "axiom.infra.paths.get_user_state_dir", lambda: tmp_path / "home-state"
    )
    (tmp_path / "home-state").mkdir(parents=True, exist_ok=True)
    return tmp_path / "home-state"


@pytest.fixture
def undecided(monkeypatch):
    """A fresh install that has never been asked.

    Patched rather than inherited: load_consent() reads the developer's own
    ~/.axi, so without this the tests exercise whatever THIS machine
    happens to have consented to — which is how a test passes for a reason
    that has nothing to do with the code.
    """
    class _C:
        decided = False
        opted_out = False
        enabled: list[str] = []
        decided_version = ""

    monkeypatch.setattr(
        "axiom.extensions.builtins.agents.consent.load_consent", lambda: _C()
    )
    monkeypatch.setattr(
        "axiom.extensions.builtins.agents.consent.needs_prompt", lambda c, m: True
    )
    monkeypatch.setattr(
        "axiom.extensions.builtins.agents.consent.should_reoffer_after_optout",
        lambda c, v: False,
    )
    # Registration must never actually run from a test — it installs an OS
    # task on the machine running the suite.
    monkeypatch.setattr(
        "axiom.extensions.builtins.agents.cli.register_all_daemon_agents",
        lambda: (_ for _ in ()).throw(
            AssertionError("a test tried to register OS services")
        ),
    )
    return _C


@pytest.fixture
def no_input(monkeypatch):
    """Any call to input() is the bug, so make it loud."""
    def boom(*a, **k):
        raise AssertionError(
            "self-heal called input() — it blocks a command the user ran for "
            "something else"
        )
    monkeypatch.setattr("builtins.input", boom)
    return boom


class TestItNeverBlocks:
    def test_an_unregistered_install_does_not_prompt(
        self, interactive, home_state, undecided, no_input, monkeypatch, capsys
    ):
        """The bug, stated as a test."""
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.missing_daemon_agents",
            lambda: ["diagnostics", "vault"],
        )
        axiom_cli._self_heal_daemon_agents()

    def test_it_still_says_something_once(
        self, interactive, home_state, undecided, no_input, monkeypatch, capsys
    ):
        """Silence would be worse than a prompt — they would never learn the
        agents are not running. A line they can ignore is the middle."""
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.missing_daemon_agents",
            lambda: ["diagnostics", "vault"],
        )
        axiom_cli._self_heal_daemon_agents()
        out = capsys.readouterr().out
        assert "agents register" in out, out
        assert out.count("\n") <= 4, f"a notice, not a paragraph:\n{out}"

    def test_the_notice_says_what_kind_of_thing_an_agent_is(
        self, interactive, home_state, undecided, no_input, monkeypatch, capsys
    ):
        """A colleague met this during onboarding on 2026-10-01 having never run
        a background agent, and could not decide. "diagnostics, vault" names two
        things and says nothing about what either would do on his machine.

        The category rides inside the line that was already there rather than
        arriving as a line per agent, because of the budget asserted above. Both
        halves are asserted so the next person does not resolve the tension by
        re-breaking one of them: the detail belongs in `agents register`, which
        asks, and this only reports.
        """
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.missing_daemon_agents",
            lambda: ["diagnostics", "vault"],
        )
        axiom_cli._self_heal_daemon_agents()
        out = capsys.readouterr().out
        assert "background agent" in out, out
        assert "upkeep" in out, f"nothing says what these would do:\n{out}"
        assert "later" in out, f"the answer somebody new wants is not mentioned:\n{out}"
        assert out.count("\n") <= 4, f"a notice, not a paragraph:\n{out}"

    def test_the_notice_does_not_recur_every_command(
        self, interactive, home_state, undecided, no_input, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.missing_daemon_agents",
            lambda: ["diagnostics"],
        )
        axiom_cli._self_heal_daemon_agents()
        capsys.readouterr()
        axiom_cli._self_heal_daemon_agents()
        assert capsys.readouterr().out == "", "it nagged twice in a row"


class TestItDoesNotOfferToModifyAHostThatIsNotOne:
    def test_a_redirected_state_dir_is_left_alone(
        self, interactive, undecided, no_input, monkeypatch, tmp_path, capsys
    ):
        """AXI_STATE_DIR set means a throwaway, a CI job or a review env.
        There is no host to keep an OS task alive on."""
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "scratch"))
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.missing_daemon_agents",
            lambda: ["diagnostics", "vault"],
        )
        axiom_cli._self_heal_daemon_agents()
        assert capsys.readouterr().out == ""


class TestRepairIsNotAQuestion:
    def test_a_consented_install_still_self_heals_silently(
        self, interactive, home_state, no_input, monkeypatch, capsys
    ):
        healed = []
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.missing_daemon_agents",
            lambda: ["diagnostics"],
        )
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.cli.register_all_daemon_agents",
            lambda: healed.append(True),
        )
        monkeypatch.setattr(
            "axiom.extensions.builtins.agents.consent.load_consent",
            lambda: type("C", (), {"decided": True, "opted_out": False})(),
        )
        axiom_cli._self_heal_daemon_agents()
        assert healed == [True], "a consented install stopped repairing itself"


class TestTheDocstringIsTrue:
    def test_it_claims_not_to_block_and_does_not(self):
        """The docstring said 'never blocks the CLI' while calling input().
        A comment that contradicts the code is worse than no comment."""
        import inspect

        source = inspect.getsource(axiom_cli._self_heal_daemon_agents)
        # The docstring now explains the history, so look for a CALL rather
        # than the word.
        import re

        assert not re.search(r"^\s*(ans\s*=\s*)?input\(", source, re.M), (
            "self-heal calls input() again; the docstring's promise is the "
            "one that has to hold"
        )
