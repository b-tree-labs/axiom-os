# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A model's argument is checked against what the capability declares.

The chat loop already routes a question to a verb: the model picks a
function-calling tool, the platform runs it, and the rows come out of the
database rather than out of the model. Two things could still go wrong with
that, and only one of them had a symptom.

Naming a tool that does not exist was already refused — nothing runs, and
the model is told. Passing an argument the capability does not take was
not. ``params.get`` simply ignores it, the capability runs on the rest, and
the answer is a correct answer to a DIFFERENT question, indistinguishable
from the right one. Asked for last week and given all time, a reader sees a
table and no reason to doubt it.

So the stray argument is refused and named. The narrowness matters as much
as the check: ``inputs`` is documentation today rather than a contract, and
a capability that has declared nothing must not start refusing everything.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from axiom.extensions.builtins.chat.tools import _execute_skill_tool
from axiom.infra.capability_projection import undeclared_params
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

SEEN: list[dict] = []


def _echo(params, ctx):
    SEEN.append(dict(params))
    return SkillResult(ok=True, value={"got": dict(params)})


def _spec(name="data.catalog", inputs=None):
    return SkillSpec(
        name=name,
        fn=_echo,
        description="what it does",
        inputs={"tier": "str", "object": "str!"} if inputs is None else inputs,
        side_effects=False,
        surfaces=("cli", "mcp", "agent_tool"),
    )


def _wire(monkeypatch, spec):
    registry = SkillRegistry()
    registry.register_skill(spec, mutating=False)
    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.tools._skill_registry", lambda: registry
    )
    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.tools._skill_context",
        lambda r: SkillContext(
            registry=r,
            state_dir=Path(tempfile.mkdtemp()),
            logger=logging.getLogger("test.capability.args"),
        ),
    )
    SEEN.clear()
    return registry


# --- the check itself ------------------------------------------------------


def test_an_argument_the_capability_declares_is_fine():
    assert undeclared_params(_spec(), {"tier": "gold", "object": "signals"}) == []


def test_an_argument_it_does_not_declare_is_named():
    assert undeclared_params(_spec(), {"object": "x", "colour": "red"}) == ["colour"]


def test_a_capability_that_declared_nothing_is_not_policed():
    """`inputs` is documentation, not a contract. Reading "takes nothing"
    from "has not said" would refuse every call to most of the tree."""
    assert undeclared_params(_spec(inputs={}), {"anything": 1}) == []


def test_several_stray_arguments_are_all_named_in_order():
    assert undeclared_params(_spec(), {"zulu": 1, "alpha": 2}) == ["alpha", "zulu"]


# --- through the dispatch a model actually reaches -------------------------


def test_the_capability_never_runs_with_an_argument_it_does_not_take(monkeypatch):
    _wire(monkeypatch, _spec())
    out = _execute_skill_tool("data__catalog", {"object": "signals", "colour": "red"})

    assert out["ok"] is False
    assert "colour" in out["errors"][0]
    assert SEEN == [], "the capability ran on the remaining arguments"


def test_the_refusal_names_the_capability_so_the_next_call_can_be_right(monkeypatch):
    _wire(monkeypatch, _spec())
    out = _execute_skill_tool("data__catalog", {"nope": 1})

    assert "data.catalog" in out["errors"][0]


def test_a_well_formed_call_still_runs(monkeypatch):
    _wire(monkeypatch, _spec())
    out = _execute_skill_tool("data__catalog", {"tier": "gold", "object": "signals"})

    assert out["ok"] is True
    assert SEEN == [{"tier": "gold", "object": "signals"}]


def test_a_capability_with_no_declared_inputs_still_runs(monkeypatch):
    """The regression this would have been across most of the tree."""
    _wire(monkeypatch, _spec(inputs={}))
    out = _execute_skill_tool("data__catalog", {"whatever": 1})

    assert out["ok"] is True
    assert SEEN == [{"whatever": 1}]


def test_the_shape_of_the_answer_is_unchanged(monkeypatch):
    """Same four keys whichever way it went, so no caller learns a new shape."""
    _wire(monkeypatch, _spec())
    ok = _execute_skill_tool("data__catalog", {"object": "s"})
    refused = _execute_skill_tool("data__catalog", {"bad": 1})

    assert set(ok) == set(refused) == {"ok", "value", "errors", "actions_taken"}
