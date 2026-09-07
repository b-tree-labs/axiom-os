# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Declared authority (P5, step 1): every chat tool call consults ``decide()``.

Behaviour matrix for ``axiom.infra.authority``:

- envelope construction: registered intent, ``tool://<name>`` resource,
  actor parsed from ``@name:context``; malformed handles are rejected the
  way ``Principal`` rejects them.
- ``decide_tool_call`` with the default tool context (no site rules) →
  PERMIT / PROCEED with a receipt id; a *bare* ``DecideContext`` proposes
  (that is why the open rule exists).
- hook mapping: PROCEED → pass-through; ABORT → ``deny``; AWAIT_HUMAN /
  ENQUEUE_PROPOSAL → ``request_approval``; an exception → pass-through +
  WARNING (fail-open is deliberate for this step).
- registration is idempotent and runs after memory_dedup.
- end-to-end through ``dispatch_tool`` and the chat agent path, with a
  refusal journaled in the action ledger.
- the ``no_action_without_authz`` lint passes on the module and would
  fail without the ``decide`` call.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.authz.decide import DecideContext
from axiom.extensions.builtins.authz.rules import Rule
from axiom.governance import (
    ActionEnvelope,
    Classification,
    Decision,
    IntentPattern,
    NextAction,
    ResourcePattern,
    Verdict,
)
from axiom.infra import authority
from axiom.infra.bus import EventBus
from axiom.infra.hooks import HookBus, HookDenied, HookSpec, set_default_hookbus

_MEMORY_MANIFEST = (
    Path(authority.__file__).resolve().parents[1]
    / "extensions"
    / "builtins"
    / "memory"
    / "axiom-extension.toml"
)
_AUTHZ_MANIFEST = (
    Path(authority.__file__).resolve().parents[1]
    / "extensions"
    / "builtins"
    / "authz"
    / "axiom-extension.toml"
)


@pytest.fixture
def no_db_context(monkeypatch):
    """A default tool context with no receipt session (never touches a DB)."""
    ctx = authority.build_tool_context(session_factory=None)
    monkeypatch.setattr(authority, "_default_ctx", ctx)
    return ctx


@pytest.fixture
def isolated_buses():
    hookbus = HookBus()
    eventbus = EventBus()
    set_default_hookbus(hookbus)
    yield hookbus, eventbus
    set_default_hookbus(None)


@pytest.fixture
def jsonl_ledger(tmp_path, monkeypatch):
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    action_ledger._reset_backend_cache()
    yield tmp_path
    action_ledger._reset_backend_cache()


def _deny_rule(tool_name: str, *, disposition: str = "deny") -> Rule:
    return Rule(
        name=f"site_{disposition}_{tool_name}",
        intent_pattern=IntentPattern(authority.TOOL_INVOKE_INTENT),
        actor_pattern="*",
        resource_pattern=ResourcePattern(f"tool://{tool_name}"),
        disposition=disposition,  # type: ignore[arg-type]
    )


def _verdict(decision: Decision, reason: str = "because") -> Verdict:
    return Verdict.from_decision(decision, reason, "authz-test-receipt")


# ---------------------------------------------------------------------------
# build_tool_envelope
# ---------------------------------------------------------------------------


class TestBuildToolEnvelope:
    def test_produces_a_valid_registered_envelope(self):
        env = authority.build_tool_envelope(
            "read_file", {"file_path": "/tmp/x"}, "@alice:axiom", ext_origin="chat"
        )
        assert isinstance(env, ActionEnvelope)
        assert env.intent.value == authority.TOOL_INVOKE_INTENT
        assert env.intent.is_registered()
        assert str(env.resource) == "tool://read_file"
        assert env.resource.scheme == authority.TOOL_RESOURCE_SCHEME
        assert env.resource.identifier == "read_file"
        assert env.actor.handle == "@alice:axiom"
        assert env.actor.name == "alice"
        assert env.actor.context == "axiom"
        assert env.classification is Classification.INTERNAL
        assert env.context_fragment_id == "extension://chat"
        assert env.is_local
        assert env.dedup_key

    def test_strict_construction_accepts_the_intent(self):
        # ``strict=True`` is the lint's runtime twin: an unregistered intent
        # would raise here.
        env = authority.build_tool_envelope("search", {}, "@alice:axiom", strict=True)
        assert env.strict is True

    def test_classification_is_passed_through(self):
        env = authority.build_tool_envelope(
            "search", {}, "@alice:axiom", classification="regulated"
        )
        assert env.classification is Classification.REGULATED

    def test_unknown_classification_is_rejected(self):
        with pytest.raises(ValueError):
            authority.build_tool_envelope("search", {}, "@alice:axiom", classification="nope")

    @pytest.mark.parametrize("handle", ["alice@axiom", "@alice@axiom", "@", "", "@bad handle"])
    def test_malformed_principal_is_rejected(self, handle):
        with pytest.raises(ValueError):
            authority.build_tool_envelope("search", {}, handle)

    def test_bare_name_is_promoted_to_a_handle(self):
        # The platform's own helper (``governance.simple``) prepends ``@``.
        env = authority.build_tool_envelope("search", {}, "alice:axiom")
        assert env.actor.handle == "@alice:axiom"

    def test_tool_name_is_sanitised_into_the_resource(self):
        env = authority.build_tool_envelope("  my tool ", {}, "@alice:axiom")
        assert str(env.resource) == "tool://my_tool"

    def test_empty_tool_name_is_rejected(self):
        with pytest.raises(ValueError):
            authority.build_tool_envelope("   ", {}, "@alice:axiom")

    def test_each_call_gets_a_fresh_dedup_key(self):
        a = authority.build_tool_envelope("search", {}, "@alice:axiom")
        b = authority.build_tool_envelope("search", {}, "@alice:axiom")
        assert a.dedup_key != b.dedup_key


# ---------------------------------------------------------------------------
# decide_tool_call + default context
# ---------------------------------------------------------------------------


class TestDecideToolCall:
    def test_default_context_permits_with_a_receipt(self, no_db_context):
        env = authority.build_tool_envelope("read_file", {}, "@alice:axiom")
        verdict = authority.decide_tool_call(env)
        assert verdict.decision is Decision.PERMIT
        assert verdict.next_action_for_caller is NextAction.PROCEED
        assert verdict.receipt_fragment_id.startswith("authz-")

    def test_explicit_context_is_honoured(self):
        ctx = authority.build_tool_context(session_factory=None)
        env = authority.build_tool_envelope("read_file", {}, "@alice:axiom")
        assert authority.decide_tool_call(env, ctx).next_action_for_caller is NextAction.PROCEED

    def test_open_rule_is_the_only_rule_and_names_itself_in_the_receipt(self, no_db_context):
        names = [r.name for r in no_db_context.rule_engine.rules]
        assert names == [authority.OPEN_RULE_NAME]
        env = authority.build_tool_envelope("read_file", {}, "@alice:axiom")
        verdict = authority.decide_tool_call(env, no_db_context)
        assert authority.OPEN_RULE_NAME in verdict.reason

    def test_bare_context_proposes_which_is_why_the_open_rule_exists(self):
        # Documents the platform default this module compensates for: a novel
        # action with no matching rule is *proposed*, not permitted.
        env = authority.build_tool_envelope("read_file", {}, "@alice:axiom")
        verdict = authority.decide_tool_call(env, DecideContext())
        assert verdict.next_action_for_caller is NextAction.ENQUEUE_PROPOSAL

    def test_site_deny_rule_wins_over_the_open_rule(self, no_db_context):
        no_db_context.add_rule(_deny_rule("write_file"))
        env = authority.build_tool_envelope("write_file", {}, "@alice:axiom")
        verdict = authority.decide_tool_call(env, no_db_context)
        assert verdict.next_action_for_caller is NextAction.ABORT
        # Other tools are untouched.
        other = authority.build_tool_envelope("read_file", {}, "@alice:axiom")
        assert authority.decide_tool_call(other, no_db_context).next_action_for_caller is (
            NextAction.PROCEED
        )

    def test_default_context_is_process_wide(self, monkeypatch):
        monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
        authority._reset_default_tool_context()
        try:
            first = authority.default_tool_context()
            assert first is authority.default_tool_context()
            assert first.session_factory is None
        finally:
            authority._reset_default_tool_context()

    def test_receipt_session_is_best_effort_when_db_unreachable(self, monkeypatch):
        monkeypatch.delenv("AXIOM_AUTHORITY_RECEIPTS", raising=False)
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://nobody:nobody@127.0.0.1:1/none")
        authority._reset_default_tool_context()
        try:
            ctx = authority.default_tool_context()
            assert ctx.session_factory is None
            env = authority.build_tool_envelope("read_file", {}, "@alice:axiom")
            assert authority.decide_tool_call(env).next_action_for_caller is NextAction.PROCEED
        finally:
            authority._reset_default_tool_context()


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def _payload(tool_name="read_file", principal="@alice:axiom", **extra):
    payload = {
        "tool_name": tool_name,
        "args": {"file_path": "/tmp/x"},
        "principal": principal,
        "classification": "",
        "ext_origin": "chat",
    }
    payload.update(extra)
    return payload


class TestAuthorityHook:
    def test_proceed_passes_through(self, no_db_context):
        assert authority.authority_pre_invoke_hook(_payload(), "@alice:axiom") is None

    def test_abort_maps_to_deny_with_the_reason(self, monkeypatch):
        monkeypatch.setattr(
            authority, "decide_tool_call", lambda env, ctx=None: _verdict(Decision.DENY, "nope")
        )
        result = authority.authority_pre_invoke_hook(_payload(), "@alice:axiom")
        assert result is not None
        assert result.decision == "deny"
        assert result.reason == "nope"

    @pytest.mark.parametrize("decision", [Decision.PROPOSE_TO_HUMAN])
    def test_proposal_maps_to_request_approval(self, monkeypatch, decision):
        monkeypatch.setattr(
            authority, "decide_tool_call", lambda env, ctx=None: _verdict(decision, "ask")
        )
        result = authority.authority_pre_invoke_hook(_payload(), "@alice:axiom")
        assert result is not None
        assert result.decision == "approval_required"
        assert result.reason == "ask"
        assert result.approval_token == "authz-test-receipt"

    def test_await_human_maps_to_request_approval(self):
        verdict = Verdict(
            decision=Decision.PROPOSE_TO_HUMAN,
            reason="wait",
            receipt_fragment_id="authz-r",
            next_action_for_caller=NextAction.AWAIT_HUMAN,
        )
        result = authority.verdict_to_hook_result(verdict)
        assert result is not None
        assert result.decision == "approval_required"
        assert result.reason == "wait"

    def test_expired_capability_aborts(self):
        result = authority.verdict_to_hook_result(_verdict(Decision.EXPIRED_CAPABILITY, "old"))
        assert result is not None and result.decision == "deny"

    def test_exception_in_decide_passes_through_with_a_warning(self, monkeypatch, caplog):
        def boom(env, ctx=None):
            raise RuntimeError("engine on fire")

        monkeypatch.setattr(authority, "decide_tool_call", boom)
        with caplog.at_level(logging.WARNING, logger="axiom.infra.authority"):
            assert authority.authority_pre_invoke_hook(_payload(), "@alice:axiom") is None
        assert any("engine on fire" in rec.getMessage() for rec in caplog.records)
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_malformed_principal_passes_through_with_a_warning(self, no_db_context, caplog):
        with caplog.at_level(logging.WARNING, logger="axiom.infra.authority"):
            result = authority.authority_pre_invoke_hook(
                _payload(principal="alice@axiom"), "alice@axiom"
            )
        assert result is None
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_empty_principal_falls_back_to_the_open_local_principal(self, no_db_context):
        seen: list[ActionEnvelope] = []

        def spy(env, ctx=None):
            seen.append(env)
            return _verdict(Decision.PERMIT)

        from axiom.infra.principal import local_handle

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(authority, "decide_tool_call", spy)
            assert authority.authority_pre_invoke_hook(_payload(principal=""), "") is None
        assert seen and seen[0].actor.handle == local_handle()

    def test_handler_adapter_returns_allow_on_pass_through(self, no_db_context):
        from axiom.infra.hooks import HookContext

        ctx = HookContext(event="tool.pre_invoke", payload=_payload(), principal="@alice:axiom")
        result = authority.pre_invoke_handler(ctx)
        assert result.decision == "allow"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registering_twice_leaves_one_hook(self):
        bus = HookBus()
        first = authority.register_authority_hook(bus)
        second = authority.register_authority_hook(bus)
        assert first is second
        ours = [
            s for s in bus.hooks_for("tool.pre_invoke") if s.entry is authority.pre_invoke_handler
        ]
        assert len(ours) == 1
        assert ours[0].fail_mode == "warn"
        assert ours[0].source == authority.AUTHORITY_HOOK_SOURCE

    def test_registers_on_the_default_bus_when_none_given(self, isolated_buses):
        hookbus, _ = isolated_buses
        authority.register_authority_hook()
        authority.register_authority_hook()
        entries = [s.entry for s in hookbus.hooks_for("tool.pre_invoke")]
        assert entries.count(authority.pre_invoke_handler) == 1

    def test_runs_after_memory_dedup(self):
        from axiom.policy.memory_dedup_hook import pre_invoke_handler as dedup_handler

        manifest = tomllib.loads(_MEMORY_MANIFEST.read_text(encoding="utf-8"))
        dedup = next(
            p
            for p in manifest["extension"]["provides"]
            if p.get("kind") == "hook" and "tool.pre_invoke" in p.get("events", [])
        )
        bus = HookBus()
        # Register authority FIRST so ordering is by priority, not insertion.
        authority.register_authority_hook(bus)
        bus.register(
            HookSpec(
                event="tool.pre_invoke",
                entry=dedup_handler,
                priority=dedup["priority"],
                fail_mode=dedup["fail_mode"],
                source="memory",
            )
        )
        ordered = [s.entry for s in bus.hooks_for("tool.pre_invoke")]
        assert ordered.index(dedup_handler) < ordered.index(authority.pre_invoke_handler)
        assert dedup["priority"] < authority.AUTHORITY_HOOK_PRIORITY
        # ... and after any manifest hook left at the AEOS default priority.
        assert authority.AUTHORITY_HOOK_PRIORITY > 100

    def test_authz_manifest_declares_the_hook(self):
        from axiom.extensions.contracts import parse_manifest
        from axiom.infra.hooks.registry import discover_manifest_hooks

        ext = parse_manifest(_AUTHZ_MANIFEST)
        interceptors, _observers = discover_manifest_hooks([ext])
        ours = [s for s in interceptors if s.entry is authority.pre_invoke_handler]
        assert len(ours) == 1
        assert ours[0].event == "tool.pre_invoke"
        assert ours[0].priority == authority.AUTHORITY_HOOK_PRIORITY
        assert ours[0].fail_mode == "warn"


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_tool_still_runs_with_no_site_rules(self, isolated_buses, no_db_context):
        from axiom.infra.tool_gateway import dispatch_tool

        hookbus, eventbus = isolated_buses
        authority.register_authority_hook(hookbus)
        calls: list[tuple[str, dict]] = []

        def fake_dispatcher(name, args):
            calls.append((name, dict(args)))
            return {"ok": True}

        result = dispatch_tool(
            tool_name="read_file",
            args={"file_path": "/tmp/x"},
            principal="@alice:axiom",
            hookbus=hookbus,
            eventbus=eventbus,
            dispatcher=fake_dispatcher,
            ext_origin="chat",
        )
        assert result == {"ok": True}
        assert calls == [("read_file", {"file_path": "/tmp/x"})]

    def test_site_deny_rule_raises_hook_denied_before_dispatch(self, isolated_buses, no_db_context):
        from axiom.infra.tool_gateway import dispatch_tool

        hookbus, eventbus = isolated_buses
        authority.register_authority_hook(hookbus)
        no_db_context.add_rule(_deny_rule("write_file"))
        calls: list[str] = []

        def fake_dispatcher(name, args):
            calls.append(name)
            return {"ok": True}

        with pytest.raises(HookDenied) as info:
            dispatch_tool(
                tool_name="write_file",
                args={"file_path": "/tmp/x", "content": "hi"},
                principal="@alice:axiom",
                hookbus=hookbus,
                eventbus=eventbus,
                dispatcher=fake_dispatcher,
                ext_origin="chat",
            )
        assert calls == []
        assert "site_deny_write_file" in info.value.reason

    def test_site_propose_rule_raises_approval_required(self, isolated_buses, no_db_context):
        from axiom.infra.hooks import ApprovalRequired
        from axiom.infra.tool_gateway import dispatch_tool

        hookbus, eventbus = isolated_buses
        authority.register_authority_hook(hookbus)
        no_db_context.add_rule(_deny_rule("write_file", disposition="propose"))

        with pytest.raises(ApprovalRequired) as info:
            dispatch_tool(
                tool_name="write_file",
                args={},
                principal="@alice:axiom",
                hookbus=hookbus,
                eventbus=eventbus,
                dispatcher=lambda n, a: {"ok": True},
            )
        assert info.value.token.startswith("authz-")

    def test_chat_path_records_the_refusal_in_the_action_ledger(
        self, isolated_buses, no_db_context, jsonl_ledger
    ):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
        from axiom.infra.orchestrator.session import Session
        from axiom.policy.action_ledger import ActionLedger

        no_db_context.add_rule(_deny_rule("read_file"))

        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"
        agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session(principal_id="@alice:axiom"))
        agent.set_render_provider(MagicMock())

        response = CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name="read_file", input={"file_path": "/tmp/x"})],
            provider="test",
            success=True,
        )
        results = agent._process_tool_calls(response)

        assert len(results) == 1
        _tid, _name, result = results[0]
        assert result["error"].startswith("denied by hook: ")
        assert "site_deny_read_file" in result["error"]

        rows = ActionLedger(state_dir=jsonl_ledger).query(outcome="refused")
        assert len(rows) == 1
        row = rows[0]
        assert row["agent"] == "chat"
        assert row["op_class"] == authority.TOOL_INVOKE_INTENT
        assert row["name"] == "read_file"
        assert row["candidate"] == "tool://read_file"
        assert "site_deny_read_file" in row["refusing_rule"]
        assert row["metadata"]["principal"] == "@alice:axiom"
        assert row["metadata"]["arg_keys"] == ["file_path"]
        assert "content" not in str(row["metadata"])

    def test_chat_path_registers_the_hook_on_the_default_bus(self, isolated_buses, no_db_context):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
        from axiom.infra.orchestrator.session import Session

        hookbus, _ = isolated_buses
        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"
        agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session())
        agent.set_render_provider(MagicMock())
        response = CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
            provider="test",
            success=True,
        )
        agent._process_tool_calls(response)
        entries = [s.entry for s in hookbus.hooks_for("tool.pre_invoke")]
        assert entries.count(authority.pre_invoke_handler) == 1


class TestRecordToolRefusal:
    def test_never_raises(self, monkeypatch):
        from axiom.policy import action_ledger

        def boom(*a, **k):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(action_ledger, "ActionLedger", boom)
        authority.record_tool_refusal(tool_name="x", principal="@a:b", reason="r", args={"k": 1})


# ---------------------------------------------------------------------------
# Lint: no_action_without_authz
# ---------------------------------------------------------------------------


class TestAuthzLint:
    def test_module_passes_the_lint(self):
        from axiom.extensions.builtins.authz.lint import check_paths

        report = check_paths([Path(authority.__file__)])
        assert report.ok, report.violations
        assert report.allowlisted == []
        assert report.checked_functions >= 1

    def test_removing_the_decide_call_fails_the_lint(self, tmp_path):
        from axiom.extensions.builtins.authz.lint import check_source

        source = Path(authority.__file__).read_text(encoding="utf-8")
        assert "return decide(" in source
        mutated = source.replace("return decide(", "return _not_a_decision(")
        report = check_source(mutated, tmp_path / "authority_mutated.py")
        assert not report.ok
        assert [v.function for v in report.violations] == ["decide_tool_call"]
