# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Declared authority — every tool call consults GUARD (P5, step 1).

Until now the chat, MCP and CLI tool surfaces never consulted the platform
authorization decision point. ``axiom.infra.tool_gateway.dispatch_tool``
fired the ``tool.pre_invoke`` hook chain, but no hook called ``decide()``:
a tool call left no receipt, and a refusal (when some hook did deny) left
no record. This module closes the first half of that gap, for the chat
surface, without changing what a user sees when no site policy is loaded.

What is wired
-------------
* :func:`build_tool_envelope` — the ``ActionEnvelope`` for one tool call:
  intent :data:`TOOL_INVOKE_INTENT` (a registered platform verb), the tool
  named in the resource ``tool://<name>``, the actor parsed from the
  ``@name:context`` handle the gateway already carries, an open capability,
  and the classification passed through.
* :func:`decide_tool_call` — the envelope-typed decision site the
  ``no_action_without_authz`` lint enforces; its first statement is
  ``decide(...)``.
* :func:`default_tool_context` — the process-wide ``DecideContext``: no site
  rules loaded, the default policy sources, and a best-effort receipt
  session only when the platform DB is reachable.
* :func:`authority_pre_invoke_hook` / :func:`pre_invoke_handler` — the
  ``tool.pre_invoke`` interceptor that maps a verdict onto the hook
  vocabulary, and :func:`register_authority_hook`, which puts it on the
  default ``HookBus`` idempotently and after every other hook.
* :func:`record_tool_refusal` — the action-ledger record a surface writes
  when a hook denial becomes an error string.

Behaviour-neutral by construction
---------------------------------
``decide()`` treats a novel action with no matching rule as *propose to a
human* (``policy.PolicySourceRegistry.combine``, phase 3). Left alone, that
would turn every chat tool call into an approval prompt. The default tool
context therefore carries exactly one rule, :data:`OPEN_RULE_NAME` — permit
``tool.*`` on ``tool://*`` — the same shape ``governance.simple`` uses for
its dev-mode ``dev_permit_<ext>`` rule. A site ``deny`` or ``propose`` rule
wins over it automatically (deny > propose > permit in the rule engine), so
the site-manifest step only has to *add* rules; nothing here is removed.

Posture is site-conditional (ADR-114 §2)
----------------------------------------
With **no** site policy loaded the hook is fail-open: if envelope construction
or ``decide()`` itself raises, it logs at WARNING and passes the call through —
the open default must not break a tool call that worked yesterday. Once a site
policy file is present (:mod:`axiom.infra.authority_rules`), the same error path
instead **denies** (fail-closed): a site that declared a policy must not have a
decision-engine error silently admit a call. Presence is the switch — a
malformed policy still counts as present (loud ERROR, no rules applied).

The MCP and CLI capability surfaces route through ``dispatch_tool`` too, via
:func:`axiom.infra.skill_dispatch.invoke_capability`, so one site rule on
``tool://<capability>`` covers all three surfaces (surface-scoped rules —
"require approval for ``press.publish`` *on mcp* only" — are ADR-114 §3).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import uuid
from typing import TYPE_CHECKING, Any

from axiom.governance.capability import CapabilityToken
from axiom.governance.classification import Classification
from axiom.governance.envelope import ActionEnvelope
from axiom.governance.intent import ActionIntent, IntentPattern
from axiom.governance.provenance import ProvenanceRef
from axiom.governance.resource import ResourcePattern, ResourceRef
from axiom.governance.verdict import NextAction, Verdict
from axiom.infra.hooks import (
    HookBus,
    HookContext,
    HookResult,
    HookSpec,
    allow,
    deny,
    get_default_hookbus,
    request_approval,
)
from axiom.vega.identity.principal import Principal

if TYPE_CHECKING:
    from axiom.extensions.builtins.authz.decide import DecideContext
    from axiom.extensions.builtins.authz.rules import Rule

log = logging.getLogger("axiom.infra.authority")

#: The registered intent every tool call carries (``governance.intent``).
TOOL_INVOKE_INTENT = "tool.invoke"
#: Resource scheme: the tool is the resource — ``tool://<name>``.
TOOL_RESOURCE_SCHEME = "tool"
#: The single default rule: open posture until a site manifest says otherwise.
OPEN_RULE_NAME = "tool_open_default"
#: Runs after every manifest hook (AEOS default 100; memory_dedup is 50) so
#: the decision sees the *effective* args and a dedup deny costs no receipt.
AUTHORITY_HOOK_PRIORITY = 1000
AUTHORITY_HOOK_SOURCE = "platform"
#: ``off`` disables receipt persistence (tests, air-gapped dev); default probes
#: the platform DB once per process and degrades silently when unreachable.
RECEIPTS_ENV = "AXIOM_AUTHORITY_RECEIPTS"

_SURFACE_LEDGER_GUARD = "tool.pre_invoke"


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _principal_from_handle(handle: str) -> Principal:
    """The platform's placeholder-key Principal for a bare handle.

    Mirrors ``governance.simple._principal_from_handle`` / the HTTP authz
    hook: deterministic SHA-256 public bytes until the identity boundary
    hands us a keypair-backed Principal. ``Principal`` itself rejects a
    malformed handle.
    """
    handle = (handle or "").strip()
    if not handle.startswith("@"):
        handle = f"@{handle}"
    placeholder = hashlib.sha256(handle.encode("utf-8")).digest()
    return Principal(handle=handle, public_bytes=placeholder)


def _sanitise_tool_name(tool_name: str) -> str:
    name = re.sub(r"\s+", "_", (tool_name or "").strip())
    if not name:
        raise ValueError("tool_name cannot be empty")
    return name


def build_tool_envelope(
    tool_name: str,
    args: dict[str, Any],
    principal: str,
    *,
    classification: str = "",
    ext_origin: str = "",
    surface: str = "",
    strict: bool = False,
) -> ActionEnvelope:
    """The ``ActionEnvelope`` for one tool call.

    ``principal`` is the ``@name:context`` handle ``dispatch_tool`` already
    receives; a malformed handle raises ``ValueError`` exactly as
    ``Principal`` does. ``args`` are not copied into the envelope (they may
    carry content); the tool is identified by the resource ``tool://<name>``.
    """
    name = _sanitise_tool_name(tool_name)
    actor = _principal_from_handle(principal)
    cls = Classification.from_str(classification) if classification else Classification.INTERNAL
    origin = (ext_origin or "").strip() or "tool_gateway"
    return ActionEnvelope(
        actor=actor,
        capability=CapabilityToken.unscoped_test_token(subject=actor),
        classification=cls,
        context_fragment_id=f"extension://{origin}",
        provenance_parent=ProvenanceRef.synthetic(f"tool:{name}"),
        federation_origin=None,
        intent=ActionIntent(TOOL_INVOKE_INTENT),
        resource=ResourceRef(scheme=TOOL_RESOURCE_SCHEME, identifier=name),
        deadline=None,
        dedup_key=f"tool-{uuid.uuid4().hex}",
        surface=(surface or "").strip(),
        strict=strict,
    )


# ---------------------------------------------------------------------------
# Decision site + default context
# ---------------------------------------------------------------------------


def decide_tool_call(envelope: ActionEnvelope, ctx: DecideContext | None = None) -> Verdict:
    """Consult GUARD for one tool call. The lint-enforced decision site."""
    from axiom.extensions.builtins.authz.decide import decide

    return decide(envelope, ctx if ctx is not None else default_tool_context())


def open_tool_rule() -> Rule:
    """Permit ``tool.*`` on ``tool://*`` — the open posture, by name.

    Named so the receipt says *why* a call was permitted. Any site rule that
    denies or proposes outranks it without touching this rule.
    """
    from axiom.extensions.builtins.authz.rules import Rule

    return Rule(
        name=OPEN_RULE_NAME,
        intent_pattern=IntentPattern(f"{ActionIntent(TOOL_INVOKE_INTENT).primitive}.*"),
        actor_pattern="*",
        resource_pattern=ResourcePattern(f"{TOOL_RESOURCE_SCHEME}://*"),
        disposition="permit",
        priority=-1000,
    )


#: External-egress write capabilities that require human approval when invoked
#: over MCP (ADR-114 §3). These return to the MCP surface behind this floor,
#: replacing the interim withholding (P8/B). They are named here because the
#: platform has no extension→authority rule-contribution seam yet; a
#: per-capability declaration (a ``SkillSpec`` field, mirroring §1's
#: ``allowed_principals``) is the documented follow-up. A site MAY add a stricter
#: ``deny``; the ``propose`` floor is not relaxable from the site policy
#: (``deny > propose > permit``) — an operator that wants ungated egress-writes
#: over MCP removes ``mcp`` from the capability, a deliberate edit.
EGRESS_WRITE_MCP_APPROVAL_TOOLS: tuple[str, ...] = ("press.publish", "press.mirror_sync")
EGRESS_WRITE_MCP_RULE_PREFIX = "egress_write_mcp_approval"


def egress_write_mcp_rules() -> list[Rule]:
    """The built-in ``propose``-on-mcp floor for external-egress writes (§3).

    Surface-scoped: these rules match only ``surface="mcp"`` envelopes, so the
    CLI and agent paths (where the owner already gets a confirm) are untouched.
    """
    from axiom.extensions.builtins.authz.rules import Rule

    intent = f"{ActionIntent(TOOL_INVOKE_INTENT).primitive}.*"
    return [
        Rule(
            name=f"{EGRESS_WRITE_MCP_RULE_PREFIX}:{tool}",
            intent_pattern=IntentPattern(intent),
            actor_pattern="*",
            resource_pattern=ResourcePattern(f"{TOOL_RESOURCE_SCHEME}://{tool}"),
            surface_pattern="mcp",
            disposition="propose",
        )
        for tool in EGRESS_WRITE_MCP_APPROVAL_TOOLS
    ]


def build_tool_context(*, session_factory: Any | None = None) -> DecideContext:
    """A fresh ``DecideContext`` in the open posture (see :func:`open_tool_rule`).

    Only the open rule — the built-in egress floor + any site rules are layered
    on by :func:`default_tool_context` (production), so this stays the minimal
    building block tests reason about.
    """
    from axiom.extensions.builtins.authz.decide import DecideContext

    ctx = DecideContext(session_factory=session_factory)
    ctx.add_rule(open_tool_rule())
    return ctx


def _receipt_session_factory() -> Any | None:
    """``lambda: session_for('authz')`` when the platform DB answers, else ``None``.

    Probed once per process (the context is cached): a dead DB must not cost
    a connection attempt on every tool call, and receipts are best-effort
    in this step — ``decide()`` already swallows a failed write.
    """
    if os.environ.get(RECEIPTS_ENV, "auto").strip().lower() in {"off", "0", "false", "no"}:
        return None
    try:
        from axiom.infra import db as _db

        engine = _db.get_engine()
        with engine.connect():
            pass
    except Exception as exc:  # noqa: BLE001 — best-effort receipts
        log.info(
            "authority receipts disabled: platform DB unreachable (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return None
    from axiom.infra.db import session_for

    return lambda: session_for("authz")


_default_ctx: DecideContext | None = None
_default_ctx_lock = threading.Lock()
#: Set when the default context is built: whether a site policy file is present
#: (ADR-114 §2). Drives the fail-open→fail-closed flip in the hook: with a site
#: policy loaded, an erroring/undecidable decision denies instead of passing
#: through. A malformed policy still counts as *present* (fail-closed, loud).
_site_policy_present: bool = False


def _apply_site_rules(ctx: DecideContext, path: Any | None = None) -> bool:
    """Load site rules into ``ctx`` and return whether a policy file is present.

    Never raises: a malformed policy is logged at ERROR and treated as present
    (fail-closed) with **no** rules applied — a half-parsed policy is never
    installed. An absent policy leaves the open posture untouched.
    """
    from axiom.infra import authority_rules

    try:
        policy = authority_rules.load_site_policy(path)
    except authority_rules.SiteRuleError as exc:
        log.error(
            "authority: site policy present but unloadable — applying NO site rules "
            "and treating the surface as fail-closed; fix the policy file: %s",
            exc,
        )
        return True
    for rule in policy.rules:
        ctx.add_rule(rule)
    if policy.rules:
        log.info(
            "authority: loaded %d site rule(s) from %s", len(policy.rules), policy.path
        )
    return policy.present


def default_tool_context() -> DecideContext:
    """The process-wide tool ``DecideContext``: the open posture, plus the
    built-in egress-write MCP approval floor (ADR-114 §3) and any site rules
    (ADR-114 §2), with receipts when the platform DB is reachable."""
    global _default_ctx, _site_policy_present
    with _default_ctx_lock:
        if _default_ctx is None:
            ctx = build_tool_context(session_factory=_receipt_session_factory())
            for rule in egress_write_mcp_rules():
                ctx.add_rule(rule)
            _site_policy_present = _apply_site_rules(ctx)
            _default_ctx = ctx
        return _default_ctx


def _site_policy_active() -> bool:
    """Whether a site policy is loaded — ensuring the default context is built.

    Called on the hook's error path, where the exception may have fired before
    the context was ever materialised, so the flag would otherwise read stale.
    """
    if _site_policy_present:
        return True
    try:
        default_tool_context()
    except Exception:  # noqa: BLE001 — never let the guard's guard raise
        return _site_policy_present
    return _site_policy_present


def _reset_default_tool_context() -> None:
    """Test seam: forget the cached default context and the site-policy flag."""
    global _default_ctx, _site_policy_present
    with _default_ctx_lock:
        _default_ctx = None
        _site_policy_present = False


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def verdict_to_hook_result(verdict: Verdict) -> HookResult | None:
    """Map ``next_action_for_caller`` onto the hook vocabulary.

    ``None`` is pass-through (the bus treats it as ``allow()``).
    """
    nxt = verdict.next_action_for_caller
    if nxt is NextAction.PROCEED:
        return None
    if nxt is NextAction.ABORT:
        return deny(reason=verdict.reason)
    if nxt in (NextAction.AWAIT_HUMAN, NextAction.ENQUEUE_PROPOSAL):
        return request_approval(why=verdict.reason, approval_token=verdict.receipt_fragment_id)
    if nxt is NextAction.SATISFY_CHALLENGE:
        why = verdict.reason
        if verdict.challenge is not None and verdict.challenge.remediation:
            why = f"{why}; {verdict.challenge.remediation}"
        return request_approval(why=why, approval_token=verdict.receipt_fragment_id)
    log.warning("authority: unmapped next action %r; passing through", nxt)
    return None


def authority_pre_invoke_hook(payload: dict[str, Any], principal: str = "") -> HookResult | None:
    """``tool.pre_invoke`` hook: build the envelope, consult GUARD, map the verdict.

    Posture is site-conditional (ADR-114 §2): with **no** site policy loaded the
    step stays fail-open — any exception here is logged at WARNING and the call
    passes through, preserving the open default. Once a site policy is present
    (:func:`_site_policy_active`), an erroring/undecidable decision instead
    **denies** (fail-closed) — a site that bothered to declare a policy must not
    have a decision-engine error silently admit a call.
    """
    tool_name = payload.get("tool_name") or ""
    handle = payload.get("principal") or principal or ""
    if not handle:
        from axiom.infra.principal import local_handle

        handle = local_handle()
    try:
        envelope = build_tool_envelope(
            tool_name,
            payload.get("args") or {},
            handle,
            classification=payload.get("classification") or "",
            ext_origin=payload.get("ext_origin") or "",
            surface=payload.get("surface") or "",
        )
        verdict = decide_tool_call(envelope)
    except Exception as exc:  # noqa: BLE001 — posture-conditional, see docstring
        if _site_policy_active():
            log.error(
                "authority: could not decide tool call %r for %r; DENYING "
                "(fail-closed, site policy active): %s: %s",
                tool_name,
                handle,
                type(exc).__name__,
                exc,
            )
            return deny(
                reason=(
                    "authority decision failed and a site policy is active; "
                    "denying (fail-closed)"
                )
            )
        log.warning(
            "authority: could not decide tool call %r for %r; passing through "
            "(fail-open, no site policy): %s: %s",
            tool_name,
            handle,
            type(exc).__name__,
            exc,
        )
        return None
    return verdict_to_hook_result(verdict)


def pre_invoke_handler(ctx: HookContext) -> HookResult:
    """Platform ``HookEntry`` adapter (``HookContext -> HookResult``).

    Declared by the authz extension's ``axiom-extension.toml`` as a
    ``kind = "hook"`` provider so ``HookRegistry`` discovery sees it, and
    registered imperatively by :func:`register_authority_hook` at the chat
    call site (discovery is not run on the chat path today).
    """
    return authority_pre_invoke_hook(ctx.payload, ctx.principal) or allow()


_SPEC = HookSpec(
    event="tool.pre_invoke",
    entry=pre_invoke_handler,
    priority=AUTHORITY_HOOK_PRIORITY,
    fail_mode="warn",
    source=AUTHORITY_HOOK_SOURCE,
)
_register_lock = threading.Lock()


def register_authority_hook(bus: HookBus | None = None) -> HookSpec:
    """Put the authority hook on ``bus`` (default: the process ``HookBus``).

    Idempotent per bus: a second call — or a manifest-discovered copy of the
    same handler — leaves exactly one registration.
    """
    target = bus if bus is not None else get_default_hookbus()
    with _register_lock:
        for spec in target.hooks_for(_SPEC.event):
            if spec.entry is pre_invoke_handler:
                return spec
        target.register(_SPEC)
    return _SPEC


# ---------------------------------------------------------------------------
# Refusal audit
# ---------------------------------------------------------------------------


def record_tool_refusal(
    *,
    tool_name: str,
    principal: str,
    reason: str,
    surface: str = "chat",
    hook_source: str = "",
    args: dict[str, Any] | None = None,
) -> None:
    """Journal a hook denial in the action ledger. Best-effort; never raises.

    Only argument *keys* are recorded — argument values may carry content.
    """
    try:
        from axiom.infra.paths import get_user_state_dir
        from axiom.policy.action_ledger import OUTCOME_REFUSED, ActionLedger

        name = _sanitise_tool_name(tool_name) if tool_name else "(unknown)"
        ActionLedger(state_dir=get_user_state_dir()).record_action(
            agent=surface,
            op_class=TOOL_INVOKE_INTENT,
            name=name,
            candidate=f"{TOOL_RESOURCE_SCHEME}://{name}",
            guards=[_SURFACE_LEDGER_GUARD],
            outcome=OUTCOME_REFUSED,
            refusing_rule=reason or "refused",
            metadata={
                "principal": principal,
                "hook_source": hook_source,
                "arg_keys": sorted(args or {}),
            },
        )
    except Exception:  # noqa: BLE001 — audit must never break the surface
        log.debug("action ledger emission failed for tool %r", tool_name, exc_info=True)


__all__ = [
    "AUTHORITY_HOOK_PRIORITY",
    "AUTHORITY_HOOK_SOURCE",
    "EGRESS_WRITE_MCP_APPROVAL_TOOLS",
    "EGRESS_WRITE_MCP_RULE_PREFIX",
    "OPEN_RULE_NAME",
    "RECEIPTS_ENV",
    "TOOL_INVOKE_INTENT",
    "TOOL_RESOURCE_SCHEME",
    "authority_pre_invoke_hook",
    "build_tool_context",
    "build_tool_envelope",
    "decide_tool_call",
    "default_tool_context",
    "egress_write_mcp_rules",
    "open_tool_rule",
    "pre_invoke_handler",
    "record_tool_refusal",
    "register_authority_hook",
    "verdict_to_hook_result",
]
