# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""Label-aware MCP tool-routing layer: the peer gate and the client-sink gate.

Two gates, one rule (ADR-152, ADR-175, ADR-201 decisions 1, 4 and 16): a
stored or declared **label** decides where content may go; a model's verdict
only ever advises.

  1. The **peer gate** (:func:`route_tool_call`) decides whether a tool call
     may dispatch to a remote peer, and which one. A request whose arguments
     carry a non-public label (``access_tier`` / ``classification``), or an
     unlabelled request on a site whose default label is non-public, is
     refused before dispatch to a peer whose ``ec_eligible`` flag is False —
     the canonical case being a public-cloud relay such as Portkey/OpenAI.
     A public request that names such a peer may be *narrowed* to local
     dispatch by the detector's advice; it is never refused on it.
  2. The **client-sink gate** (:func:`gate_result_for_client`) withholds a
     tool *result* from a host client whose model runs outside the deployment
     when the result carries a non-public stored label. It does not classify
     result text at all (ADR-175).
  3. Both return a structured ``routing`` block so the end user can SEE where
     their request ran, what decided it, and what advised. Every block names
     the code revision of the server that produced it (ADR-202), and a
     withhold says whether that code is older than the install.
  4. Both emit a :class:`RoutingProvenance` payload that callers can persist
     onto a memory fragment's ``content`` (or hand to ``CompositionService``)
     without this module taking a hard dependency on the memory layer.

This module **never re-implements classification** — when it asks for advice
it consumes ``QueryRouter.classify``'s output directly, and the EC keyword
tables live in :mod:`axiom.infra.router`. It stays domain-agnostic.

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
# trails answer "this fragment came from EC-tier compute, decided by X"
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
    # A mutating tool runs locally BEFORE the gate reads its result's labels, so
    # by the time a withhold is decided the write has already happened. These
    # say so honestly: ``committed`` means the side effect took effect, and
    # ``result_withheld`` means only the OUTPUT was kept from a non-EC client.
    # For a read both stay False and ``refused`` carries the meaning as before.
    committed: bool = False
    result_withheld: bool = False
    #: The labels the verdict was taken from (``access_tier=…``,
    #: ``classification=…``, ``site_default=…``). Empty for a public verdict.
    labels: list[str] = field(default_factory=list)
    #: What the detector said when it was asked, and what that changed:
    #: ``effect`` is ``none``, ``rerouted_local`` or ``unavailable``. ``None``
    #: when it was not consulted. Advice never refuses (ADR-201 decision 16).
    advice: dict[str, Any] | None = None
    #: The code revision of the server that produced this block, and whether
    #: the install has moved since it loaded (ADR-202).
    server: dict[str, Any] | None = None
    #: Whether this decision reached the audit log, and why not if it did not
    #: (set on withholds and refusals; ``None`` where nothing was to record).
    audit_recorded: bool | None = None
    audit_error: str | None = None

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
        **extra: Any,
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
                **extra,
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
            **extra,
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
            "labels": list(self.labels),
            "advice": dict(self.advice) if self.advice is not None else None,
            "server": dict(self.server) if self.server is not None else None,
            "audit_recorded": self.audit_recorded,
            "audit_error": self.audit_error,
        }


# ---------------------------------------------------------------------------
# Free-text extraction — what we hand to the detector when advice is wanted
# ---------------------------------------------------------------------------


# String-typed argument keys we treat as classifiable free text. Conservative
# on purpose: a field named ``api_key`` or ``__peer__`` is operational, not
# user content. Tools that want richer classification can pass a ``text=``
# field explicitly (most already do).
_TEXT_ARG_NAMES = ("text", "query", "prompt", "input", "message", "content")


def _extract_classifiable_text(arguments: dict[str, Any]) -> str:
    """Pull free-text arguments out of a tool-call payload for the detector.

    Falls back to concatenating all string values if no canonical text-bearing
    field is found. Non-string values (numbers, dicts, lists) are skipped to
    keep the detector window focused on natural language.
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
# What may decide: a label, and nothing else
# ---------------------------------------------------------------------------
#
# Only a stored or declared LABEL on the content (``access_tier`` /
# ``classification``). The store adjudicated it at ingest, per document, so the
# sink gate reads that answer rather than guessing one; a request declares its
# own, or carries the site's default (ADR-201 decision 4).
#
# Text is not classified for export control to decide anything (ADR-175).
# Export control attaches to the controlled codes themselves and to their
# execution by a named person; it is enforced where the code is held and run
# (filesystem permissions and the job broker; see
# ``axiom.governance.controlled_code``). A code's NAME, and the inputs, outputs
# and results of running it, are not export controlled. A keyword list of code
# names matched against text, or a small model's guess about it, withheld
# exactly that ordinary material, so neither decides here any more. The
# detector may still ADVISE at the peer gate, and advice only narrows a route.
#
# Code that is itself controlled and has been ingested (a RAG chunk of its
# source, say) is withheld because it carries an export-control label from
# ingest, which is the code's own access boundary. Data under a different
# regime (proprietary, personal, safeguards) is withheld under its own label
# and reported as that regime.

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

#: The label an unlabelled request carries when the site declares none.
DEFAULT_SITE_LABEL = "public"


def _warn_if_retired_switch_set() -> None:
    if os.environ.get(SINK_SEMANTIC_ENV, "").strip():
        log.warning(
            "%s is set but no longer has any effect: the sink gate does not classify "
            "result text for export control (ADR-175); stored labels decide",
            SINK_SEMANTIC_ENV,
        )


def _collect_labels(obj: Any, depth: int = 0) -> list[tuple[str | None, str | None]]:
    """(access_tier, classification) of every labelled item in a payload."""
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

    A mixed payload is treated whole."""
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


def _request_verdict(arguments: dict[str, Any], default_label: str | None) -> tuple[str, list[str]]:
    """``(regime, labels)`` for a request: its declared labels joined to the
    strictest, or the site's default label when it declares none.

    ``regime`` is ``public`` or one of the withholding regimes. Unlabelled is
    not unknown (ADR-201 decision 4): on a public-only site the default is
    ``public`` and an unlabelled request is public.
    """
    state, detail = _label_state(arguments)
    if state == "nonpublic":
        return _label_regime(detail), detail
    if state == "public":
        return "public", []
    site = (default_label or DEFAULT_SITE_LABEL).strip().lower()
    if site in _PUBLIC_TIERS or site in _PUBLIC_CLASSIFICATIONS:
        return "public", []
    labels = [f"site_default={site}"]
    return _label_regime(labels), labels


def _server_block(*, check_stale: bool) -> dict[str, Any] | None:
    """The revision of this server and, when asked, whether the install has moved.

    Best-effort: a block that cannot be built is omitted, never a failure."""
    try:
        from axiom.infra import revision as _revision

        loaded = _revision.loaded_revision()
        block: dict[str, Any] = {
            "revision": loaded.short,
            "source": loaded.source,
            "stale": None,
            "current": None,
        }
        if check_stale:
            state = _revision.staleness()
            block["stale"] = state["stale"]
            block["current"] = state["current"]
        return block
    except Exception:  # noqa: BLE001 — an observation never breaks a route
        return None


# ---------------------------------------------------------------------------
# The peer gate
# ---------------------------------------------------------------------------


# A dispatcher is any async callable (tool_name, arguments) -> result.
Dispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _advise(router: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Ask the detector about the request text. Advice, never a verdict."""
    decision: RoutingDecision = router.classify(_extract_classifiable_text(arguments))
    return {
        "tier": decision.tier.value,
        "classifier": decision.classifier,
        "reason": decision.reason,
        "matched_terms": list(decision.matched_terms),
        "effect": "none",
    }


async def route_tool_call(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    dispatcher: Dispatcher,
    router: Any = None,  # duck-typed: anything with .classify(text) -> RoutingDecision
    peers: PeerRegistry,
    requested_peer: str | None = None,
    default_label: str | None = None,
) -> dict[str, Any]:
    """Run a tool call through the peer gate: a label decides, the detector advises.

    **Verdict** (labels only; ADR-152, ADR-175, ADR-201 decisions 1, 4 and 16).
    The request's declared labels (``access_tier`` / ``classification`` on any
    argument) join to the strictest; an unlabelled request carries the site's
    ``default_label`` (``public`` when none is declared). A non-public request
    is refused before dispatch to a peer that is not EC-eligible, and the
    refusal names the regime and the labels. A public request, and any request
    that names no peer, is never refused here. No model output is an input to
    the verdict.

    **Advice** (never a refusal). When a public request names a peer that is
    not EC-eligible and a ``router`` is given, the detector is asked about the
    request text. An export-control answer narrows the route: the call runs
    locally instead of at that peer, and the block records the advice and its
    effect. A detector that fails also removes the peer option, not the
    request. The detector is not consulted when nothing it says could change
    the route: no peer named, or an EC-eligible peer.

    Returns one of two shapes:

      Success::
        {
          "result":  <whatever the dispatcher returned>,
          "routing": {tier, classifier, labels, advice, ..., chosen_peer, ...},
        }

      Refusal (labelled non-public request pointed at a non-EC-eligible peer)::
        {
          "routing": {tier:<regime>, classifier:"label", refused:True, refused_peer:..., reason:...},
        }
        (No ``result`` key — the dispatcher was deliberately NOT invoked.)
    """
    regime, labels = _request_verdict(arguments, default_label)
    nonpublic = regime != "public"
    requested = peers.get(requested_peer) if requested_peer else None
    labelled = f" ({', '.join(labels)})" if labels else " (unlabelled; site default public)"

    def _block(**kw: Any) -> RoutingProvenance:
        base: dict[str, Any] = {
            "routing_event_id": str(uuid.uuid4()),
            "tier": regime,
            "classifier": "label",
            "matched_terms": [],
            "chosen_peer": None,
            "forced_local": True,
            "override_honored": False,
            "refused": False,
            "refused_peer": None,
            "fail_safe": False,
            "labels": list(labels),
            "server": _server_block(check_stale=False),
        }
        base.update(kw)
        return RoutingProvenance(**base)

    # ── No peer named: local dispatch. Nothing to decide, nothing to advise. ──
    if requested is None:
        prov = _block(reason=f"local dispatch; verdict={regime} by label{labelled}")
        result_payload = await dispatcher(tool_name, arguments)
        return {"result": result_payload, "routing": prov.to_dict()}

    # ── A labelled non-public request may not leave for a non-EC-eligible peer. ──
    if nonpublic and not requested.ec_eligible:
        reason = (
            f"refused: peer {requested.name!r} is not EC-eligible and the request carries "
            f"a non-public label{labelled}: {_REGIME_WORDS[regime]}"
        )
        prov = _block(
            reason=reason,
            refused=True,
            refused_peer=requested.name,
            server=_server_block(check_stale=True),
        )
        prov = _audit_withhold(
            prov, tool_name=tool_name, client_name=requested.name, session_id="mcp-peer"
        )
        return {"routing": prov.to_dict()}

    # ── Labelled content + EC-eligible peer: the correct route, not an override. ──
    if nonpublic:
        prov = _block(
            reason=f"labelled content + EC-eligible peer {requested.name!r}{labelled}",
            chosen_peer=requested.name,
            forced_local=False,
        )
        result_payload = await dispatcher(
            tool_name, {**arguments, "__peer__": requested.name}
        )
        return {"result": result_payload, "routing": prov.to_dict()}

    # ── Public request naming a peer: honour it, after advice where it matters. ──
    advice: dict[str, Any] | None = None
    if router is not None and not requested.ec_eligible:
        try:
            advice = _advise(router, arguments)
        except Exception as exc:  # noqa: BLE001 — advice unavailable removes the option, not the request
            log.warning(
                "routing: detector raised %s; peer %r removed, dispatching locally",
                type(exc).__name__, requested.name,
            )
            prov = _block(
                reason=(
                    f"public by label{labelled}; detector failed ({type(exc).__name__}); "
                    f"peer {requested.name!r} removed, served locally"
                ),
                fail_safe=True,
                advice={"effect": "unavailable", "error": type(exc).__name__},
                server=_server_block(check_stale=True),
            )
            result_payload = await dispatcher(tool_name, arguments)
            return {"result": result_payload, "routing": prov.to_dict()}

        if advice["tier"] == RoutingTier.EXPORT_CONTROLLED.value:
            advice["effect"] = "rerouted_local"
            keyword_tag = (
                f" (matched: {', '.join(advice['matched_terms'][:3])})"
                if advice["matched_terms"]
                else ""
            )
            prov = _block(
                reason=(
                    f"public by label{labelled}; detector advice "
                    f"(`{advice['classifier']}`: {advice['reason']}{keyword_tag}) removed peer "
                    f"{requested.name!r}, which is not EC-eligible; served locally"
                ),
                matched_terms=list(advice["matched_terms"]),
                advice=advice,
            )
            result_payload = await dispatcher(tool_name, arguments)
            return {"result": result_payload, "routing": prov.to_dict()}

    advised = (
        f"; detector advice: `{advice['classifier']}` {advice['reason']}" if advice else ""
    )
    prov = _block(
        reason=(
            f"public-tier override honored: user requested peer {requested.name!r}"
            f"{labelled}{advised}"
        ),
        chosen_peer=requested.name,
        forced_local=False,
        override_honored=True,
        advice=advice,
    )
    result_payload = await dispatcher(
        tool_name, {**arguments, "__peer__": requested.name}
    )
    return {"result": result_payload, "routing": prov.to_dict()}


# ---------------------------------------------------------------------------
# The client-sink gate
# ---------------------------------------------------------------------------


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
            server=_server_block(check_stale=True),
        )
        prov = _audit_withhold(prov, tool_name=tool_name, client_name=client_name)
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
        labels=list(label_detail), server=_server_block(check_stale=True),
    )
    prov = replace(prov, tier=regime)
    prov = _audit_withhold(prov, tool_name=tool_name, client_name=client_name)
    return {"routing": prov.to_dict()}


def _audit_withhold(
    prov: RoutingProvenance,
    *,
    tool_name: str,
    client_name: str,
    session_id: str = "mcp-sink",
) -> RoutingProvenance:
    """Persist a withhold or refusal keyed by the id the client is handed.

    Without this, the breadcrumb the client gets (``routing_event_id``) resolved
    nowhere: the enforced branch logged nothing, and the advisory path did not
    record the id. Auditing a refusal must never raise into the refusal path,
    but a failure is not swallowed either: the returned block says whether the
    decision is on record (``audit_recorded``) and why not (``audit_error``),
    and the failure is logged. See ``audit explain``, which reads this back.
    """
    try:
        from axiom.infra.routing_audit import log_routing_decision

        server = prov.server or {}
        written = log_routing_decision(
            routing_event_id=prov.routing_event_id,
            session_id=session_id,
            tier=prov.tier,
            classifier=prov.classifier,
            matched_terms=list(prov.matched_terms),
            reason=f"{prov.reason}; tool={tool_name}; client={client_name}; "
            f"committed={prov.committed}; result_withheld={prov.result_withheld}",
            extra={
                "tool": tool_name,
                "client": client_name,
                "labels": list(prov.labels),
                "revision": server.get("revision"),
                "current": server.get("current"),
                "stale": server.get("stale"),
            },
        )
        if written:
            return replace(prov, audit_recorded=True, audit_error=None)
        return replace(
            prov,
            audit_recorded=False,
            audit_error="the routing audit log could not be written; see the server log",
        )
    except Exception as exc:  # noqa: BLE001 - audit must never raise into the refusal path
        log.warning(
            "routing audit: %s was not recorded (%s: %s)",
            prov.routing_event_id, type(exc).__name__, exc,
        )
        return replace(
            prov, audit_recorded=False, audit_error=f"{type(exc).__name__}: {exc}"
        )


def wrap_dispatcher(
    dispatcher: Dispatcher,
    *,
    router: Any = None,
    peers: PeerRegistry,
    default_label: str | None = None,
) -> Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Return a routing-wrapped version of ``dispatcher``.

    Drop-in for ``axiom.extensions.builtins.mcp.server.dispatch_call``: pass
    the original dispatch coroutine in, get back one with the same call shape
    plus a ``routing`` block on every reply. Honors a ``__peer__`` key in the
    arguments dict as an explicit peer request (the MCP server should pop a
    matching tool-input field into that key before calling). ``default_label``
    is the site's label for an unlabelled request (ADR-201 decision 4).
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
            default_label=default_label,
        )

    return _wrapped


__all__ = [
    "DEFAULT_SITE_LABEL",
    "Dispatcher",
    "PeerDescriptor",
    "PeerRegistry",
    "RoutingProvenance",
    "gate_result_for_client",
    "route_tool_call",
    "wrap_dispatcher",
]
