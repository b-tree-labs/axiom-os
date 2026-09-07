# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Capability dispatch through the tool gateway (P5, step 2).

Step 1 put every *chat* tool call through
``axiom.infra.tool_gateway.dispatch_tool``, so the ``tool.pre_invoke`` chain
(and with it the GUARD consult in :mod:`axiom.infra.authority`) saw it. The
CLI and MCP surfaces still called :meth:`SkillRegistry.invoke` directly and
so bypassed the chain entirely: no authorization consult, no receipt, no
``tool.post_invoke`` observation. :func:`invoke_capability` is the single
chokepoint both surfaces now go through, and every CLI verb dispatcher in
the tree goes through it, not a chosen few. A chokepoint with holes in it
is not a chokepoint; :data:`DIRECT_INVOKE_EXEMPT` and the guard test that
reads it are what keep the holes from reappearing.

One identity, three surfaces
----------------------------
The gateway ``tool_name`` is the **capability** name (``press.draft``) on
every surface. The MCP transport still advertises its mangled
``axiom_press__draft`` tool name for global uniqueness across MCP servers,
but that mangling stops at the transport: it never reaches the decision
point. That is the whole point of the chokepoint. A site rule written
against ``tool://press.draft`` covers the CLI verb, the chat tool and the
MCP tool at once, and a reviewer reading a receipt sees one name rather
than three spellings of it.

What is NOT routed
------------------
Two kinds of call site look routable and are not. Both are enumerated in
:data:`DIRECT_INVOKE_EXEMPT` with a reason apiece, and the guard test fails
on any direct registry ``invoke`` outside that list.

*Composition inside one action.* A skill invoking another skill is not a
surface crossing. Routing it would consult GUARD twice for a single user
intent and write two ledger records for one decision. The outer call, the
one a person or an MCP client actually made, crosses the boundary and is
already routed here.

*A call already downstream of the gateway.* The chat capability bridge in
``chat/tools.py`` runs inside the dispatcher the chat agent hands to
``dispatch_tool``, so it is past the decision point by the time it runs.
Routing it would double-consult the same call.

A note on the authorization verbs themselves: ``authz/cli.py`` IS routed,
and that is safe. ``decide()`` composes a rule engine, the policy sources
and the substrate; it never reaches :class:`SkillRegistry`, so consulting
it on the way into an ``audit.*`` verb cannot recurse. The verbs also stay
usable when authz is broken, because the authority hook fails open.

Hook outcomes are results, not exceptions
-----------------------------------------
A CLI verb and an MCP handler both need a value back. ``HookDenied`` becomes
a failed :class:`SkillResult` naming the hook source and the reason, plus an
action-ledger record via ``authority.record_tool_refusal``. ``ApprovalRequired``
becomes a failed :class:`SkillResult` too: neither an MCP handler nor a
non-interactive CLI can prompt a human, so a pending approval is a refusal
here rather than a hang. In both cases the skill body never runs.

Fail-open is unchanged
----------------------
If envelope construction or the decision itself raises, the authority hook
logs at WARNING and passes the call through. That behaviour lives in the
hook (step 1's deliberate choice) and is deliberately not duplicated here.

Post-invoke telemetry, and what it deliberately omits
-----------------------------------------------------
Both surfaces publish ``tool.post_invoke`` on the process default bus.
Omitting ``eventbus`` means "the process default"; passing ``None``
explicitly still means "no event", which is ``dispatch_tool``'s own
semantics and what a caller that wants silence asks for. The default moved
here rather than in ``dispatch_tool`` because the narrowing below is a
property of these two surfaces, not of the gateway: a caller reaching the
gateway directly must still choose its own bus and its own payload.

Two things had to be true first. The bus had to be bounded, and it now is:
``EventBus`` keeps at most ``DEFAULT_HISTORY_LIMIT`` events, so a
long-lived server retains a flat count rather than one per call for as
long as it runs. That closes the memory half of the older warning.

The other half was never about memory. The gateway payload carries the
tool's ``args`` and its ``result`` verbatim, and these surfaces run the
platform's credential verbs. Storing a foreign credential takes the value
on stdin and hands it to the skill in ``params``; the git-credential
endpoint hands over a whole protocol block, password line included;
revealing a stored credential returns the plaintext in ``result.value``;
issuing an API key returns the minted token in both ``result.value`` and
``actions_taken``, having just written only its hash to disk. The default
bus has no durable log, but ``get_default_eventbus`` documents that
production swaps in one wired to a canonical log file, and durable buses
are ordinary here: the command-line entry point already builds a
JSONL-backed one of its own for CLI lifecycle events. Publishing the
gateway payload as-is on these surfaces would have written credentials to
whatever file the default bus is pointed at, and written them twice over:
a subscriber that raises puts the whole payload back on the bus inside
``bus.errors``.

So these surfaces publish a projection instead of the payload. See
:data:`TELEMETRY_FIELDS` and :func:`telemetry_projection`: identity,
surface, outcome and latency travel; no argument value and no result value
does. Arguments are represented by ``args_digest``, computed with the same
:func:`axiom.infra.audit_trail.params_digest` the action-audit chain
already writes for every mutating invocation, so the event correlates to
its audit record and contributes nothing to a log that was not being
written there anyway. Key-shaped redaction was considered and rejected: it
keys off names like ``token`` and ``password``, and the leaks above ride
in ``value``, ``input`` and free-text ``actions_taken``.

The chat surface is unchanged and still publishes the full payload to its
own per-agent bus. That is a narrower blast radius than a process-wide,
possibly durable bus reached by every command-line verb and every protocol
call, which is why the same shape is not carried over here.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, Final

from axiom.infra.skills import SkillRegistry, SkillResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from axiom.infra.bus import EventBus
    from axiom.infra.hooks import HookBus
    from axiom.infra.skills import SkillContext


class _Unset(enum.Enum):
    """Sentinel type separating "no ``eventbus`` argument" from ``None``.

    ``None`` is a meaningful value here (publish nothing), so it cannot
    double as the default. An ``Enum`` member is the type-checker-friendly
    spelling of a sentinel.
    """

    TOKEN = "unset"


#: Default for ``invoke_capability(eventbus=...)``: use the process bus.
UNSET: Final = _Unset.TOKEN

#: Surfaces that cross into the platform from outside a skill.
CLI_SURFACE = "cli"
MCP_SURFACE = "mcp"

#: Modules allowed to call ``SkillRegistry.invoke`` directly, path (relative to
#: ``src/``) → why. Everything else on a CLI or MCP surface must come through
#: :func:`invoke_capability`, and the guard test in
#: ``tests/infra/test_skill_dispatch.py`` walks the tree with ``ast`` and fails
#: on any direct call outside this list. Adding an entry is a deliberate act:
#: write the reason, and a reviewer can check it.
DIRECT_INVOKE_EXEMPT: dict[str, str] = {
    # The chokepoint. This is the one direct call the gateway dispatches to.
    "axiom/infra/skill_dispatch.py": "the chokepoint; the gateway dispatches here",
    # Already downstream of the gateway: the chat agent hands this module's
    # execute_tool to dispatch_tool, so the decision has been made by the time
    # the bridge runs. Routing it would consult GUARD twice for one call.
    "axiom/extensions/builtins/chat/tools.py": (
        "runs inside the dispatcher the chat agent already hands to the gateway"
    ),
    # Composition inside one action: a standard bundle running its own steps.
    "axiom/extensions/builtins/publishing/skills/do_standard.py": (
        "intra-skill composition: the steps of one standard bundle"
    ),
    # Composition inside one action: the action a fired schedule runs. The
    # schedule verb that armed it was itself consulted at the CLI surface.
    "axiom/extensions/builtins/schedule/executor.py": (
        "intra-skill composition: the action a fired schedule runs"
    ),
    # Composition inside one action: a diagnose skill escalating to troubleshoot.
    "axiom/extensions/builtins/data_platform/skills/diagnose.py": (
        "intra-skill composition: diagnose escalating to troubleshoot"
    ),
    # Composition inside one action: install running its own post-step diagnose.
    "axiom/extensions/builtins/data_platform/skills/install.py": (
        "intra-skill composition: install running its own diagnose step"
    ),
    "axiom/extensions/builtins/observability/skills/install.py": (
        "intra-skill composition: install running its own diagnose step"
    ),
}


#: Every key a CLI or MCP ``tool.post_invoke`` payload carries, and the whole
#: of it. Deliberately absent: ``args`` and ``result``, the two fields the
#: gateway payload carries verbatim and the two that make a credential verb's
#: telemetry a copy of the credential. Widening this set puts tool arguments
#: and tool results on whatever sink the process default bus is wired to, so
#: widen it only with that in mind; the tests in
#: ``tests/infra/test_skill_dispatch.py`` fail if it drifts from what is
#: actually published.
TELEMETRY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "tool_name",
        "principal",
        "surface",
        "ok",
        "errors_count",
        "error",
        "latency_ms",
        "args_digest",
    }
)


def telemetry_projection(surface: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return the ``post_payload`` projection for ``surface``.

    Maps a gateway ``tool.post_invoke`` payload onto exactly
    :data:`TELEMETRY_FIELDS`. What survives is what a reader of a log needs
    to answer "who invoked what, from where, and did it work": the
    capability name (the same identity a site rule is written against, never
    the protocol-mangled one), the principal, the surface, the outcome and
    the latency.

    What does not survive is content. ``args`` becomes ``args_digest``, the
    same SHA-256 over redacted canonical JSON that
    :func:`axiom.infra.audit_trail.params_digest` writes into the action
    chain for the same invocation, so the two records join on it. ``result``
    becomes ``ok`` and ``errors_count``: a failed call is legible without
    its error strings, which are free text a skill composed and may have
    interpolated a value into. ``error`` is the gateway's own
    ``"TypeName: message"`` for a dispatcher that raised, which on this path
    is the ``KeyError`` for an unregistered capability.

    ``tokens`` is dropped rather than kept: it is a length taken over the
    arguments and the result, and there is no model on these surfaces for a
    cost meter to attribute it to.
    """

    def project(payload: dict[str, Any]) -> dict[str, Any]:
        from axiom.infra.audit_trail import params_digest

        result = payload.get("result")
        projection = result if isinstance(result, dict) else {}
        return {
            "tool_name": payload.get("tool_name", ""),
            "principal": payload.get("principal", ""),
            "surface": surface,
            "ok": bool(projection.get("ok", False)),
            "errors_count": len(projection.get("errors") or ()),
            "error": payload.get("error", ""),
            "latency_ms": payload.get("latency_ms", 0),
            "args_digest": params_digest(payload.get("args")),
        }

    return project


def capability_namespace(capability: str) -> str:
    """``press.draft`` → ``press``. The originating extension, for the envelope."""
    namespace, sep, _ = (capability or "").partition(".")
    return namespace if sep else (capability or "")


def principal_handle(ctx: SkillContext | None) -> str:
    """The ``@name:context`` handle the gateway parses, from ``ctx.principal``.

    ``PrincipalContext.handle`` is already the rendered handle (ADR-074); no
    second format is invented here. ``ctx=None`` falls back to
    ``open_principal()``, which is exactly what :class:`SkillContext` uses as
    its own default.
    """
    from axiom.infra.principal import open_principal

    principal = ctx.principal if ctx is not None else None
    if principal is None:
        principal = open_principal()
    return principal.handle


def invoke_capability(
    registry: SkillRegistry,
    capability: str,
    params: dict[str, Any],
    ctx: SkillContext | None,
    *,
    surface: str,
    hookbus: HookBus | None = None,
    eventbus: EventBus | None | _Unset = UNSET,
) -> SkillResult:
    """Invoke ``capability`` through the tool gateway and return its result.

    Args:
        registry: The registry holding the capability.
        capability: Qualified capability name (``press.draft``). This is the
            gateway ``tool_name`` on every surface.
        params: Skill params. A ``tool.pre_invoke`` hook returning
            ``allow_modified(args=...)`` splices them before the skill runs.
        ctx: The skill context. ``None`` is accepted (two publishing verbs
            pass it today) and is threaded to the skill unchanged; only the
            principal falls back to the open one.
        surface: ``"cli"`` or ``"mcp"``, recorded on a refusal so the ledger
            says where the call came from.
        hookbus: Bus the ``tool.pre_invoke`` chain fires on. ``None`` uses the
            process default.
        eventbus: Where ``tool.post_invoke`` is published, narrowed to
            :data:`TELEMETRY_FIELDS`. Omitted uses the process default bus;
            an explicit ``None`` publishes nothing, matching
            ``dispatch_tool``. The two are different asks, so the default is
            a sentinel rather than ``None``.

    Returns:
        The typed :class:`SkillResult` the skill returned, or a refusal result
        when a hook denied the call or asked for an approval this surface
        cannot obtain.

    Raises:
        KeyError: ``capability`` is not registered. Unchanged from
            :meth:`SkillRegistry.invoke`.
    """
    from axiom.infra.authority import record_tool_refusal, register_authority_hook
    from axiom.infra.bus import get_default_eventbus
    from axiom.infra.hooks import ApprovalRequired, HookDenied
    from axiom.infra.tool_gateway import dispatch_tool

    handle = principal_handle(ctx)
    origin = capability_namespace(capability)
    bus = get_default_eventbus() if eventbus is UNSET else eventbus

    # Declared authority (P5): the GUARD consult is a hook, and the hook has
    # to be on the bus this call fires. Idempotent per bus, exactly as the
    # chat surface does it.
    register_authority_hook(hookbus)

    holder: dict[str, SkillResult] = {}

    def _run(name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run the skill, keep the typed result, hand the gateway a payload.

        The gateway is written against dicts, so it gets the projection and
        the caller gets the ``SkillResult`` itself. What reaches
        ``tool.post_invoke`` is narrower still: :func:`telemetry_projection`
        reduces this dict to ``ok`` and ``errors_count`` before it is
        published.
        """
        result = registry.invoke(name, args, ctx)
        holder["result"] = result
        return {
            "ok": result.ok,
            "value": result.value,
            "errors": list(result.errors),
            "actions_taken": list(result.actions_taken),
        }

    try:
        dispatch_tool(
            tool_name=capability,
            args=params,
            principal=handle,
            hookbus=hookbus,
            eventbus=bus,
            dispatcher=_run,
            ext_origin=origin,
            post_payload=telemetry_projection(surface),
        )
    except HookDenied as denial:
        source = denial.hook_source or origin
        record_tool_refusal(
            tool_name=capability,
            principal=handle,
            reason=denial.reason,
            surface=surface,
            hook_source=source,
            args=params,
        )
        return SkillResult(
            ok=False,
            errors=[f"denied by hook ({source}): {denial.reason}"],
        )
    except ApprovalRequired as approval:
        source = approval.hook_source or origin
        return SkillResult(
            ok=False,
            errors=[
                f"approval required ({source}): {approval.reason}. "
                f"{capability} was not run: the {surface} surface cannot prompt "
                f"for approval. Approve it from an interactive surface, or add a "
                f"site rule that permits it."
            ],
        )

    return holder["result"]


__all__ = [
    "CLI_SURFACE",
    "DIRECT_INVOKE_EXEMPT",
    "MCP_SURFACE",
    "TELEMETRY_FIELDS",
    "UNSET",
    "capability_namespace",
    "invoke_capability",
    "principal_handle",
    "telemetry_projection",
]
