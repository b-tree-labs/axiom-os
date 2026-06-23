# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""Classification-aware MCP tool-routing layer.

Wraps any async tool dispatcher with a thin pre-flight that consults the
existing :class:`axiom.infra.router.QueryRouter`. Based on the resulting
:class:`axiom.infra.router.RoutingDecision`, the wrapper:

  1. Decides whether dispatch may target a remote peer at all, and which one.
  2. Refuses (without dispatching) when an export-controlled query is being
     pointed at a peer whose ``ec_eligible`` flag is False — the canonical
     case being a public-cloud relay such as Portkey/OpenAI.
  3. Returns a structured ``routing`` block alongside the tool result so the
     end user can SEE which compute tier ran their request and *why*.
  4. Emits a :class:`RoutingProvenance` payload that callers can persist onto
     a memory fragment's ``content`` (or hand to ``CompositionService``)
     without this module taking a hard dependency on the memory layer.

This module **never re-implements classification** — it consumes
``QueryRouter.classify``'s output directly. It also stays domain-agnostic:
the EC keyword tables live in :mod:`axiom.infra.router` and are configurable
from ``runtime/config/export_control_terms.txt``. Domain extensions register
their own keyword extensions there; this module never names them.

Phase 1 scope (Prague / 0.10.x):
  Only ``public`` and ``export_controlled`` tiers are recognized. The
  finer-grained classification regimes from
  ``docs/specs/spec-classification-boundary.md`` (CUI, SECRET, compartments)
  are deferred — those need a real Phase-2 EC-boundary policy + signed
  classification stamps. This module is structured so adding more tiers is
  a closed-set extension.

Wire-in for the MCP server (``server.py::dispatch_call``):

  >>> from axiom.extensions.builtins.mcp.routing import wrap_dispatcher
  >>> from axiom.infra.router import QueryRouter
  >>> router = QueryRouter()
  >>> peers  = PeerRegistry.from_settings()  # or build inline for tests
  >>> dispatch_call = wrap_dispatcher(dispatch_call, router=router, peers=peers)

  The wrapped dispatcher returns ``{"result": <tool_result>, "routing": {...}}``.
  Callers that need MCP wire-format ``TextContent`` should ``json.dumps`` the
  whole envelope — the existing server already does this in its handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from axiom.infra.router import RoutingDecision, RoutingTier

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Peer registry — a deliberately tiny shape so the routing module stays
# decoupled from the federation registry. The MCP server (or its caller) is
# responsible for translating its own peer config into PeerDescriptors.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeerDescriptor:
    """A peer the router may forward a tool call to.

    ``ec_eligible`` is the central field: True iff this peer has been
    designated cleared for export-controlled traffic by facility policy.
    Public-cloud relays (Portkey, raw OpenAI) MUST be ``ec_eligible=False``.
    """

    name: str
    endpoint: str
    ec_eligible: bool = False
    tags: frozenset[str] = field(default_factory=frozenset)


@dataclass
class PeerRegistry:
    """In-memory peer lookup. Wrap any backing store you like behind this."""

    peers: list[PeerDescriptor] = field(default_factory=list)

    def get(self, name: str) -> PeerDescriptor | None:
        for p in self.peers:
            if p.name == name:
                return p
        return None

    def __iter__(self) -> Iterable[PeerDescriptor]:  # type: ignore[override]
        return iter(self.peers)


# ---------------------------------------------------------------------------
# Provenance payload — what gets persisted onto a memory fragment so audit
# trails answer "this fragment came from EC-tier compute, classifier reason X"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingProvenance:
    """Audit-grade routing breadcrumb attachable to a memory fragment.

    Stored in ``MemoryFragment.content["routing"]`` (the safe place — fragment
    ``provenance`` is immutable and slot-fixed; ``content`` is the natural
    extension surface for arbitrary write-time metadata).
    """

    routing_event_id: str
    tier: str  # "public" | "export_controlled" | "unknown"
    classifier: str
    reason: str
    matched_terms: list[str]
    chosen_peer: str | None
    forced_local: bool
    override_honored: bool
    refused: bool
    refused_peer: str | None
    fail_safe: bool

    @classmethod
    def from_decision(
        cls,
        *,
        decision: RoutingDecision | None,
        chosen_peer: str | None,
        forced_local: bool,
        override_honored: bool,
        refused: bool,
        refused_peer: str | None,
        fail_safe: bool,
        reason_override: str | None = None,
    ) -> "RoutingProvenance":
        if decision is None:
            return cls(
                routing_event_id="",
                tier="unknown",
                classifier="unavailable",
                reason=reason_override or "classifier unavailable; failed safe to local",
                matched_terms=[],
                chosen_peer=chosen_peer,
                forced_local=forced_local,
                override_honored=override_honored,
                refused=refused,
                refused_peer=refused_peer,
                fail_safe=fail_safe,
            )
        return cls(
            routing_event_id=decision.routing_event_id,
            tier=decision.tier.value,
            classifier=decision.classifier,
            reason=reason_override or decision.reason,
            matched_terms=list(decision.matched_terms),
            chosen_peer=chosen_peer,
            forced_local=forced_local,
            override_honored=override_honored,
            refused=refused,
            refused_peer=refused_peer,
            fail_safe=fail_safe,
        )

    def to_dict(self) -> dict[str, Any]:
        # ``chosen_peer`` is the internal field name on the dataclass;
        # ``routed_to_peer`` is the user-visible alias surfaced in tool
        # responses (matches the spec's MCP-response shape). Both keys are
        # populated so audit consumers and downstream UIs can read whichever
        # they were written against.
        return {
            "routing_event_id": self.routing_event_id,
            "tier": self.tier,
            "classifier": self.classifier,
            "reason": self.reason,
            "matched_terms": list(self.matched_terms),
            "chosen_peer": self.chosen_peer,
            "routed_to_peer": self.chosen_peer,
            "forced_local": self.forced_local,
            "override_honored": self.override_honored,
            "refused": self.refused,
            "refused_peer": self.refused_peer,
            "fail_safe": self.fail_safe,
        }


# ---------------------------------------------------------------------------
# Free-text extraction — what we hand to the classifier
# ---------------------------------------------------------------------------


# String-typed argument keys we treat as classifiable free text. Conservative
# on purpose: a field named ``api_key`` or ``__peer__`` is operational, not
# user content. Tools that want richer classification can pass a ``text=``
# field explicitly (most already do).
_TEXT_ARG_NAMES = ("text", "query", "prompt", "input", "message", "content")


def _extract_classifiable_text(arguments: dict[str, Any]) -> str:
    """Pull free-text arguments out of a tool-call payload for classification.

    Falls back to concatenating all string values if no canonical text-bearing
    field is found. Non-string values (numbers, dicts, lists) are skipped to
    keep the classifier window focused on natural language.
    """
    parts: list[str] = []
    for key in _TEXT_ARG_NAMES:
        v = arguments.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    if parts:
        return " ".join(parts)
    # Last-resort: any string value, but skip dunder/private operational keys.
    for k, v in arguments.items():
        if k.startswith("_"):
            continue
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The wrapper itself
# ---------------------------------------------------------------------------


# A dispatcher is any async callable (tool_name, arguments) -> result.
Dispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def route_tool_call(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    dispatcher: Dispatcher,
    router: Any,  # duck-typed: anything with .classify(text) -> RoutingDecision
    peers: PeerRegistry,
    requested_peer: str | None = None,
) -> dict[str, Any]:
    """Run a tool call through the classification-aware router.

    Returns one of two shapes:

      Success::
        {
          "result":  <whatever the dispatcher returned>,
          "routing": {tier, reason, ..., chosen_peer, forced_local, ...},
        }

      Refusal (EC content pointed at non-EC-eligible peer)::
        {
          "routing": {tier:"export_controlled", refused:True, refused_peer:..., reason:...},
        }
        (No ``result`` key — the dispatcher was deliberately NOT invoked.)
    """
    text = _extract_classifiable_text(arguments)

    # ── Stage 1: classify ───────────────────────────────────────────────────
    decision: RoutingDecision | None = None
    classifier_failure: BaseException | None = None
    try:
        decision = router.classify(text)
    except Exception as exc:  # noqa: BLE001 — fail-safe, not blow up
        classifier_failure = exc
        log.warning(
            "routing: classifier raised %s; failing safe to local-only dispatch",
            type(exc).__name__,
        )

    # ── Stage 2: fail-safe path when classifier broke ──────────────────────
    if classifier_failure is not None:
        prov = RoutingProvenance.from_decision(
            decision=None,
            chosen_peer=None,
            forced_local=True,
            override_honored=False,
            refused=False,
            refused_peer=None,
            fail_safe=True,
            reason_override=(
                f"classifier failed ({type(classifier_failure).__name__}); "
                "routed to local for safety"
            ),
        )
        result_payload = await dispatcher(tool_name, arguments)
        return {"result": result_payload, "routing": prov.to_dict()}

    assert decision is not None  # narrowed for type-checkers

    is_ec = decision.tier == RoutingTier.EXPORT_CONTROLLED
    requested = peers.get(requested_peer) if requested_peer else None

    # ── Stage 3: EC content → enforce peer eligibility ─────────────────────
    if is_ec and requested is not None and not requested.ec_eligible:
        # Refuse without dispatching. This is the headline guarantee of the
        # whole module — EC content NEVER reaches a public-cloud relay.
        keyword_tag = (
            f" (matched: {', '.join(decision.matched_terms[:3])})"
            if decision.matched_terms
            else ""
        )
        reason = (
            f"refused: peer {requested.name!r} is not EC-eligible; "
            f"classifier=`{decision.classifier}` "
            f"reason=`{decision.reason}`{keyword_tag}"
        )
        prov = RoutingProvenance.from_decision(
            decision=decision,
            chosen_peer=None,
            forced_local=True,
            override_honored=False,
            refused=True,
            refused_peer=requested.name,
            fail_safe=False,
            reason_override=reason,
        )
        return {"routing": prov.to_dict()}

    # ── Stage 4: explicit peer override path ───────────────────────────────
    if requested is not None:
        # Either: (a) public content + any peer (override is honored, but loud),
        # or:    (b) EC content + EC-eligible peer (legitimate co-routing).
        if is_ec:
            reason = (
                f"EC content + EC-eligible peer {requested.name!r}; "
                f"classifier=`{decision.classifier}` "
                f"reason=`{decision.reason}`"
            )
            override = False  # this is the *correct* route, not an override
        else:
            reason = (
                f"public-tier override honored: user requested peer "
                f"{requested.name!r}; classifier=`{decision.classifier}` "
                f"reason=`{decision.reason}`"
            )
            override = True
        prov = RoutingProvenance.from_decision(
            decision=decision,
            chosen_peer=requested.name,
            forced_local=False,
            override_honored=override,
            refused=False,
            refused_peer=None,
            fail_safe=False,
            reason_override=reason,
        )
        result_payload = await dispatcher(
            tool_name, {**arguments, "__peer__": requested.name}
        )
        return {"result": result_payload, "routing": prov.to_dict()}

    # ── Stage 5: no explicit peer → local dispatch ─────────────────────────
    # For both public and EC content with no peer requested, we run locally.
    # (A future "auto-pick EC-eligible peer" path is out-of-scope for Phase 1
    # — the spec says EC content stays local unless the caller explicitly
    # asks for an EC-eligible peer.)
    reason = (
        f"local dispatch ({decision.tier.value}); "
        f"classifier=`{decision.classifier}` reason=`{decision.reason}`"
    )
    prov = RoutingProvenance.from_decision(
        decision=decision,
        chosen_peer=None,
        forced_local=True,
        override_honored=False,
        refused=False,
        refused_peer=None,
        fail_safe=False,
        reason_override=reason,
    )
    result_payload = await dispatcher(tool_name, arguments)
    return {"result": result_payload, "routing": prov.to_dict()}


def wrap_dispatcher(
    dispatcher: Dispatcher,
    *,
    router: Any,
    peers: PeerRegistry,
) -> Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Return a routing-wrapped version of ``dispatcher``.

    Drop-in for ``axiom.extensions.builtins.mcp.server.dispatch_call``: pass
    the original dispatch coroutine in, get back one with the same call shape
    plus a ``routing`` block on every reply. Honors a ``__peer__`` key in the
    arguments dict as an explicit peer request (the MCP server should pop a
    matching tool-input field into that key before calling).
    """

    async def _wrapped(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        peer = arguments.pop("__peer__", None)
        return await route_tool_call(
            tool_name=name,
            arguments=arguments,
            dispatcher=dispatcher,
            router=router,
            peers=peers,
            requested_peer=peer,
        )

    return _wrapped


__all__ = [
    "Dispatcher",
    "PeerDescriptor",
    "PeerRegistry",
    "RoutingProvenance",
    "route_tool_call",
    "wrap_dispatcher",
]
