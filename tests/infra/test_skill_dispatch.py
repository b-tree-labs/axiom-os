# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Declared authority (P5, step 2): CLI + MCP capability dispatch through the gateway.

Behaviour matrix for ``axiom.infra.skill_dispatch.invoke_capability``:

- a permitted capability runs and its ``SkillResult`` comes back untouched;
- a ``tool.pre_invoke`` hook that denies turns into ``ok=False`` carrying the
  hook source and the reason, the skill body never runs, and the refusal is
  journaled through ``authority.record_tool_refusal``;
- ``ApprovalRequired`` is a refusal result too: an MCP handler and a
  non-interactive CLI cannot prompt, so a hang is not an option;
- ``tool.post_invoke`` fires on the process default bus when no bus is
  named, on both the success and the failure path, and not at all when a
  caller explicitly passes ``eventbus=None`` or a hook denied the call;
- its payload is exactly ``TELEMETRY_FIELDS``: the capability name, the
  principal, the surface, the outcome and the latency, plus an
  ``args_digest`` equal to the action audit's own. No argument value and no
  result value reaches it, which is what keeps a credential verb's
  telemetry off a durable log;
- a subscriber that raises, and a bus whose publish raises, leave the tool
  call and its result untouched on either path;
- the gateway ``tool_name`` is the capability name, identical from
  ``surface="cli"`` and ``surface="mcp"`` (the one-identity claim: a site
  rule written against ``tool://press.draft`` covers every surface);
- ``allow_modified`` splices args before the skill sees them;
- ``ctx=None`` is accepted and the call carries the open principal;
- ``ext_origin`` is the capability namespace;
- an unregistered capability still raises ``KeyError``, as
  ``SkillRegistry.invoke`` always did;
- the authority hook is registered on whichever bus the call uses, so the
  GUARD consult actually happens on these surfaces;
- the module passes the ``no_action_without_authz`` lint.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.infra import authority
from axiom.infra.bus import EventBus
from axiom.infra.hooks import HookBus, HookSpec, allow, allow_modified, deny, request_approval
from axiom.infra.principal import PrincipalContext, open_principal
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult

CAPABILITY = "press.draft"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep the refusal ledger inside the test's tmp dir, never ``~/.axi``."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")


@pytest.fixture
def hookbus() -> HookBus:
    return HookBus()


@pytest.fixture
def eventbus() -> EventBus:
    return EventBus()


@pytest.fixture
def no_db_context(monkeypatch):
    """Authority decisions with no receipt session (never touches a DB)."""
    ctx = authority.build_tool_context(session_factory=None)
    monkeypatch.setattr(authority, "_default_ctx", ctx)
    return ctx


class Spy:
    """A skill body that records every invocation it actually ran."""

    def __init__(self, result: SkillResult | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result if result is not None else SkillResult(ok=True, value="ran")

    def __call__(self, params, ctx) -> SkillResult:
        self.calls.append(dict(params))
        return self.result

    @property
    def ran(self) -> bool:
        return bool(self.calls)


def _registry(spy: Spy, name: str = CAPABILITY) -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(name, spy)
    return reg


def _ctx(reg: SkillRegistry, tmp_path: Path, *, handle: str = "@tester:axiom") -> SkillContext:
    return SkillContext(
        registry=reg,
        state_dir=tmp_path,
        logger=logging.getLogger("test.skill_dispatch"),
        user_prompt=None,
        principal=PrincipalContext(handle=handle),
    )


def _record_hook(sink: list[dict], decision=None) -> HookSpec:
    """A ``tool.pre_invoke`` hook that records the payload it was handed."""

    def hook(ctx):
        sink.append(dict(ctx.payload))
        return decision() if decision is not None else allow()

    return HookSpec(
        event="tool.pre_invoke", entry=hook, priority=10, fail_mode="abort", source="test"
    )


def _verdict_hook(decision) -> HookSpec:
    return HookSpec(
        event="tool.pre_invoke",
        entry=lambda ctx: decision(),
        priority=10,
        fail_mode="abort",
        source="test",
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestPermittedDispatch:
    def test_returns_the_skill_result_object_untouched(self, tmp_path, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        expected = SkillResult(ok=True, value={"drafted": 3}, actions_taken=["wrote x"])
        spy = Spy(expected)
        reg = _registry(spy)

        out = invoke_capability(
            reg,
            CAPABILITY,
            {"source": "doc.md"},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
        )

        assert out is expected
        assert spy.calls == [{"source": "doc.md"}]

    def test_unknown_capability_still_raises_key_error(self, tmp_path, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        reg = _registry(Spy())
        with pytest.raises(KeyError):
            invoke_capability(
                reg, "press.nope", {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
            )


# ---------------------------------------------------------------------------
# One identity across surfaces
# ---------------------------------------------------------------------------


class TestOneIdentity:
    def test_cli_and_mcp_produce_the_same_gateway_tool_name(self, tmp_path, hookbus, no_db_context):
        """A site rule on ``tool://press.draft`` must cover both surfaces."""
        from axiom.infra.skill_dispatch import invoke_capability

        seen: list[dict] = []
        hookbus.register(_record_hook(seen))
        reg = _registry(Spy())

        for surface in ("cli", "mcp"):
            invoke_capability(
                reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface=surface, hookbus=hookbus
            )

        assert [p["tool_name"] for p in seen] == [CAPABILITY, CAPABILITY]

    def test_the_tool_name_is_never_the_mcp_mangled_name(self, tmp_path, hookbus, no_db_context):
        from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name
        from axiom.infra.skill_dispatch import invoke_capability

        seen: list[dict] = []
        hookbus.register(_record_hook(seen))
        reg = _registry(Spy())

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="mcp", hookbus=hookbus)

        assert seen[0]["tool_name"] != mcp_tool_name(CAPABILITY)

    def test_ext_origin_is_the_capability_namespace(self, tmp_path, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        seen: list[dict] = []
        hookbus.register(_record_hook(seen))
        reg = _registry(Spy())

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus)

        assert seen[0]["ext_origin"] == "press"


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


class TestPrincipal:
    def test_ctx_principal_handle_is_what_the_gateway_receives(
        self, tmp_path, hookbus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        seen: list[dict] = []
        hookbus.register(_record_hook(seen))
        reg = _registry(Spy())

        invoke_capability(
            reg,
            CAPABILITY,
            {},
            _ctx(reg, tmp_path, handle="@operator:site"),
            surface="cli",
            hookbus=hookbus,
        )

        assert seen[0]["principal"] == "@operator:site"

    def test_none_ctx_is_accepted_and_uses_the_open_principal(self, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        seen: list[dict] = []
        hookbus.register(_record_hook(seen))
        spy = Spy()
        reg = _registry(spy)

        out = invoke_capability(reg, CAPABILITY, {"a": 1}, None, surface="cli", hookbus=hookbus)

        assert out.ok
        assert spy.ran
        assert seen[0]["principal"] == open_principal().handle


# ---------------------------------------------------------------------------
# Hook outcomes become results
# ---------------------------------------------------------------------------


class TestDenied:
    def test_deny_yields_a_failed_result_and_the_skill_never_runs(
        self, tmp_path, hookbus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        hookbus.register(_verdict_hook(lambda: deny(reason="site policy forbids drafts")))
        spy = Spy()
        reg = _registry(spy)

        out = invoke_capability(
            reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        assert out.ok is False
        assert spy.ran is False
        assert any("site policy forbids drafts" in e for e in out.errors)

    def test_the_error_names_the_hook_source(self, tmp_path, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        hookbus.register(_verdict_hook(lambda: deny(reason="nope")))
        reg = _registry(Spy())

        out = invoke_capability(
            reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        # ``dispatch_tool`` stamps the denial with ``ext_origin`` as its source.
        assert any("press" in e for e in out.errors)

    def test_the_refusal_is_journaled(self, tmp_path, hookbus, monkeypatch, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        recorded: list[dict] = []
        monkeypatch.setattr(authority, "record_tool_refusal", lambda **kw: recorded.append(kw))
        hookbus.register(_verdict_hook(lambda: deny(reason="denied by site")))
        reg = _registry(Spy())

        invoke_capability(
            reg,
            CAPABILITY,
            {"source": "doc.md"},
            _ctx(reg, tmp_path),
            surface="mcp",
            hookbus=hookbus,
        )

        assert len(recorded) == 1
        assert recorded[0]["tool_name"] == CAPABILITY
        assert recorded[0]["surface"] == "mcp"
        assert recorded[0]["reason"] == "denied by site"
        assert recorded[0]["args"] == {"source": "doc.md"}


class TestApprovalRequired:
    def test_becomes_a_refusal_result_rather_than_a_raise(self, tmp_path, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        hookbus.register(_verdict_hook(lambda: request_approval(why="a human must sign off")))
        spy = Spy()
        reg = _registry(spy)

        out = invoke_capability(
            reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="mcp", hookbus=hookbus
        )

        assert out.ok is False
        assert spy.ran is False
        assert any("a human must sign off" in e for e in out.errors)

    def test_the_message_says_the_surface_cannot_prompt(self, tmp_path, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        hookbus.register(_verdict_hook(lambda: request_approval(why="sign off")))
        reg = _registry(Spy())

        out = invoke_capability(
            reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        joined = " ".join(out.errors)
        assert "approval required" in joined
        assert "cli" in joined


# ---------------------------------------------------------------------------
# Arg splicing
# ---------------------------------------------------------------------------


class TestAllowModified:
    def test_modified_args_reach_the_skill(self, tmp_path, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        hookbus.register(_verdict_hook(lambda: allow_modified(args={"source": "redacted.md"})))
        spy = Spy()
        reg = _registry(spy)

        invoke_capability(
            reg,
            CAPABILITY,
            {"source": "secret.md"},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
        )

        assert spy.calls == [{"source": "redacted.md"}]


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class TestPostInvoke:
    def test_post_invoke_carries_the_capability_name(
        self, tmp_path, hookbus, eventbus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        seen: list[dict] = []
        eventbus.subscribe("tool.post_invoke", lambda subject, payload: seen.append(payload))
        reg = _registry(Spy())

        invoke_capability(
            reg,
            CAPABILITY,
            {},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
            eventbus=eventbus,
        )

        assert len(seen) == 1
        assert seen[0]["tool_name"] == CAPABILITY

    def test_post_invoke_summarises_the_result_rather_than_carrying_it(
        self, tmp_path, hookbus, eventbus, no_db_context
    ):
        """The gateway gets the dict projection; the bus gets a summary of it.

        This test used to assert the projection reached the bus verbatim.
        It does not any more, on purpose: ``value`` and ``actions_taken`` are
        where a credential verb returns a minted token, and this bus can be
        wired to a durable log. See ``TestTelemetryPayloadIsNarrow``.
        """
        from axiom.infra.skill_dispatch import invoke_capability

        seen: list[dict] = []
        eventbus.subscribe("tool.post_invoke", lambda subject, payload: seen.append(payload))
        spy = Spy(SkillResult(ok=True, value={"n": 1}, actions_taken=["wrote x"]))
        reg = _registry(spy)

        invoke_capability(
            reg,
            CAPABILITY,
            {},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
            eventbus=eventbus,
        )

        assert "result" not in seen[0]
        assert seen[0]["ok"] is True
        assert seen[0]["errors_count"] == 0


# ---------------------------------------------------------------------------
# The GUARD consult is actually wired on these surfaces
# ---------------------------------------------------------------------------


class TestAuthorityWiring:
    def test_the_authority_hook_is_registered_on_the_bus_the_call_uses(
        self, tmp_path, hookbus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        assert hookbus.hooks_for("tool.pre_invoke") == []
        reg = _registry(Spy())

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus)

        entries = [spec.entry for spec in hookbus.hooks_for("tool.pre_invoke")]
        assert authority.pre_invoke_handler in entries

    def test_registration_is_idempotent_across_calls(self, tmp_path, hookbus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        reg = _registry(Spy())
        for _ in range(3):
            invoke_capability(
                reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
            )

        assert len(hookbus.hooks_for("tool.pre_invoke")) == 1

    def test_a_site_deny_rule_refuses_the_capability(self, tmp_path, hookbus, monkeypatch):
        """The point of the chokepoint: one rule, and the CLI surface obeys it."""
        from axiom.extensions.builtins.authz.rules import Rule
        from axiom.governance import IntentPattern, ResourcePattern
        from axiom.infra.skill_dispatch import invoke_capability

        decide_ctx = authority.build_tool_context(session_factory=None)
        decide_ctx.add_rule(
            Rule(
                name="site_deny_press_draft",
                intent_pattern=IntentPattern(authority.TOOL_INVOKE_INTENT),
                actor_pattern="*",
                resource_pattern=ResourcePattern(f"tool://{CAPABILITY}"),
                disposition="deny",
            )
        )
        monkeypatch.setattr(authority, "_default_ctx", decide_ctx)

        spy = Spy()
        reg = _registry(spy)
        out = invoke_capability(
            reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        assert out.ok is False
        assert spy.ran is False


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


class TestAuthzLint:
    def test_module_passes_no_action_without_authz(self):
        from axiom.extensions.builtins.authz.lint import check_paths
        from axiom.infra import skill_dispatch

        report = check_paths([Path(skill_dispatch.__file__)])
        assert report.ok, report.violations
        assert report.allowlisted == []


# ---------------------------------------------------------------------------
# The chokepoint stays a chokepoint
# ---------------------------------------------------------------------------


SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _direct_registry_invokes() -> dict[str, list[tuple[int, str]]]:
    """Every direct ``<something>.invoke(...)`` under ``src/axiom``.

    Keyed by path relative to ``src/``, valued by ``(lineno, receiver)``.
    Test trees are skipped: a test may reach the registry directly, that is
    the level it is testing at.
    """
    import ast

    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted((SRC_ROOT / "axiom").rglob("*.py")):
        parts = path.parts
        if "__pycache__" in parts or "tests" in parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or fn.attr != "invoke":
                continue
            rel = path.relative_to(SRC_ROOT).as_posix()
            found.setdefault(rel, []).append((node.lineno, ast.unparse(fn.value)))
    return found


class TestNoNewDirectRegistryInvoke:
    """A new CLI or MCP verb must not reopen the bypass this item closed."""

    def test_every_direct_invoke_is_on_the_documented_exclusion_list(self):
        from axiom.infra.skill_dispatch import DIRECT_INVOKE_EXEMPT

        found = _direct_registry_invokes()
        unexpected = {rel: sites for rel, sites in found.items() if rel not in DIRECT_INVOKE_EXEMPT}
        assert not unexpected, (
            "direct SkillRegistry.invoke outside the chokepoint:\n"
            + "\n".join(
                f"  {rel}:{lineno}  {receiver}.invoke(...)"
                for rel, sites in sorted(unexpected.items())
                for lineno, receiver in sites
            )
            + "\n\nRoute it through axiom.infra.skill_dispatch.invoke_capability, or, "
            "if it is composition inside one action, add it to DIRECT_INVOKE_EXEMPT "
            "with the reason."
        )

    def test_the_exclusion_list_has_no_stale_entries(self):
        """An entry that no longer has a direct call is a claim gone false."""
        from axiom.infra.skill_dispatch import DIRECT_INVOKE_EXEMPT

        found = _direct_registry_invokes()
        stale = sorted(set(DIRECT_INVOKE_EXEMPT) - set(found))
        assert not stale, (
            f"DIRECT_INVOKE_EXEMPT names paths with no direct invoke left: {stale}. "
            "Drop the entries."
        )

    def test_every_exclusion_carries_a_reason(self):
        from axiom.infra.skill_dispatch import DIRECT_INVOKE_EXEMPT

        assert all(reason.strip() for reason in DIRECT_INVOKE_EXEMPT.values())

    def test_the_chokepoint_itself_is_the_only_infra_exemption(self):
        """Nothing in ``axiom/infra`` reaches the registry except the chokepoint."""
        from axiom.infra.skill_dispatch import DIRECT_INVOKE_EXEMPT

        infra = sorted(p for p in DIRECT_INVOKE_EXEMPT if p.startswith("axiom/infra/"))
        assert infra == ["axiom/infra/skill_dispatch.py"]

    def test_no_cli_module_invokes_the_registry_directly(self):
        """Every ``cli.py`` verb dispatcher is routed, not a chosen few."""
        found = _direct_registry_invokes()
        cli_modules = sorted(rel for rel in found if rel.endswith("/cli.py"))
        assert cli_modules == []

    def test_a_regression_would_be_caught(self):
        """The detector actually sees a direct call (guard against a no-op guard)."""
        import ast

        source = "def verb(ctx):\n    return ctx.registry.invoke('x.y', {}, ctx)\n"
        tree = ast.parse(source)
        hits = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "invoke"
        ]
        assert len(hits) == 1


# ---------------------------------------------------------------------------
# Post-invoke telemetry on the CLI + MCP surfaces
# ---------------------------------------------------------------------------


@pytest.fixture
def process_bus():
    """Pin a fresh bus as the process default and hand it back."""
    from axiom.infra.bus import set_default_eventbus

    bus = EventBus()
    set_default_eventbus(bus)
    try:
        yield bus
    finally:
        set_default_eventbus(None)


def _sink(bus: EventBus) -> list[dict]:
    seen: list[dict] = []
    bus.subscribe("tool.post_invoke", lambda subject, payload: seen.append(payload))
    return seen


class TestTelemetryIsOnByDefault:
    """Omitting ``eventbus`` publishes on the process default bus."""

    def test_a_cli_call_publishes_exactly_one_post_invoke_event(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus)

        assert len(seen) == 1
        assert seen[0]["tool_name"] == CAPABILITY

    def test_an_mcp_call_publishes_exactly_one_post_invoke_event(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="mcp", hookbus=hookbus)

        assert len(seen) == 1
        assert seen[0]["tool_name"] == CAPABILITY

    def test_the_event_names_the_surface_it_came_from(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        for surface in ("cli", "mcp"):
            invoke_capability(
                reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface=surface, hookbus=hookbus
            )

        assert [p["surface"] for p in seen] == ["cli", "mcp"]

    def test_the_event_carries_the_capability_name_not_the_mangled_one(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """The identity in the telemetry is the identity a site rule is written against."""
        from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="mcp", hookbus=hookbus)

        assert seen[0]["tool_name"] == CAPABILITY
        assert seen[0]["tool_name"] != mcp_tool_name(CAPABILITY)

    def test_an_explicit_none_bus_still_publishes_nothing(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """``eventbus=None`` is a deliberate opt-out, not "unset"."""
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        out = invoke_capability(
            reg,
            CAPABILITY,
            {},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
            eventbus=None,
        )

        assert out.ok
        assert seen == []

    def test_an_explicit_bus_wins_over_the_process_default(
        self, tmp_path, hookbus, eventbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        default_seen = _sink(process_bus)
        explicit_seen = _sink(eventbus)
        reg = _registry(Spy())

        invoke_capability(
            reg,
            CAPABILITY,
            {},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
            eventbus=eventbus,
        )

        assert len(explicit_seen) == 1
        assert default_seen == []

    def test_the_process_default_bus_is_bounded(self):
        """The precondition that made defaulting to it safe."""
        from axiom.infra.bus import (
            DEFAULT_HISTORY_LIMIT,
            get_default_eventbus,
            set_default_eventbus,
        )

        set_default_eventbus(None)
        try:
            assert get_default_eventbus().history_limit == DEFAULT_HISTORY_LIMIT
            assert DEFAULT_HISTORY_LIMIT is not None
        finally:
            set_default_eventbus(None)


class TestTelemetryPayloadIsNarrow:
    """The CLI/MCP payload carries identity and outcome, never content."""

    def test_the_payload_carries_exactly_the_declared_fields(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import TELEMETRY_FIELDS, invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(
            reg, CAPABILITY, {"a": 1}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        assert set(seen[0]) == set(TELEMETRY_FIELDS)

    def test_neither_an_argument_value_nor_a_result_value_reaches_the_payload(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """The whole point: a credential in the args or the result must not be observable."""
        import json

        from axiom.infra.skill_dispatch import invoke_capability

        arg_secret = "glpat-ARGUMENTSECRET0000"
        result_secret = "axk_RESULTSECRET0000"
        seen = _sink(process_bus)
        spy = Spy(
            SkillResult(
                ok=True,
                value={"token": result_secret},
                actions_taken=[f"minted {result_secret}"],
            )
        )
        reg = _registry(spy)

        invoke_capability(
            reg,
            CAPABILITY,
            {"value": arg_secret},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
        )

        rendered = json.dumps(seen[0], default=str)
        assert arg_secret not in rendered
        assert result_secret not in rendered

    def test_the_raw_args_and_result_keys_are_absent(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """Named explicitly so re-adding either key fails here, not in a review."""
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(
            reg, CAPABILITY, {"a": 1}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        assert "args" not in seen[0]
        assert "result" not in seen[0]

    def test_the_args_digest_is_the_one_the_action_audit_records(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """One invocation, one digest: the bus event correlates to the audit chain."""
        from axiom.infra.audit_trail import params_digest
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())
        params = {"source": "doc.md", "copies": 2}

        invoke_capability(
            reg, CAPABILITY, params, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        assert seen[0]["args_digest"] == params_digest(params)

    def test_the_digest_covers_the_args_the_skill_actually_ran(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """``allow_modified`` spliced the args; the digest follows the splice."""
        from axiom.infra.audit_trail import params_digest
        from axiom.infra.skill_dispatch import invoke_capability

        hookbus.register(_verdict_hook(lambda: allow_modified(args={"source": "redacted.md"})))
        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(
            reg,
            CAPABILITY,
            {"source": "secret.md"},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
        )

        assert seen[0]["args_digest"] == params_digest({"source": "redacted.md"})
        assert seen[0]["args_digest"] != params_digest({"source": "secret.md"})

    def test_the_payload_summarises_the_outcome(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        spy = Spy(SkillResult(ok=False, errors=["boom", "also boom"]))
        reg = _registry(spy)

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus)

        assert seen[0]["ok"] is False
        assert seen[0]["errors_count"] == 2

    def test_a_successful_call_reports_ok(self, tmp_path, hookbus, process_bus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy(SkillResult(ok=True, value="ran")))

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus)

        assert seen[0]["ok"] is True
        assert seen[0]["errors_count"] == 0
        assert seen[0]["error"] == ""

    def test_the_error_text_never_carries_the_result(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """A failed skill's own error strings stay off the bus; only the count travels."""
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy(SkillResult(ok=False, errors=["get failed: glpat-LEAK0000"])))

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus)

        assert "glpat-LEAK0000" not in seen[0]["error"]

    def test_the_payload_carries_the_principal(self, tmp_path, hookbus, process_bus, no_db_context):
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(
            reg,
            CAPABILITY,
            {},
            _ctx(reg, tmp_path, handle="@operator:site"),
            surface="cli",
            hookbus=hookbus,
        )

        assert seen[0]["principal"] == "@operator:site"

    def test_the_payload_carries_the_measured_latency(
        self, tmp_path, hookbus, process_bus, monkeypatch, no_db_context
    ):
        """Not just "an int": the number the gateway actually measured."""
        import axiom.infra.tool_gateway as gateway
        from axiom.infra.skill_dispatch import invoke_capability

        ticks = iter([10.0, 10.25])
        monkeypatch.setattr(gateway.time, "monotonic", lambda: next(ticks))
        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus)

        assert seen[0]["latency_ms"] == 250

    def test_the_declared_field_set_matches_what_is_published(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """``TELEMETRY_FIELDS`` is documentation only if nothing checks it."""
        from axiom.infra.skill_dispatch import TELEMETRY_FIELDS, invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        invoke_capability(reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus)

        assert set(TELEMETRY_FIELDS) == set(seen[0])
        assert "value" not in TELEMETRY_FIELDS
        assert "actions_taken" not in TELEMETRY_FIELDS
        assert "errors" not in TELEMETRY_FIELDS


class TestTelemetryOnTheFailurePath:
    def test_an_unknown_capability_still_publishes_one_event(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        with pytest.raises(KeyError):
            invoke_capability(
                reg, "press.nope", {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
            )

        assert len(seen) == 1
        assert seen[0]["tool_name"] == "press.nope"
        assert seen[0]["ok"] is False
        assert "KeyError" in seen[0]["error"]

    def test_the_failure_payload_is_the_same_narrow_shape(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import TELEMETRY_FIELDS, invoke_capability

        seen = _sink(process_bus)
        reg = _registry(Spy())

        with pytest.raises(KeyError):
            invoke_capability(
                reg,
                "press.nope",
                {"value": "glpat-ARGSECRET0000"},
                _ctx(reg, tmp_path),
                surface="cli",
                hookbus=hookbus,
            )

        assert set(seen[0]) == set(TELEMETRY_FIELDS)
        assert "glpat-ARGSECRET0000" not in str(seen[0])

    def test_a_denied_call_publishes_nothing(self, tmp_path, hookbus, process_bus, no_db_context):
        """The skill never ran, so there is no invocation to observe."""
        from axiom.infra.skill_dispatch import invoke_capability

        hookbus.register(_verdict_hook(lambda: deny(reason="site policy")))
        seen = _sink(process_bus)
        reg = _registry(Spy())

        out = invoke_capability(
            reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        assert out.ok is False
        assert seen == []


class TestSubscriberIsolation:
    """A broken observer must not become a broken tool call."""

    def test_a_raising_subscriber_does_not_fail_the_call(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        from axiom.infra.skill_dispatch import invoke_capability

        def boom(subject, payload):
            raise RuntimeError("subscriber exploded")

        process_bus.subscribe("tool.post_invoke", boom, fail_mode="abort")
        expected = SkillResult(ok=True, value={"drafted": 3})
        reg = _registry(Spy(expected))

        out = invoke_capability(
            reg, CAPABILITY, {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
        )

        assert out is expected

    def test_a_raising_subscriber_does_not_mask_the_failure_path(
        self, tmp_path, hookbus, process_bus, no_db_context
    ):
        """The caller still sees the real error, not the subscriber's."""
        from axiom.infra.skill_dispatch import invoke_capability

        def boom(subject, payload):
            raise RuntimeError("subscriber exploded")

        process_bus.subscribe("tool.post_invoke", boom, fail_mode="abort")
        reg = _registry(Spy())

        with pytest.raises(KeyError):
            invoke_capability(
                reg, "press.nope", {}, _ctx(reg, tmp_path), surface="cli", hookbus=hookbus
            )

    def test_a_bus_whose_publish_raises_does_not_fail_the_call(
        self, tmp_path, hookbus, no_db_context
    ):
        """Not just subscribers: a bus that cannot publish at all is survivable."""
        from axiom.infra.skill_dispatch import invoke_capability

        class BrokenBus:
            def publish(self, subject, payload=None, source=""):
                raise OSError("durable log is unwritable")

        expected = SkillResult(ok=True, value="ran")
        reg = _registry(Spy(expected))

        out = invoke_capability(
            reg,
            CAPABILITY,
            {},
            _ctx(reg, tmp_path),
            surface="cli",
            hookbus=hookbus,
            eventbus=BrokenBus(),
        )

        assert out is expected


class TestNothingSecretReachesADurableLog:
    """The hazard this narrowing exists for, pinned end to end."""

    def test_a_durable_default_bus_never_sees_an_argument_secret(
        self, tmp_path, hookbus, no_db_context
    ):
        from axiom.infra.bus import set_default_eventbus
        from axiom.infra.skill_dispatch import invoke_capability

        log_path = tmp_path / "events.jsonl"
        set_default_eventbus(EventBus(log_path=log_path))
        try:
            reg = _registry(Spy(), name="secrets.set")
            invoke_capability(
                reg,
                "secrets.set",
                {"name": "gitlab", "value": "glpat-ONDISKSECRET0000"},
                _ctx(reg, tmp_path),
                surface="cli",
                hookbus=hookbus,
            )
        finally:
            set_default_eventbus(None)

        written = log_path.read_text(encoding="utf-8")
        assert "tool.post_invoke" in written
        assert "glpat-ONDISKSECRET0000" not in written

    def test_a_durable_default_bus_never_sees_a_result_secret(
        self, tmp_path, hookbus, no_db_context
    ):
        from axiom.infra.bus import set_default_eventbus
        from axiom.infra.skill_dispatch import invoke_capability

        log_path = tmp_path / "events.jsonl"
        minted = "axk_abc_ONDISKTOKEN0000"
        set_default_eventbus(EventBus(log_path=log_path))
        try:
            spy = Spy(
                SkillResult(
                    ok=True,
                    value={"key_id": "abc", "token": minted},
                    actions_taken=[f"API key (shown once): {minted}"],
                )
            )
            reg = _registry(spy, name="gate.issue")
            invoke_capability(
                reg,
                "gate.issue",
                {"principal": "@svc:test"},
                _ctx(reg, tmp_path),
                surface="cli",
                hookbus=hookbus,
            )
        finally:
            set_default_eventbus(None)

        written = log_path.read_text(encoding="utf-8")
        assert "tool.post_invoke" in written
        assert minted not in written


class TestTheCredentialBearingVerbsAreStillCredentialBearing:
    """Rot-detectors for the finding: if these stop carrying a secret, the
    narrowing's rationale changed and this file should say so."""

    def test_gate_issue_returns_the_minted_token_in_its_result(self, tmp_path):
        from axiom.extensions.builtins.webgate.skills import issue_key

        out = issue_key.run(
            {
                "resource": "api-key",
                "principal": "@svc:test",
                "scope": ["llm"],
                "keys_file": str(tmp_path / "keys.json"),
            },
            _ctx(SkillRegistry(), tmp_path),
        )

        assert out.ok, out.errors
        assert out.value["token"].startswith("axk_")
        assert any(out.value["token"] in line for line in out.actions_taken)

    def test_secrets_get_reveal_returns_the_plaintext_value(self, tmp_path):
        from contextlib import contextmanager

        from axiom.extensions.builtins.secrets.skills import get as get_skill

        class FakeSecret:
            def as_str(self):
                return "glpat-REVEALED0000"

        class FakeStore:
            def metadata(self, name):
                return {"name": name}

            @contextmanager
            def get(self, name):
                yield FakeSecret()

        out = get_skill.run(
            {"name": "gitlab", "reveal": True, "_store": FakeStore()},
            _ctx(SkillRegistry(), tmp_path),
        )

        assert out.ok, out.errors
        assert out.value["value"] == "glpat-REVEALED0000"

    def test_the_secrets_cli_hands_the_stored_value_to_the_chokepoint(self, tmp_path, monkeypatch):
        """``axi secrets set`` reads the value off stdin and puts it in params."""
        import io

        from axiom.extensions.builtins.secrets import cli as secrets_cli

        seen: list[dict] = []

        def spy(registry, capability, params, ctx, **kwargs):
            seen.append({"capability": capability, "params": dict(params)})
            return SkillResult(ok=True)

        monkeypatch.setattr(secrets_cli, "invoke_capability", spy)
        monkeypatch.setattr(secrets_cli.sys, "stdin", io.StringIO("glpat-PIPEDSECRET0000\n"))
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))

        secrets_cli.main(["set", "gitlab"])

        assert seen[0]["capability"] == "secrets.set"
        assert seen[0]["params"]["value"] == "glpat-PIPEDSECRET0000"


class TestTheDeclaredSchemaCoversTheNarrowedPayload:
    """``ToolPostInvokePayload`` is what a subscriber author reads."""

    def test_every_telemetry_field_is_declared_in_the_payload_schema(self):
        from axiom.infra.hooks.event_schemas import ToolPostInvokePayload
        from axiom.infra.skill_dispatch import TELEMETRY_FIELDS

        declared = set(ToolPostInvokePayload.__annotations__)
        assert declared >= TELEMETRY_FIELDS, TELEMETRY_FIELDS - declared

    def test_the_schema_is_total_false_because_publishers_narrow(self):
        from axiom.infra.hooks.event_schemas import ToolPostInvokePayload

        assert ToolPostInvokePayload.__total__ is False
