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

Fail-open is deliberate for this step
-------------------------------------
If envelope construction or ``decide()`` itself raises, the hook logs at
WARNING and passes the call through. Step 1 must not be able to break a
tool call that worked yesterday. The site-manifest step (a loaded ``Rule``
set plus the load-time ``permitted_by_site`` gate) flips this to
fail-closed.

Step 2 routed the MCP and CLI capability surfaces through ``dispatch_tool``
too, via :func:`axiom.infra.skill_dispatch.invoke_capability`, so one site
rule on ``tool://<capability>`` now covers all three surfaces.

Follow-ups: site manifest → ``Rule`` loader; load-time gate
``permitted_by_site``.
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


def build_tool_context(*, session_factory: Any | None = None) -> DecideContext:
    """A fresh ``DecideContext`` in the open posture (see :func:`open_tool_rule`)."""
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


def default_tool_context() -> DecideContext:
    """The process-wide tool ``DecideContext``: no site rules, open posture,
    receipts when the platform DB is reachable."""
    global _default_ctx
    with _default_ctx_lock:
        if _default_ctx is None:
            _default_ctx = build_tool_context(session_factory=_receipt_session_factory())
        return _default_ctx


def _reset_default_tool_context() -> None:
    """Test seam: forget the cached default context."""
    global _default_ctx
    with _default_ctx_lock:
        _default_ctx = None


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

    Fail-open by design for this step: any exception here is logged at
    WARNING and the call passes through. The site-manifest step flips this
    to fail-closed once a policy is actually loaded.
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
        )
        verdict = decide_tool_call(envelope)
    except Exception as exc:  # noqa: BLE001 — fail-open, see docstring
        log.warning(
            "authority: could not decide tool call %r for %r; passing through "
            "(fail-open, P5 step 1): %s: %s",
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
    "OPEN_RULE_NAME",
    "RECEIPTS_ENV",
    "TOOL_INVOKE_INTENT",
    "TOOL_RESOURCE_SCHEME",
    "authority_pre_invoke_hook",
    "build_tool_context",
    "build_tool_envelope",
    "decide_tool_call",
    "default_tool_context",
    "open_tool_rule",
    "pre_invoke_handler",
    "record_tool_refusal",
    "register_authority_hook",
    "verdict_to_hook_result",
]
