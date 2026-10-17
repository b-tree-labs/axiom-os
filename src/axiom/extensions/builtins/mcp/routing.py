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

The client-sink gate (:func:`gate_result_for_client`) is the exception to
"consumes ``QueryRouter.classify``": it does not classify result text at all.
It reads stored labels only (ADR-175).

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
import os
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

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
    tier: str  # "public" | "export_controlled" | "access_controlled" | "unknown"
    classifier: str
    reason: str
    matched_terms: list[str]
    chosen_peer: str | None
    forced_local: bool
    override_honored: bool
    refused: bool
    refused_peer: str | None
    fail_safe: bool
    # A mutating tool runs locally BEFORE the gate classifies its result, so by
    # the time a withhold is decided the write has already happened. These say
    # so honestly: ``committed`` means the side effect took effect, and
    # ``result_withheld`` means only the OUTPUT was kept from a non-EC client.
    # For a read both stay False and ``refused`` carries the meaning as before.
    committed: bool = False
    result_withheld: bool = False

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
        committed: bool = False,
        result_withheld: bool = False,
    ) -> RoutingProvenance:
        if decision is None:
            return cls(
                # A minted id, not "", so a fail-closed withhold is auditable by
                # the same breadcrumb the client is handed.
                routing_event_id=str(uuid.uuid4()),
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
                committed=committed,
                result_withheld=result_withheld,
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
            committed=committed,
            result_withheld=result_withheld,
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
            "committed": self.committed,
            "result_withheld": self.result_withheld,
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


# ---------------------------------------------------------------------------
# What may withhold a result at the client-sink gate
# ---------------------------------------------------------------------------
#
# Only a stored LABEL on the content (``access_tier`` / ``classification``). The
# store adjudicated it at ingest, per document, so the gate reads that answer
# rather than guessing one.
#
# The result TEXT is not classified for export control (ADR-175). Export control
# attaches to the controlled codes themselves and to their execution by a named
# person; it is enforced where the code is held and run (filesystem permissions
# and the job broker; see ``axiom.governance.controlled_code``). A code's NAME, and
# the inputs, outputs and results of running it, are not export controlled. A
# keyword list of code names matched against result text, or a small model's guess
# about it, withheld exactly that ordinary material, so neither runs here any more.
#
# Code that is itself controlled and has been ingested (a RAG chunk of its source,
# say) is withheld because it carries an export-control label from ingest, which is
# the code's own access boundary. Data under a different regime (proprietary,
# personal, safeguards) is withheld under its own label and reported as that regime.

#: The retired switch that made the model's verdict withhold. Read only to warn an
#: operator who set it that it no longer has any effect.
SINK_SEMANTIC_ENV = "AXIOM_ROUTING_SINK_SEMANTIC"
_PUBLIC_TIERS = frozenset({"public"})
_PUBLIC_CLASSIFICATIONS = frozenset({"", "unclassified", "public"})
_LABEL_SCAN_DEPTH = 6

# Data a deployment serves can carry a label from an access-control regime:
# proprietary, personal or institutional. Those withhold from a client whose model
# runs outside the deployment, and are reported as ``access_controlled``, never as
# export control.
ACCESS_CONTROLLED = "access_controlled"
_ACCESS_CONTROL_LABELS = frozenset(
    {"internal", "restricted", "proprietary", "confidential", "sensitive", "pii", "safeguards"}
)
#: Labels that declare export-controlled content: the controlled code itself.
_EXPORT_CONTROL_LABELS = frozenset({"export_controlled", "export-controlled", "ec", "controlled"})


def _warn_if_retired_switch_set() -> None:
    if os.environ.get(SINK_SEMANTIC_ENV, "").strip():
        log.warning(
            "%s is set but no longer has any effect: the sink gate does not classify "
            "result text for export control (ADR-175); stored labels decide",
            SINK_SEMANTIC_ENV,
        )


def _collect_labels(obj: Any, depth: int = 0) -> list[tuple[str | None, str | None]]:
    """(access_tier, classification) of every labelled item in a tool result."""
    found: list[tuple[str | None, str | None]] = []
    if depth > _LABEL_SCAN_DEPTH:
        return found
    if isinstance(obj, dict):
        if "access_tier" in obj or "classification" in obj:
            tier, cls = obj.get("access_tier"), obj.get("classification")
            found.append((tier if isinstance(tier, str) else None, cls if isinstance(cls, str) else None))
        for v in obj.values():
            found.extend(_collect_labels(v, depth + 1))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_collect_labels(v, depth + 1))
    return found


def _label_state(result: Any) -> tuple[str, list[str]]:
    """``nonpublic`` | ``public`` | ``unlabeled``, plus the offending labels.

    A mixed result is withheld whole."""
    labels = _collect_labels(result)
    if not labels:
        return "unlabeled", []
    bad = []
    for tier, cls in labels:
        if tier is not None and tier.strip().lower() not in _PUBLIC_TIERS:
            bad.append(f"access_tier={tier}")
        if cls is not None and cls.strip().lower() not in _PUBLIC_CLASSIFICATIONS:
            bad.append(f"classification={cls}")
    return ("nonpublic", sorted(set(bad))) if bad else ("public", [])


def _label_regime(bad_labels: list[str]) -> str:
    """The regime that withholds: ``export_controlled`` if any label declares
    export-controlled content, else ``access_controlled`` if every label is an
    access-control label, else ``unknown`` (an undeclared label; still withheld)."""
    values = [item.split("=", 1)[-1].strip().lower() for item in bad_labels]
    if any(v in _EXPORT_CONTROL_LABELS for v in values):
        return RoutingTier.EXPORT_CONTROLLED.value
    if values and all(v in _ACCESS_CONTROL_LABELS for v in values):
        return ACCESS_CONTROLLED
    return "unknown"


_REGIME_WORDS = {
    "export_controlled": "export-controlled content (the controlled code itself)",
    ACCESS_CONTROLLED: "a data-access label, not export control",
    "unknown": "an undeclared label, withheld until its regime is declared",
}


async def gate_result_for_client(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    dispatcher: Dispatcher,
    router: Any = None,  # retained for call compatibility; result text is not classified
    client_ec_capable: bool,
    client_name: str = "unknown",
    tool_mutates: bool = False,
) -> dict[str, Any]:
    """Client-sink gate: labelled non-public content stays inside the deployment.

    The MCP tools run locally, but the *result* is handed back to the host client,
    whose model may be a public cloud. A client is EC-capable only when its model
    is the in-deployment endpoint (the installer stamps
    ``AXIOM_MCP_CLIENT_EC_CAPABLE``); for it the gate is bypassed. For any other
    client the tool runs, and its result is withheld only when it carries a
    non-public stored label. The refusal names the regime that withheld it.

    Returns ``{"result": ...}`` on allow, or ``{"routing": {...}}`` on withhold.
    """
    result = await dispatcher(tool_name, arguments)
    if client_ec_capable:
        return {"result": result}
    _warn_if_retired_switch_set()

    try:
        label_state, label_detail = _label_state(result)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED for the client sink
        reason = (
            f"withheld: the result's labels could not be read ({type(exc).__name__}) and "
            f"client {client_name!r} runs its model outside this deployment"
        )
        prov = RoutingProvenance.from_decision(
            decision=None, chosen_peer=None, forced_local=True,
            override_honored=False, refused=not tool_mutates, refused_peer=client_name,
            fail_safe=True, reason_override=reason,
            committed=tool_mutates, result_withheld=tool_mutates,
        )
        _audit_withhold(prov, tool_name=tool_name, client_name=client_name)
        return {"routing": prov.to_dict()}

    if label_state != "nonpublic":
        return {"result": result}

    regime = _label_regime(label_detail)
    reason = (
        f"withheld: result carries a non-public label ({', '.join(label_detail)}): "
        f"{_REGIME_WORDS[regime]}; client {client_name!r} runs its model outside this deployment"
    )
    # A mutation already ran (above): its side effect is committed. Keep the
    # RESULT from the client, but do not tell it the operation was refused.
    prov = RoutingProvenance.from_decision(
        decision=RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED, reason=reason, classifier="label"
        ),
        chosen_peer=None, forced_local=True, override_honored=False,
        refused=not tool_mutates, refused_peer=client_name, fail_safe=False,
        reason_override=reason, committed=tool_mutates, result_withheld=tool_mutates,
    )
    prov = replace(prov, tier=regime)
    _audit_withhold(prov, tool_name=tool_name, client_name=client_name)
    return {"routing": prov.to_dict()}


def _audit_withhold(prov: RoutingProvenance, *, tool_name: str, client_name: str) -> None:
    """Persist a sink-gate withhold keyed by the id the client is handed.

    Without this, the breadcrumb the client gets (``routing_event_id``) resolved
    nowhere: the enforced branch logged nothing, and the advisory path did not
    record the id. Best-effort — auditing a refusal must never raise into the
    refusal path. See ``audit explain``, which reads this back.
    """
    try:
        from axiom.infra.routing_audit import log_routing_decision

        log_routing_decision(
            routing_event_id=prov.routing_event_id,
            session_id="mcp-sink",
            tier=prov.tier,
            classifier=prov.classifier,
            matched_terms=list(prov.matched_terms),
            reason=f"{prov.reason}; tool={tool_name}; client={client_name}; "
            f"committed={prov.committed}; result_withheld={prov.result_withheld}",
        )
    except Exception:  # noqa: BLE001 - audit is best-effort
        pass


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
    "gate_result_for_client",
    "route_tool_call",
    "wrap_dispatcher",
]
