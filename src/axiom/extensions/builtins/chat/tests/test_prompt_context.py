# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Prompt contributors that want to know which request they are contributing to.

An installed extension contributes system-prompt fragments through an entry
point. Until now the loader called every contributor with no arguments, so a
contributor could know nothing about the request: not who was asking, not
which conversation, not what mode the surface was in. Anything that varies
per request rather than per process was therefore impossible to contribute.

These tests pin the seam that fixes it, and they pin its two hard edges:

* a contributor that takes no arguments keeps being called with none, and
  what it produces is unchanged;
* the loader still never raises, so a contributor that is missing, broken,
  or confused by the new argument is logged and skipped and the agent still
  builds a prompt.

The subtle one is the last class in this file. Backward compatibility is
decided by inspecting the contributor's signature, not by calling it and
catching ``TypeError``, because a ``TypeError`` raised inside a
contributor's own body is indistinguishable from one raised by the
interpreter for a bad call. Under the catching implementation such a
contributor is silently invoked a second time with no arguments, which runs
its side effects twice and can land a fragment that was composed without the
context it asked for.
"""

from __future__ import annotations

import importlib.metadata
import logging
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat.agent import (
    PROMPT_CONTRIBUTOR_GROUP,
    ChatAgent,
    _accepts_prompt_context,
    _discover_prompt_contributions,
    _prompt_context_for,
)
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.infra.orchestrator.session import Session
from axiom.infra.prompt_context import PromptContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEntryPoint:
    """One entry point in the contributor group, with a controllable load."""

    def __init__(self, name: str, target: Any = None, load_error: Exception | None = None):
        self.name = name
        self._target = target
        self._load_error = load_error

    def load(self) -> Any:
        if self._load_error is not None:
            raise self._load_error
        return self._target


@contextmanager
def registered(*eps: _FakeEntryPoint):
    """Install ``eps`` as the contributor group, leaving other groups alone.

    Other machinery on the prompt path reads its own entry-point groups, so
    this delegates every group but the contributor one to the real lookup.
    """
    real = importlib.metadata.entry_points

    def fake(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("group") == PROMPT_CONTRIBUTOR_GROUP:
            return list(eps)
        return real(*args, **kwargs)

    with patch.object(importlib.metadata, "entry_points", fake):
        yield


def fragment(name: str, content: str, layer: str = "identity") -> dict:
    return {
        "layer": layer,
        "name": name,
        "content": content,
        "source": "test",
        "required": False,
    }


def scope_for(
    principal: str = "@axi:one",
    session_id: str = "sess-1",
    mode: str = "agent",
    workspace: str = "",
) -> ChatScope:
    return ChatScope(
        session=Session(session_id=session_id, principal_id=principal),
        interaction_mode=mode,
        workspace_context=workspace,
    )


# ---------------------------------------------------------------------------
# The zero-argument form, unchanged
# ---------------------------------------------------------------------------


class TestZeroArgumentContributorIsUnchanged:
    """The published contract is a zero-arg callable. It keeps working."""

    def test_zero_arg_contributor_is_called_with_no_arguments(self):
        seen: list[tuple] = []

        def contribute():
            seen.append(())
            return [fragment("legacy", "LEGACY-FRAGMENT")]

        with registered(_FakeEntryPoint("legacy", contribute)):
            frags = _discover_prompt_contributions(PromptContext(principal_id="@axi:one"))

        assert seen == [()]
        assert frags == [fragment("legacy", "LEGACY-FRAGMENT")]

    def test_zero_arg_result_is_identical_with_and_without_a_context(self):
        def contribute():
            return [fragment("legacy", "LEGACY-FRAGMENT")]

        with registered(_FakeEntryPoint("legacy", contribute)):
            without = _discover_prompt_contributions()
            with_ctx = _discover_prompt_contributions(scope_context())

        assert without == with_ctx

    def test_discovery_still_callable_with_no_argument_at_all(self):
        # Nothing forces a caller to have a request in hand.
        with registered(_FakeEntryPoint("legacy", lambda: [fragment("a", "A")])):
            assert _discover_prompt_contributions() == [fragment("a", "A")]


def scope_context() -> PromptContext:
    return _prompt_context_for(scope_for())


# ---------------------------------------------------------------------------
# The new form
# ---------------------------------------------------------------------------


class TestContextIsDelivered:
    """A contributor that asks for the context gets it, populated."""

    def test_contributor_receives_the_context_positionally(self):
        seen: list[PromptContext] = []

        def contribute(context):
            seen.append(context)
            return [fragment("aware", f"principal={context.principal_id}")]

        ctx = PromptContext(
            principal_id="@axi:one",
            session_id="sess-1",
            interaction_mode="plan",
            workspace_context="BRIEF",
        )
        with registered(_FakeEntryPoint("aware", contribute)):
            frags = _discover_prompt_contributions(ctx)

        assert seen == [ctx]
        assert frags[0]["content"] == "principal=@axi:one"

    def test_every_pinned_field_is_populated_for_the_contributor(self):
        seen: list[PromptContext] = []

        def contribute(context):
            seen.append(context)
            return []

        scope = scope_for(
            principal="@axi:one", session_id="sess-1", mode="plan", workspace="BRIEF-A"
        )
        with registered(_FakeEntryPoint("aware", contribute)):
            _discover_prompt_contributions(_prompt_context_for(scope))

        (got,) = seen
        assert got.principal_id == "@axi:one"
        assert got.session_id == "sess-1"
        assert got.interaction_mode == "plan"
        assert got.workspace_context == "BRIEF-A"

    def test_positional_only_parameter_is_accepted(self):
        # Pins the calling convention as positional: a contributor may name
        # its parameter whatever it likes.
        seen: list[PromptContext] = []

        def contribute(whatever_it_is_called, /):
            seen.append(whatever_it_is_called)
            return []

        ctx = scope_context()
        with registered(_FakeEntryPoint("aware", contribute)):
            _discover_prompt_contributions(ctx)

        assert seen == [ctx]

    def test_a_contributor_with_a_defaulted_parameter_still_gets_the_context(self):
        seen: list[Any] = []

        def contribute(context=None):
            seen.append(context)
            return []

        ctx = scope_context()
        with registered(_FakeEntryPoint("aware", contribute)):
            _discover_prompt_contributions(ctx)

        assert seen == [ctx]

    def test_a_contributor_given_no_context_receives_an_empty_one(self):
        seen: list[PromptContext] = []

        def contribute(context):
            seen.append(context)
            return []

        with registered(_FakeEntryPoint("aware", contribute)):
            _discover_prompt_contributions()

        (got,) = seen
        assert got == PromptContext()

    def test_the_context_a_contributor_receives_is_read_only(self):
        import dataclasses

        errors: list[Exception] = []

        def contribute(context):
            try:
                context.principal_id = "@axi:escalated"  # type: ignore[misc]
            except Exception as exc:
                errors.append(exc)
            return []

        with registered(_FakeEntryPoint("aware", contribute)):
            _discover_prompt_contributions(PromptContext(principal_id="@axi:one"))

        assert len(errors) == 1
        assert isinstance(errors[0], dataclasses.FrozenInstanceError)


class TestSignatureInspection:
    """Which callables are judged to want a context, and which are not."""

    def test_no_parameters_means_no_context(self):
        assert _accepts_prompt_context(lambda: []) is False

    def test_one_positional_parameter_means_context(self):
        assert _accepts_prompt_context(lambda ctx: []) is True

    def test_defaulted_positional_parameter_means_context(self):
        assert _accepts_prompt_context(lambda ctx=None: []) is True

    def test_var_positional_means_context(self):
        assert _accepts_prompt_context(lambda *args: []) is True

    def test_keywords_only_means_no_context(self):
        # The convention is positional, so a callable with nowhere positional
        # to put the context is called the way it always was.
        assert _accepts_prompt_context(lambda **kwargs: []) is False

    def test_keyword_only_parameter_means_no_context(self):
        assert _accepts_prompt_context(lambda *, context=None: []) is False

    def test_a_kwargs_only_contributor_is_called_with_no_arguments(self):
        seen: list[dict] = []

        def contribute(**kwargs):
            seen.append(kwargs)
            return []

        with registered(_FakeEntryPoint("kw", contribute)):
            _discover_prompt_contributions(scope_context())

        assert seen == [{}]

    def test_bound_method_ignores_its_own_self(self):
        class Contributor:
            def zero(self):
                return []

            def one(self, context):
                return []

        c = Contributor()
        assert _accepts_prompt_context(c.zero) is False
        assert _accepts_prompt_context(c.one) is True

    def test_callable_object_is_read_through_its_call(self):
        class Zero:
            def __call__(self):
                return []

        class One:
            def __call__(self, context):
                return []

        assert _accepts_prompt_context(Zero()) is False
        assert _accepts_prompt_context(One()) is True

    def test_uninspectable_callable_is_treated_as_zero_argument(self):
        # ``range`` has no introspectable signature. An unreadable signature
        # is not evidence that a context is wanted.
        assert _accepts_prompt_context(range) is False


# ---------------------------------------------------------------------------
# One context per request
# ---------------------------------------------------------------------------


class TestPerRequestIsolation:
    """Two requests, two contexts, neither carrying the other's values."""

    def test_two_scopes_produce_two_independent_contexts(self):
        seen: list[PromptContext] = []

        def contribute(context):
            seen.append(context)
            return []

        a = scope_for(principal="@axi:a", session_id="sess-a", mode="ask", workspace="BRIEF-A")
        b = scope_for(principal="@axi:b", session_id="sess-b", mode="plan", workspace="BRIEF-B")

        with registered(_FakeEntryPoint("aware", contribute)):
            _discover_prompt_contributions(_prompt_context_for(a))
            _discover_prompt_contributions(_prompt_context_for(b))

        first, second = seen
        assert first is not second
        assert (first.principal_id, first.session_id) == ("@axi:a", "sess-a")
        assert (second.principal_id, second.session_id) == ("@axi:b", "sess-b")
        assert (first.interaction_mode, first.workspace_context) == ("ask", "BRIEF-A")
        assert (second.interaction_mode, second.workspace_context) == ("plan", "BRIEF-B")

    def test_two_prompts_built_from_two_scopes_carry_two_contexts(self):
        seen: list[PromptContext] = []

        def contribute(context):
            seen.append(context)
            return []

        agent = ChatAgent()
        a = scope_for(principal="@axi:a", session_id="sess-a", mode="ask", workspace="BRIEF-A")
        b = scope_for(principal="@axi:b", session_id="sess-b", mode="plan", workspace="BRIEF-B")

        with registered(_FakeEntryPoint("aware", contribute)):
            agent._build_system_prompt(scope=a)
            agent._build_system_prompt(scope=b)

        assert [c.principal_id for c in seen] == ["@axi:a", "@axi:b"]
        assert [c.session_id for c in seen] == ["sess-a", "sess-b"]
        assert [c.interaction_mode for c in seen] == ["ask", "plan"]
        assert [c.workspace_context for c in seen] == ["BRIEF-A", "BRIEF-B"]

    def test_a_contributor_cannot_reach_the_scope_through_the_context(self):
        # It is a declared field set, not the request's state object.
        seen: list[PromptContext] = []

        with registered(_FakeEntryPoint("aware", lambda context: seen.append(context) or [])):
            _discover_prompt_contributions(_prompt_context_for(scope_for()))

        (got,) = seen
        assert isinstance(got, PromptContext)
        assert not isinstance(got, ChatScope)
        for banned in (
            "permissions",
            "gate",
            "allowlist",
            "cancel_event",
            "on_chunk",
            "pending_images",
            "last_composer",
            "last_retrieved",
            "session",
            "turn_query",
            "session_mode",
        ):
            assert not hasattr(got, banned), f"context exposes {banned!r}"


class TestProjection:
    """``_prompt_context_for`` names every field it copies, and cannot raise."""

    def test_reads_the_four_fields_off_the_scope(self):
        scope = scope_for(principal="@axi:one", session_id="sess-1", mode="plan", workspace="BRIEF")
        assert _prompt_context_for(scope) == PromptContext(
            principal_id="@axi:one",
            session_id="sess-1",
            interaction_mode="plan",
            workspace_context="BRIEF",
        )

    def test_an_unbound_session_yields_an_empty_principal(self):
        scope = ChatScope(session=Session(session_id="sess-1"))
        assert _prompt_context_for(scope).principal_id == ""

    def test_an_object_with_none_of_the_fields_yields_an_empty_context(self):
        assert _prompt_context_for(object()) == PromptContext()  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The never-raises promise
# ---------------------------------------------------------------------------


class TestNeverRaises:
    """Missing, broken or confused: logged and skipped, prompt still built."""

    def test_a_contributor_that_fails_to_load_is_skipped(self, caplog):
        with (
            caplog.at_level(logging.WARNING),
            registered(
                _FakeEntryPoint("gone", load_error=ImportError("no module")),
                _FakeEntryPoint("good", lambda: [fragment("good", "GOOD")]),
            ),
        ):
            frags = _discover_prompt_contributions(scope_context())

        assert frags == [fragment("good", "GOOD")]
        assert "gone" in caplog.text

    def test_a_contributor_that_raises_is_logged_and_skipped(self, caplog):
        def boom(context):
            raise RuntimeError("contributor exploded")

        with (
            caplog.at_level(logging.WARNING),
            registered(
                _FakeEntryPoint("boom", boom),
                _FakeEntryPoint("good", lambda context: [fragment("good", "GOOD")]),
            ),
        ):
            frags = _discover_prompt_contributions(scope_context())

        assert frags == [fragment("good", "GOOD")]
        assert "boom" in caplog.text
        assert "contributor exploded" in caplog.text

    def test_a_bad_fragment_shape_is_skipped(self):
        with registered(
            _FakeEntryPoint("shape", lambda context: [{"layer": "identity"}, 17, None]),
            _FakeEntryPoint("good", lambda context: [fragment("good", "GOOD")]),
        ):
            frags = _discover_prompt_contributions(scope_context())

        assert frags == [fragment("good", "GOOD")]

    def test_a_contributor_wanting_two_arguments_is_skipped(self, caplog):
        def greedy(context, extra):
            return [fragment("greedy", "GREEDY")]

        with caplog.at_level(logging.WARNING), registered(_FakeEntryPoint("greedy", greedy)):
            assert _discover_prompt_contributions(scope_context()) == []
        assert "greedy" in caplog.text

    def test_the_prompt_is_still_built_when_every_contributor_fails(self):
        def boom(context):
            raise RuntimeError("nope")

        agent = ChatAgent()
        with registered(_FakeEntryPoint("boom", boom)):
            prompt = agent._build_system_prompt(scope=scope_for())

        assert isinstance(prompt, str)
        assert prompt.strip()

    def test_an_entry_point_lookup_failure_yields_no_fragments(self, caplog):
        def exploding(*args: Any, **kwargs: Any):
            raise RuntimeError("index unreadable")

        with caplog.at_level(logging.WARNING):
            with patch.object(importlib.metadata, "entry_points", exploding):
                assert _discover_prompt_contributions(scope_context()) == []
        assert "index unreadable" in caplog.text


class TestTypeErrorInsideTheBody:
    """The one a ``TypeError`` catch gets wrong.

    A contributor may raise ``TypeError`` from its own body for reasons that
    have nothing to do with how it was called. Signature inspection decides
    the calling convention before the call, so such a contributor is skipped
    once. An implementation that called the contributor and caught
    ``TypeError`` would take the exception as proof the contributor wanted no
    argument, call it a second time, and use whatever came back.
    """

    def test_a_context_aware_contributor_raising_type_error_is_not_retried(self, caplog):
        calls: list[Any] = []

        def two_faced(context=None):
            calls.append(context)
            if context is None:
                # Only reachable through a retry that dropped the context.
                return [fragment("fallback", "COMPOSED-WITHOUT-CONTEXT")]
            raise TypeError("something inside me is the wrong type")

        with caplog.at_level(logging.WARNING), registered(_FakeEntryPoint("two_faced", two_faced)):
            frags = _discover_prompt_contributions(scope_context())

        assert len(calls) == 1, "the contributor was invoked twice; its side effects ran twice"
        assert calls[0] is not None, "the contributor was called without the context it declared"
        assert frags == [], "a fragment composed without the context reached the prompt"
        assert "two_faced" in caplog.text

    def test_a_zero_arg_contributor_raising_type_error_is_skipped(self, caplog):
        calls: list[int] = []

        def legacy_boom():
            calls.append(1)
            raise TypeError("something inside me is the wrong type")

        with (
            caplog.at_level(logging.WARNING),
            registered(
                _FakeEntryPoint("legacy_boom", legacy_boom),
                _FakeEntryPoint("good", lambda: [fragment("good", "GOOD")]),
            ),
        ):
            frags = _discover_prompt_contributions(scope_context())

        assert calls == [1]
        assert frags == [fragment("good", "GOOD")]
        assert "legacy_boom" in caplog.text

    def test_the_prompt_survives_a_type_error_inside_a_contributor(self):
        def two_faced(context=None):
            if context is None:
                return [fragment("fallback", "COMPOSED-WITHOUT-CONTEXT")]
            raise TypeError("boom")

        agent = ChatAgent()
        with registered(_FakeEntryPoint("two_faced", two_faced)):
            prompt = agent._build_system_prompt(scope=scope_for())

        assert prompt.strip()
        assert "COMPOSED-WITHOUT-CONTEXT" not in prompt


class TestCallSite:
    """The prompt build hands a context down; it does not hand the scope down."""

    def test_build_system_prompt_passes_a_prompt_context(self):
        seen: list[Any] = []

        def spy(context=None):
            seen.append(context)
            return []

        agent = ChatAgent()
        with patch.object(agent_mod, "_discover_prompt_contributions", spy):
            agent._build_system_prompt(scope=scope_for(principal="@axi:one"))

        assert len(seen) == 1
        assert isinstance(seen[0], PromptContext)
        assert seen[0].principal_id == "@axi:one"

    def test_a_contributed_fragment_still_reaches_the_prompt(self):
        agent = ChatAgent()
        with registered(
            _FakeEntryPoint(
                "aware",
                lambda context: [fragment("role", f"ROLE-FOR-{context.principal_id}")],
            )
        ):
            prompt = agent._build_system_prompt(scope=scope_for(principal="@axi:one"))

        assert "ROLE-FOR-@axi:one" in prompt


@pytest.mark.parametrize("mode", ["ask", "plan", "agent"])
def test_interaction_mode_reaches_the_contributor(mode):
    seen: list[str] = []

    with registered(
        _FakeEntryPoint("aware", lambda context: seen.append(context.interaction_mode) or [])
    ):
        _discover_prompt_contributions(_prompt_context_for(scope_for(mode=mode)))

    assert seen == [mode]
