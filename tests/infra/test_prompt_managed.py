# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A prompt can be edited without a deploy, and still never be owned elsewhere.

The repository holds the text that ships. A managed copy exists so a wording
change can be tried against real traffic and so a recorded result can say which
wording was live. Everything here defends the line between those two: the
console may influence a turn only when someone deliberately switched that on,
and it may never break one.
"""

from __future__ import annotations

import logging

from axiom.infra import prompt_managed as pm

SHIPPED = "You are careful and cite sources."


class _Console:
    """Stands in for the prompt backend."""

    def __init__(self, text: str | None = "managed text", boom: bool = False):
        self._text = text
        self._boom = boom
        self.created: list[tuple] = []
        self.asked: list[str] = []

    def get_prompt(self, name):
        self.asked.append(name)
        if self._boom:
            raise RuntimeError("console is down")
        if self._text is None:
            return None
        return type("P", (), {"text": self._text})()

    def create_prompt(self, *, name, prompt, labels):
        if self._boom:
            raise RuntimeError("console is down")
        self.created.append((name, prompt, tuple(labels)))


_ON = {"AXIOM_MANAGED_PROMPTS_LIVE": "1"}
_OFF: dict[str, str] = {}


# --- off by default, and free -----------------------------------------------

def test_disabled_by_default_and_does_no_io():
    """A capability used during an experiment must not tax every other turn."""
    console = _Console()
    assert pm.get_managed_prompt("persona", SHIPPED, client=console, env=_OFF) == SHIPPED
    assert console.asked == [], "it reached the network while switched off"


def test_tracing_being_configured_does_not_turn_it_on():
    """Plenty of deployments want traces and not remote prompt edits."""
    assert not pm.live_fetch_enabled({"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})


def test_the_switch_is_explicit():
    for value in ("1", "true", "yes", "on", "TRUE"):
        assert pm.live_fetch_enabled({"AXIOM_MANAGED_PROMPTS_LIVE": value})
    for value in ("0", "false", "", "maybe"):
        assert not pm.live_fetch_enabled({"AXIOM_MANAGED_PROMPTS_LIVE": value})


# --- when on, the shipped text is still never lost --------------------------

def test_a_managed_prompt_overrides_when_live():
    got = pm.get_managed_prompt("persona", SHIPPED, client=_Console("edited"), env=_ON)
    assert got == "edited"


def test_an_unreachable_console_cannot_change_a_turn(caplog):
    """It may slow a turn. It may never alter or fail one."""
    with caplog.at_level(logging.WARNING, logger="axiom.infra.prompt_managed"):
        got = pm.get_managed_prompt(
            "persona", SHIPPED, client=_Console(boom=True), env=_ON
        )
    assert got == SHIPPED
    assert any("unaffected" in r.message for r in caplog.records)


def test_a_missing_managed_prompt_falls_back():
    got = pm.get_managed_prompt("persona", SHIPPED, client=_Console(None), env=_ON)
    assert got == SHIPPED


def test_an_empty_managed_prompt_is_treated_as_a_mistake(caplog):
    """Far likelier a console slip than an instruction to say nothing."""
    with caplog.at_level(logging.WARNING, logger="axiom.infra.prompt_managed"):
        got = pm.get_managed_prompt("persona", SHIPPED, client=_Console(""), env=_ON)
    assert got == SHIPPED
    assert any("no text" in r.message for r in caplog.records)


def test_no_client_means_the_shipped_text():
    assert pm.get_managed_prompt("persona", SHIPPED, client=None, env=_ON) == SHIPPED


# --- registration publishes, it does not pull -------------------------------

def test_registration_pushes_the_shipped_text():
    """A prompt the console has never seen cannot be compared or rolled back."""
    console = _Console()
    names = pm.register_prompts({"persona": SHIPPED, "safety": "Refuse unsafe."},
                                client=console)
    assert names == ["axiom/persona", "axiom/safety"]
    assert console.created[0][1] == SHIPPED


def test_registration_failure_does_not_fail_a_release(caplog):
    """This runs post-deploy; a briefly unreachable backend is not a bad release."""
    with caplog.at_level(logging.WARNING, logger="axiom.infra.prompt_managed"):
        names = pm.register_prompts({"persona": SHIPPED}, client=_Console(boom=True))
    assert names == []
    assert any("could not register" in r.message for r in caplog.records)


def test_names_group_under_one_prefix():
    assert pm.qualified_name("persona") == "axiom/persona"
    assert pm.qualified_name("axiom/persona") == "axiom/persona"


# --- the cascade position ---------------------------------------------------

def test_a_local_override_still_wins_over_a_managed_prompt():
    """The person at the machine beats the person in the console — they are the
    one who can see what broke.

    Run through the real composer and the real override path, because the
    cascade position is the claim and a hand-built list would not test it.
    """
    from axiom.infra.prompt_composer import PromptComposer
    from axiom.infra.prompt_overrides import apply_overrides

    managed = pm.get_managed_prompt(
        "persona", SHIPPED, client=_Console("from console"), env=_ON
    )
    assert managed == "from console", "the managed copy did not take effect"

    composer = PromptComposer()
    composer.add("identity", name="persona", content=managed, source="managed")

    apply_overrides(composer, {"persona": "from the operator"})

    live = {c.name: c.content for c in composer.debug()}
    assert live["persona"] == "from the operator"
