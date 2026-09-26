# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One call gives a caller with no terminal a correctly configured agent.

Everything a headless surface needs already exists: a render provider that
paints nothing, a policy that answers approvals without asking anyone, a
per-request scope, budgets on that scope, a chunk consumer and a clause
consumer. Assembling them by hand is six decisions, and getting any one of
them wrong fails quietly rather than loudly. ``HeadlessChat`` is the assembly.

These tests pin four claims:

* the produced agent cannot prompt and cannot paint, across a whole turn that
  runs one tool and has another refused;
* every request gets its own scope, so an approval in one says nothing about
  the next;
* the budgets the handle was built with are actually on that scope and
  actually stop a turn;
* a misconfiguration is a construction error with a message that says what to
  pass, rather than a surprise in production.

And the fifth, which is the reason the other four are worth having: a terminal
caller is untouched by all of it.
"""

from __future__ import annotations

import builtins

import pytest

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat import renderer as renderer_mod
from axiom.extensions.builtins.chat.agent import (
    DEADLINE_MESSAGE,
    MAX_TOOL_ROUNDS,
    ChatAgent,
)
from axiom.extensions.builtins.chat.approval_policy import (
    ApprovalPolicy,
    NonInteractiveApprovalPolicy,
)
from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.extensions.builtins.chat.providers.null_render import NullRenderProvider
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.extensions.builtins.chat.utterances import UtteranceAggregator
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, StreamChunk, ToolUseBlock
from axiom.infra.orchestrator.session import Session

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock the test moves by hand, never by sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(agent_mod, "_now", fake)
    return fake


@pytest.fixture
def mock_gateway():
    from unittest.mock import MagicMock

    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    gw.complete_with_tools.return_value = CompletionResponse(
        text="ok", provider="test", success=True
    )
    return gw


@pytest.fixture
def headless(mock_gateway, tmp_path):
    """The handle under test, wired to a scripted gateway."""
    return HeadlessChat(
        turn_deadline=30.0,
        gateway=mock_gateway,
        bus=EventBus(log_path=tmp_path / "events.jsonl"),
    )


class _ExplodingStdin:
    """Any read at all is a defect: a headless turn asks nobody anything."""

    def _boom(self, *_a, **_kw):
        raise AssertionError("a headless turn read stdin")

    read = readline = readlines = __iter__ = __next__ = _boom

    def isatty(self) -> bool:
        raise AssertionError("a headless turn sniffed for a terminal")


@pytest.fixture
def no_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every route to a human, and every module-level paint, raise."""

    def _boom(*_a, **_kw):
        raise AssertionError("something reached for a human or a terminal")

    monkeypatch.setattr(builtins, "input", _boom)
    monkeypatch.setattr("sys.stdin", _ExplodingStdin())
    # The module-level renderer is the fallback taken when no render provider
    # is installed. A headless agent has one, so none of these may fire.
    monkeypatch.setattr(renderer_mod, "render_action_result", _boom)
    monkeypatch.setattr(renderer_mod, "render_approval_prompt", _boom)


def _text(s: str) -> StreamChunk:
    return StreamChunk(type="text", text=s)


def _one_good_one_refused(target) -> CompletionResponse:
    """A model round asking for a read tool and a write tool at once.

    The read is auto-approved and executes; the write reaches the policy and
    is refused. Between them they exercise every paint the tool loop makes.
    """
    return CompletionResponse(
        text="",
        tool_use=[
            ToolUseBlock(tool_id="t1", name="list_providers", input={}),
            ToolUseBlock(
                tool_id="t2",
                name="write_file",
                input={"file_path": str(target), "content": "should not land"},
            ),
        ],
        provider="test",
        success=True,
        stop_reason="tool_use",
    )


def _then_answer(*responses):
    """Script the gateway: each call returns the next response, last repeats."""
    queue = list(responses)

    def respond(*_a, **_kw):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return respond


def _answer(text: str = "Done.") -> CompletionResponse:
    return CompletionResponse(text=text, provider="test", success=True)


def _write_call(target) -> CompletionResponse:
    return CompletionResponse(
        text="",
        tool_use=[
            ToolUseBlock(
                tool_id="t1",
                name="write_file",
                input={"file_path": str(target), "content": "hi"},
            )
        ],
        provider="test",
        success=True,
        stop_reason="tool_use",
    )


def _tool_spammer():
    """A model that asks for another tool call whenever it is offered tools."""

    def respond(*_a, **kwargs):
        if not kwargs.get("tools"):
            return _answer("Synthesized answer from gathered context.")
        return CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
            provider="test",
            success=True,
            stop_reason="tool_use",
        )

    return respond


# ---------------------------------------------------------------------------
# It cannot prompt and it cannot paint
# ---------------------------------------------------------------------------


class TestItCannotPaint:
    def test_a_null_render_provider_is_installed(self, headless):
        assert isinstance(headless.agent._render, NullRenderProvider)

    def test_a_whole_turn_writes_nothing_to_stdout_or_stderr(
        self, headless, mock_gateway, capsys, no_human, tmp_path
    ):
        """One tool runs, one tool is refused, and the process stays silent."""
        target = tmp_path / "nope.txt"
        mock_gateway.complete_with_tools.side_effect = _then_answer(
            _one_good_one_refused(target), _answer()
        )

        headless.turn("Do the thing.", stream=False)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert not target.exists()

    def test_a_streaming_turn_writes_nothing_either(self, headless, mock_gateway, capsys, no_human):
        """The teed branch paints through the provider, and the provider is null."""
        mock_gateway.stream_with_tools.return_value = iter(
            [
                _text("Hello "),
                StreamChunk(type="thinking_delta", text="pondering"),
                _text("world."),
            ]
        )

        assert headless.turn("Say hello.") == "Hello world."

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_the_module_level_renderer_is_never_reached(
        self, headless, mock_gateway, no_human, tmp_path
    ):
        """The leak A1 left open: a refusal falling back to the bare renderer.

        ``no_human`` replaces both module-level renderer entry points with
        something that raises, so reaching either fails this test rather than
        printing ``[skipped] write_file`` into a log.
        """
        target = tmp_path / "nope.txt"
        mock_gateway.complete_with_tools.side_effect = _then_answer(
            _one_good_one_refused(target), _answer()
        )

        headless.turn("Do the thing.", stream=False)

    def test_a_persisted_deny_also_takes_the_null_provider(
        self, headless, mock_gateway, no_human, capsys, tmp_path
    ):
        """The other rejection path in the loop, and it paints through the same seam."""
        scope = headless.new_scope()
        scope.permissions.set("write_file", "deny")
        target = tmp_path / "nope.txt"
        mock_gateway.complete_with_tools.side_effect = _then_answer(_write_call(target), _answer())

        headless.turn("Write it.", stream=False, scope=scope)

        assert capsys.readouterr().out == ""
        assert not target.exists()


class TestItCannotPrompt:
    def test_an_approval_policy_is_installed(self, headless):
        assert isinstance(headless.agent.approval_policy, ApprovalPolicy)

    def test_the_default_policy_refuses_everything(self, headless):
        assert headless.agent.approval_policy.allowed == frozenset()

    def test_the_provider_and_the_agent_hold_the_same_policy(self, headless):
        """Two approval seams, one answer: they cannot disagree."""
        assert headless.agent._render.approval_policy is headless.agent.approval_policy

    def test_the_handle_reports_the_policy_it_installed(self, headless):
        assert headless.approval_policy is headless.agent.approval_policy

    def test_a_turn_never_calls_input(self, headless, mock_gateway, no_human, tmp_path):
        """stdin raises on any read, so a blocked prompt is a failure, not a hang."""
        target = tmp_path / "nope.txt"
        mock_gateway.complete_with_tools.side_effect = _then_answer(_write_call(target), _answer())

        headless.turn("Write it.", stream=False)

        assert not target.exists()

    def test_a_refusal_reaches_the_caller_with_its_reason(
        self, headless, mock_gateway, no_human, tmp_path
    ):
        target = tmp_path / "nope.txt"
        seen: list[dict] = []

        def respond(*_a, **kwargs):
            for message in kwargs.get("messages", []):
                if message.get("role") == "tool":
                    seen.append(message)
            return _answer() if seen else _write_call(target)

        mock_gateway.complete_with_tools.side_effect = respond
        headless.turn("Write it.", stream=False)

        assert seen, "the tool result never reached the next round"
        assert "allowlist" in seen[0]["content"].lower()

    def test_a_named_capability_is_allowed_through(self, mock_gateway, no_human, tmp_path):
        """A site widens headless authority one tool at a time, by name."""
        handle = HeadlessChat(
            turn_deadline=30.0,
            gateway=mock_gateway,
            approval_policy=NonInteractiveApprovalPolicy(allow=["write_file"]),
        )
        target = tmp_path / "yes.txt"
        mock_gateway.complete_with_tools.side_effect = _then_answer(_write_call(target), _answer())

        handle.turn("Write it.", stream=False)

        assert target.read_text() == "hi"


class TestTheRejectionPathLeak:
    """The leak A1 left open on purpose, and where it is closed.

    An approval policy stops the prompt but not the paint: with a policy and
    no render provider, a refused tool still reaches the module-level renderer
    and prints ``[skipped] <tool>``. Silencing that unconditionally would have
    changed behaviour for terminal callers, so it stayed. A headless agent has
    a null render provider and therefore never takes that fallback, which is
    asserted below against the same tool call that leaks without one.
    """

    def _refused(self, tmp_path):
        return CompletionResponse(
            text="",
            tool_use=[
                ToolUseBlock(
                    tool_id="t1",
                    name="write_file",
                    input={"file_path": str(tmp_path / "nope.txt"), "content": "x"},
                )
            ],
            provider="test",
            success=True,
        )

    def test_a_policy_alone_still_paints_the_refusal(self, mock_gateway, capsys, tmp_path):
        """The control. Without this the assertion below could pass vacuously."""
        agent = ChatAgent(
            gateway=mock_gateway,
            bus=EventBus(),
            session=Session(),
            approval_policy=NonInteractiveApprovalPolicy(),
        )

        agent._process_tool_calls(self._refused(tmp_path))

        assert "[skipped] write_file" in capsys.readouterr().out

    def test_the_headless_agent_paints_nothing_for_the_same_call(self, headless, capsys, tmp_path):
        headless.agent._process_tool_calls(self._refused(tmp_path), scope=headless.new_scope())

        assert capsys.readouterr().out == ""

    def test_every_module_level_renderer_fallback_is_guarded_by_the_provider(self):
        """Each bare-renderer import in the tool loop sits under ``if self._render``.

        Read from the source rather than trusted, because a fallback added
        later without that guard would defeat the null provider silently.
        """
        import inspect
        import re
        import textwrap

        source = textwrap.dedent(inspect.getsource(ChatAgent._process_tool_calls))
        lines = source.splitlines()
        guard = re.compile(r"^(el)?if self\._render\b")
        fallbacks = [i for i, line in enumerate(lines) if "from .renderer import" in line]
        assert len(fallbacks) == 5, "the bare-renderer fallbacks moved; retarget this test"
        for index in fallbacks:
            preceding = [line.strip() for line in lines[max(0, index - 4) : index]]
            assert "else:" in preceding, f"unguarded bare-renderer fallback at line {index}"
            assert any(guard.match(line) for line in preceding), (
                f"bare-renderer fallback at line {index} is not guarded by self._render"
            )


# ---------------------------------------------------------------------------
# A fresh scope per request
# ---------------------------------------------------------------------------


class TestOneScopePerRequest:
    def test_two_scopes_are_two_objects(self, headless):
        first, second = headless.new_scope(), headless.new_scope()
        assert first is not second
        assert first.session is not second.session
        assert first.permissions is not second.permissions
        assert first.gate is not second.gate
        assert first.allowlist is not second.allowlist
        assert first.cancel_event is not second.cancel_event

    def test_two_turns_run_in_two_scopes(self, headless, mock_gateway, monkeypatch):
        """The caller passes nothing and still gets isolation."""
        seen: list[ChatScope | None] = []
        original = headless.agent.turn

        def spy(user_input, stream=True, *, raw=False, scope=None):
            seen.append(scope)
            return original(user_input, stream=stream, raw=raw, scope=scope)

        monkeypatch.setattr(headless.agent, "turn", spy)

        headless.turn("one", stream=False)
        headless.turn("two", stream=False)

        assert len(seen) == 2
        assert seen[0] is not None and seen[1] is not None
        assert seen[0] is not seen[1]
        assert seen[0].session is not seen[1].session

    def test_a_session_wide_allowlist_does_not_cross_requests(
        self, headless, mock_gateway, no_human, tmp_path
    ):
        """The leak this whole series exists to close, at the headless entry point."""
        approved = headless.new_scope()
        approved.allowlist.add("write_file")
        theirs = tmp_path / "theirs.txt"
        mine = tmp_path / "mine.txt"

        mock_gateway.complete_with_tools.side_effect = _then_answer(_write_call(theirs), _answer())
        headless.turn("Write it.", stream=False, scope=approved)
        assert theirs.read_text() == "hi"

        mock_gateway.complete_with_tools.side_effect = _then_answer(_write_call(mine), _answer())
        headless.turn("Write it.", stream=False)
        assert not mine.exists()

    def test_a_persisted_choice_does_not_cross_requests(
        self, headless, mock_gateway, no_human, tmp_path
    ):
        first = headless.new_scope()
        first.permissions.set("write_file", "allow")
        second = headless.new_scope()
        target = tmp_path / "nope.txt"

        mock_gateway.complete_with_tools.side_effect = _then_answer(_write_call(target), _answer())
        headless.turn("Write it.", stream=False, scope=second)

        assert not target.exists()
        assert second.permissions.get("write_file") == "ask"

    def test_history_does_not_cross_requests(self, headless, mock_gateway):
        mock_gateway.complete_with_tools.side_effect = _then_answer(_answer("first answer"))
        headless.turn("who am I", stream=False)
        mock_gateway.complete_with_tools.side_effect = _then_answer(_answer("second answer"))
        second = headless.new_scope()
        headless.turn("who am I", stream=False, scope=second)

        assert [m.content for m in second.session.messages if m.role == "user"] == ["who am I"]

    def test_no_scope_touches_the_operators_permission_file(self, headless):
        """A serving process never reads or writes the operator's saved choices."""
        assert headless.new_scope().permissions.path is None
        assert headless.agent.scope.permissions.path is None

    def test_a_caller_can_carry_a_session_across_turns(self, headless, mock_gateway):
        """Continuity is opt-in, by handing back the same scope."""
        session = Session()
        scope = headless.new_scope(session=session)
        assert scope.session is session

        mock_gateway.complete_with_tools.side_effect = _then_answer(_answer("one"))
        headless.turn("first", stream=False, scope=scope)
        mock_gateway.complete_with_tools.side_effect = _then_answer(_answer("two"))
        headless.turn("second", stream=False, scope=scope)

        assert [m.content for m in scope.session.messages if m.role == "user"] == [
            "first",
            "second",
        ]


# ---------------------------------------------------------------------------
# The budgets are on the scope and they bite
# ---------------------------------------------------------------------------


class TestBudgetsAreInForce:
    def test_the_deadline_is_on_every_scope(self, mock_gateway):
        handle = HeadlessChat(turn_deadline=7.5, gateway=mock_gateway)
        assert handle.new_scope().turn_deadline == 7.5
        assert handle.new_scope().turn_deadline == 7.5

    def test_the_round_budget_is_on_every_scope(self, mock_gateway):
        handle = HeadlessChat(turn_deadline=7.5, max_tool_rounds=3, gateway=mock_gateway)
        assert handle.new_scope().max_tool_rounds == 3

    def test_the_round_budget_defaults_to_the_platform_default(self, headless):
        assert headless.new_scope().max_tool_rounds == MAX_TOOL_ROUNDS

    def test_the_deadline_actually_stops_a_turn(self, mock_gateway, clock, no_human):
        """A model call that outlasts the budget: the next round never starts."""
        handle = HeadlessChat(turn_deadline=5.0, gateway=mock_gateway)

        def slow(*_a, **kwargs):
            clock.advance(10.0)
            return _tool_spammer()(*_a, **kwargs)

        mock_gateway.complete_with_tools.side_effect = slow

        response = handle.turn("Keep searching.", stream=False)

        assert mock_gateway.complete_with_tools.call_count == 1
        assert DEADLINE_MESSAGE in response

    def test_the_round_budget_actually_stops_a_turn(self, mock_gateway, no_human):
        handle = HeadlessChat(turn_deadline=300.0, max_tool_rounds=3, gateway=mock_gateway)
        mock_gateway.complete_with_tools.side_effect = _tool_spammer()

        handle.turn("Keep searching.", stream=False)

        assert mock_gateway.complete_with_tools.call_count == 3

    def test_a_scope_handed_in_without_a_deadline_is_refused(self, headless):
        """A hand-built scope cannot smuggle an unbounded turn past the handle."""
        with pytest.raises(ValueError, match="turn_deadline"):
            headless.turn("go", scope=ChatScope())

    def test_a_scope_handed_in_may_tighten_the_budget(self, headless, mock_gateway, no_human):
        scope = headless.new_scope()
        scope.max_tool_rounds = 2
        mock_gateway.complete_with_tools.side_effect = _tool_spammer()

        headless.turn("Keep searching.", stream=False, scope=scope)

        assert mock_gateway.complete_with_tools.call_count == 2

    def test_a_turn_still_returns_text_for_a_caller_that_wants_it(self, headless, mock_gateway):
        mock_gateway.complete_with_tools.side_effect = _then_answer(_answer("hello"))
        answer = headless.turn("hi", stream=False)
        assert isinstance(answer, str)
        assert answer == "hello"


class TestItAssumesNoTerminalShape:
    """Speech is about a year out, so nothing here may hardcode a console.

    The entry point deals in chunks and clauses. It has no width, no colour,
    no cursor and no ``isatty``, which is asserted from the code rather than
    from the prose that says so.
    """

    #: Words that would mean this module had grown an opinion about a console.
    TERMINAL_SHAPES = (
        "isatty",
        "stdout",
        "stderr",
        "print(",
        "console",
        "width",
        "cursor",
        "ansi",
        "rich",
    )

    def _code_without_prose(self) -> str:
        """The module's source with every docstring removed, lowercased."""
        import ast
        import inspect

        from axiom.extensions.builtins.chat import headless as headless_mod

        tree = ast.parse(inspect.getsource(headless_mod))
        for node in ast.walk(tree):
            if not isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            ):
                continue
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
        return ast.unparse(tree).lower()

    def test_the_code_names_no_terminal_shape(self):
        code = self._code_without_prose()
        found = [word for word in self.TERMINAL_SHAPES if word in code]
        assert found == [], f"the headless entry point grew a terminal assumption: {found}"

    def test_the_only_render_provider_it_knows_is_the_silent_one(self):
        code = self._code_without_prose()
        assert "nullrenderprovider" in code
        assert "richrenderprovider" not in code
        assert "ansirenderprovider" not in code


# ---------------------------------------------------------------------------
# Consumers: chunks, clauses, or both
# ---------------------------------------------------------------------------


class TestConsumersAreWired:
    def test_a_chunk_consumer_receives_chunks(self, headless, mock_gateway):
        seen: list[StreamChunk] = []
        mock_gateway.stream_with_tools.return_value = iter([_text("a"), _text("b")])

        headless.turn("go", on_chunk=seen.append)

        assert [c.text for c in seen] == ["a", "b"]

    def test_a_clause_consumer_receives_finished_clauses(self, headless, mock_gateway):
        heard: list[str] = []
        mock_gateway.stream_with_tools.return_value = iter(
            [_text("First one. "), _text("Second one. "), _text("A trailing")]
        )

        headless.turn("go", on_utterance=heard.append)

        assert heard == ["First one.", "Second one.", "A trailing"]

    def test_the_clause_consumer_is_the_existing_helper(self, headless):
        """Composed, not reimplemented: the segmenter has one home."""
        scope = headless.new_scope(on_utterance=lambda _text: None)
        assert isinstance(scope.on_chunk, UtteranceAggregator)

    def test_both_consumers_can_run_at_once(self, headless, mock_gateway):
        """A transcript on a screen and a voice speaking it are one request."""
        chunks: list[str] = []
        heard: list[str] = []
        mock_gateway.stream_with_tools.return_value = iter([_text("One. "), _text("Two.")])

        headless.turn("go", on_chunk=lambda c: chunks.append(c.text), on_utterance=heard.append)

        assert chunks == ["One. ", "Two."]
        assert heard == ["One.", "Two."]

    def test_one_failing_consumer_does_not_starve_the_other(self, headless, mock_gateway):
        heard: list[str] = []

        def broken(_chunk):
            raise RuntimeError("the browser went away")

        mock_gateway.stream_with_tools.return_value = iter([_text("One. "), _text("Two.")])

        assert headless.turn("go", on_chunk=broken, on_utterance=heard.append) == "One. Two."
        assert heard == ["One.", "Two."]

    def test_no_consumer_is_the_default(self, headless):
        assert headless.new_scope().on_chunk is None

    def test_consumers_belong_to_the_request_not_the_process(self, headless, mock_gateway):
        first: list[str] = []
        second: list[str] = []

        mock_gateway.stream_with_tools.return_value = iter([_text("one")])
        headless.turn("go", on_chunk=lambda c: first.append(c.text))
        mock_gateway.stream_with_tools.return_value = iter([_text("two")])
        headless.turn("go", on_chunk=lambda c: second.append(c.text))

        assert first == ["one"]
        assert second == ["two"]
        assert headless.agent.scope.on_chunk is None

    def test_a_scope_and_a_consumer_together_are_refused(self, headless):
        """The scope already carries its consumer; two answers is a bug."""
        scope = headless.new_scope()
        with pytest.raises(ValueError, match="on_chunk"):
            headless.turn("go", scope=scope, on_chunk=lambda _c: None)


# ---------------------------------------------------------------------------
# It refuses to be misconfigured
# ---------------------------------------------------------------------------


class TestItRefusesToBeMisconfigured:
    def test_a_deadline_is_required(self, mock_gateway):
        with pytest.raises(TypeError, match="turn_deadline"):
            HeadlessChat(gateway=mock_gateway)  # type: ignore[call-arg]

    def test_an_explicit_none_deadline_says_what_to_pass(self, mock_gateway):
        with pytest.raises(ValueError) as excinfo:
            HeadlessChat(turn_deadline=None, gateway=mock_gateway)  # type: ignore[arg-type]
        message = str(excinfo.value)
        assert "turn_deadline" in message
        assert "seconds" in message

    def test_a_deadline_of_zero_is_refused(self, mock_gateway):
        with pytest.raises(ValueError, match="turn_deadline"):
            HeadlessChat(turn_deadline=0, gateway=mock_gateway)

    def test_a_negative_deadline_is_refused(self, mock_gateway):
        with pytest.raises(ValueError, match="turn_deadline"):
            HeadlessChat(turn_deadline=-1.0, gateway=mock_gateway)

    def test_a_round_budget_below_one_is_refused(self, mock_gateway):
        with pytest.raises(ValueError, match="max_tool_rounds"):
            HeadlessChat(turn_deadline=5.0, max_tool_rounds=0, gateway=mock_gateway)

    def test_an_explicit_none_policy_is_refused(self, mock_gateway):
        """``None`` is the one value that means "prompt an operator"."""
        with pytest.raises(ValueError) as excinfo:
            HeadlessChat(turn_deadline=5.0, approval_policy=None, gateway=mock_gateway)
        message = str(excinfo.value)
        assert "approval_policy" in message
        assert "NonInteractiveApprovalPolicy" in message

    def test_something_that_is_not_a_policy_is_refused(self, mock_gateway):
        with pytest.raises(TypeError) as excinfo:
            HeadlessChat(
                turn_deadline=5.0,
                approval_policy=lambda action: "a",  # type: ignore[arg-type]
                gateway=mock_gateway,
            )
        assert "ApprovalPolicy" in str(excinfo.value)

    def test_a_render_provider_is_not_a_parameter(self, mock_gateway):
        """A headless agent that could be handed a painting provider is not one."""
        from axiom.extensions.builtins.chat.providers.rich_render import RichRenderProvider

        with pytest.raises(TypeError, match="render"):
            HeadlessChat(
                turn_deadline=5.0,
                gateway=mock_gateway,
                render=RichRenderProvider(),  # type: ignore[call-arg]
            )

    def test_the_refusals_happen_at_construction_not_at_the_first_request(self, mock_gateway):
        """A misconfiguration that only shows up under load is the failure mode."""
        for build in (
            lambda: HeadlessChat(turn_deadline=None, gateway=mock_gateway),  # type: ignore[arg-type]
            lambda: HeadlessChat(turn_deadline=0.0, gateway=mock_gateway),
            lambda: HeadlessChat(turn_deadline=5.0, approval_policy=None, gateway=mock_gateway),
        ):
            with pytest.raises((TypeError, ValueError)):
                build()


# ---------------------------------------------------------------------------
# The terminal is untouched
# ---------------------------------------------------------------------------


class TestTheTerminalIsUntouched:
    def test_a_plain_agent_still_has_no_render_provider(self, mock_gateway, tmp_path):
        HeadlessChat(turn_deadline=5.0, gateway=mock_gateway)
        agent = ChatAgent(gateway=mock_gateway, bus=EventBus(), session=Session())
        assert agent._render is None

    def test_a_plain_agent_still_has_no_approval_policy(self, mock_gateway):
        HeadlessChat(turn_deadline=5.0, gateway=mock_gateway)
        agent = ChatAgent(gateway=mock_gateway, bus=EventBus(), session=Session())
        assert agent.approval_policy is None

    def test_a_plain_agent_still_has_no_time_bound(self, mock_gateway):
        agent = ChatAgent(gateway=mock_gateway, bus=EventBus(), session=Session())
        assert agent.scope.turn_deadline is None
        assert agent.scope.max_tool_rounds == MAX_TOOL_ROUNDS

    def test_a_plain_agent_still_loads_the_operators_permission_file(self, mock_gateway):
        agent = ChatAgent(gateway=mock_gateway, bus=EventBus(), session=Session())
        assert agent.scope.permissions.path is not None

    def test_the_bare_prompt_is_still_the_terminal_fallback(
        self, mock_gateway, monkeypatch, tmp_path
    ):
        """The leak stays open where it was left open on purpose."""
        HeadlessChat(turn_deadline=5.0, gateway=mock_gateway)
        asked: list[str] = []
        monkeypatch.setattr(builtins, "input", lambda prompt="": asked.append(prompt) or "a")

        agent = ChatAgent(gateway=mock_gateway, bus=EventBus(), session=Session())
        target = tmp_path / "yes.txt"
        agent._process_tool_calls(_write_call(target))

        assert asked, "the interactive prompt was not reached"
        assert target.read_text() == "hi"
