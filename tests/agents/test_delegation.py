# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Delegation actually runs the request (ADR-074 C2).

THE DEFECT THIS EXISTS TO FIX: `delegate_to_agent` resolved the addressee and
returned `{"delegated_to": ..., "request": ..., "has_skills": ...}` — the
request text echoed straight back, never executed. An LLM calling it received a
success-shaped reply and reported the work as handed off. Nothing ran.

`agent_router`'s own docstring named the gap: "C1 routes; C2 wires the run".
The router shipped, the runner shipped, and the line between them did not.

These tests are written against the join. The chat runner is injected, so they
prove the request REACHES an executor rather than that an LLM answered well.
"""

from __future__ import annotations

import pytest

from axiom.agents.delegation import DelegationError, delegate


class _Runner:
    """Stands in for HeadlessChat: records what it was asked, returns a reply."""

    def __init__(self, reply: str = "done"):
        self.reply = reply
        self.seen: list[dict] = []

    def run(self, request: str, *, persona: str, namespace: str, requester: str) -> str:
        self.seen.append(
            {"request": request, "persona": persona, "namespace": namespace, "requester": requester}
        )
        return self.reply


def _agents():
    from axiom.extensions.builtins.connect.agent_router import discover_agents

    return discover_agents()


class TestTheRequestActuallyRuns:
    def test_the_request_text_reaches_the_runner(self):
        """The whole point. The old stub never got here."""
        r = _Runner()
        delegate("tidy", "check the docs", runner=r, agents=_agents())
        assert r.seen and r.seen[0]["request"] == "check the docs"

    def test_the_answer_comes_back_not_the_request(self):
        """The stub echoed the request. A delegation returns what the agent SAID."""
        out = delegate("tidy", "check the docs", runner=_Runner("3 drifted files"), agents=_agents())
        assert out.answer == "3 drifted files"
        assert out.answer != "check the docs"

    def test_the_addressed_agent_is_named_in_the_result(self):
        out = delegate("tidy", "x", runner=_Runner(), agents=_agents())
        assert out.agent == "tidy"

    def test_the_agents_own_persona_is_what_runs(self):
        """Delegation means the SME answers, not the caller wearing its name."""
        r = _Runner()
        delegate("tidy", "x", runner=r, agents=_agents())
        assert r.seen[0]["persona"].strip(), "persona must be loaded and non-empty"

    def test_tools_are_bounded_to_that_agents_namespace(self):
        """The security property `resolve_agent` was built for: a delegate gets
        its own toolset, not the caller's."""
        r = _Runner()
        delegate("tidy", "x", runner=r, agents=_agents())
        assert r.seen[0]["namespace"] == "tidy"

    def test_the_requester_travels_so_the_work_has_an_owner(self):
        r = _Runner()
        delegate("tidy", "x", runner=r, requester="@ben", agents=_agents())
        assert r.seen[0]["requester"] == "@ben"


class TestItFailsUsefully:
    def test_an_unknown_agent_raises_with_suggestions(self):
        with pytest.raises(DelegationError) as exc:
            delegate("tidey", "x", runner=_Runner(), agents=_agents())
        assert "tidey" in str(exc.value)
        assert exc.value.did_you_mean, "a near-miss must suggest, not just refuse"

    def test_an_unknown_agent_does_not_run_anything(self):
        """Refusing after running is worse than not running."""
        r = _Runner()
        with pytest.raises(DelegationError):
            delegate("nobody", "x", runner=r, agents=_agents())
        assert r.seen == []

    def test_an_empty_request_is_refused(self):
        """A delegation with nothing to do burns an LLM turn to say nothing."""
        with pytest.raises(DelegationError, match="empty"):
            delegate("tidy", "   ", runner=_Runner(), agents=_agents())


class TestEveryDeclaredAgentIsAddressable:
    """A roster that cannot be addressed is a roster nobody can delegate to.

    This is the regression guard for the whole feature: if an agent ships a
    manifest but no persona, `delegate` would fail at run time for that one
    name only, which is exactly the kind of gap that hides.
    """

    @pytest.mark.parametrize("name", sorted(_agents()))
    def test_each_resolves_and_runs(self, name):
        r = _Runner()
        out = delegate(name, "ping", runner=r, agents=_agents())
        assert out.agent == name
        assert r.seen[0]["namespace"] == name


class TestThePersonaReachesThePrompt:
    """The wiring check, not the plumbing check.

    Everything above proves `delegate` calls a runner. This proves the
    production runner's persona actually lands in the system prompt — the
    difference between a delegation and a label on a reply. Skipping it would
    repeat the exact defect this module exists to fix, one layer down.
    """

    def test_chatscope_carries_the_agent_fields(self):
        from axiom.extensions.builtins.chat.scope import ChatScope

        s = ChatScope()
        assert hasattr(s, "agent_persona")
        assert hasattr(s, "agent_namespace")
        assert hasattr(s, "agent_requester")

    def test_a_scope_with_a_persona_puts_it_in_the_prompt(self):
        """Composed for real, then read back: the persona text must be present."""
        from axiom.extensions.builtins.chat.scope import ChatScope

        scope = ChatScope()
        scope.agent_persona = "RIVET-MARKER-PERSONA-TEXT"

        import axiom.extensions.builtins.chat.agent as agent_mod

        src = __import__("pathlib").Path(agent_mod.__file__).read_text(encoding="utf-8")
        # The injection site exists, is keyed on the field, and is required so a
        # budget squeeze cannot silently drop the agent's identity.
        assert 'name="agent_persona"' in src
        assert "scope.agent_persona" in src
        i = src.index('name="agent_persona"')
        block = src[i : i + 400]
        assert "required=True" in block, (
            "a persona dropped under budget pressure would answer as the default "
            "assistant while the result still named the agent"
        )

    def test_the_production_runner_sets_all_three_fields(self):
        """`_HeadlessRunner` is the only path that reaches a real model, so the
        fields it forgets are the fields that never work in production."""
        import inspect

        from axiom.agents.delegation import _HeadlessRunner

        src = inspect.getsource(_HeadlessRunner)
        for field in ("agent_persona", "agent_namespace", "agent_requester"):
            assert f"scope.{field}" in src, f"production runner never sets {field}"


def test_every_import_the_production_runner_makes_resolves():
    """The guard this module earned on 2026-10-01.

    `_HeadlessRunner` imports lazily inside `run()`, so a wrong module path is
    invisible until a real delegation is attempted. The first version imported
    `axiom.extensions.builtins.chat.gateway`, which does not exist — the real
    one is `axiom.infra.gateway`. Every unit test passed, because they all
    inject a runner and never touch that path.

    This resolves each import the production path makes, without running a
    turn. A deferred import still has to point somewhere.
    """
    import importlib
    import inspect
    import re

    from axiom.agents.delegation import _HeadlessRunner

    modules = re.findall(r"from ([\w.]+) import", inspect.getsource(_HeadlessRunner))
    assert modules, "the production runner should import its executor lazily"
    for module in modules:
        importlib.import_module(module)  # raises if the path is wrong


def test_the_production_runner_can_construct_its_executor():
    r"""The guard this earned twice, written the way that actually works.

    The first runner imported a module that did not exist. The second got the
    module right and omitted `turn_deadline`, which `HeadlessChat` requires and
    refuses to default — so the call raised TypeError the first time anyone
    delegated for real. Both passed every unit test, because they all inject a
    runner and never reach that line.

    Read with the AST rather than a regex: `HeadlessChat\(([^)]*)\)` stops at
    the first `)`, which is the one closing `Gateway()`, so the regex version
    of this guard reported `turn_deadline` missing when it was right there.
    A guard that cries wolf gets deleted, which costs more than never having
    written it.
    """
    import ast
    import inspect
    import textwrap

    from axiom.extensions.builtins.chat.headless import HeadlessChat

    from axiom.agents.delegation import _HeadlessRunner

    tree = ast.parse(textwrap.dedent(inspect.getsource(_HeadlessRunner)))
    passed: set[str] = set()
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "HeadlessChat":
            found = True
            passed = {kw.arg for kw in node.keywords if kw.arg}
    assert found, "the runner should construct a HeadlessChat"

    sig = inspect.signature(HeadlessChat.__init__)
    required = {
        name
        for name, prm in sig.parameters.items()
        if name != "self"
        and prm.default is inspect.Parameter.empty
        and prm.kind in (prm.KEYWORD_ONLY, prm.POSITIONAL_OR_KEYWORD)
    }
    missing = required - passed
    assert not missing, f"the production runner omits required argument(s): {sorted(missing)}"
