# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a chat answer comes from, in one report (ADR-168).

``where_report(agent)`` is the only place placement is computed for the chat surfaces. The status
indicator, ``/where``, ``/status``, ``/model``, the reply stamp and the MCP tool all read it, so they cannot
disagree.

Two truths are kept apart. **Generation** is the provider that answers: after a turn it is the one the
gateway actually selected (``gateway.last_selected``, not ``active_provider``, which is only the first
usable provider and ignores an override and the request's tier); before a turn it is a labelled
prediction. **Retrieval** is where the grounding documents come from, which can differ: binding to a
site node chooses the retrieval store, not the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from axiom.llm.placement import ProviderPlacement, placement_of

CONTENT_TIER_KEY = "content_tier"
_CONTROLLED = "export_controlled"


@dataclass(frozen=True)
class RetrievalWhere:
    kind: Literal["none", "local", "remote-node", "off"]
    host: str
    source: str  # "served" | "local" | "" (what the planes report called it)


@dataclass(frozen=True)
class WhereReport:
    provider: str
    model: str
    placement: ProviderPlacement
    basis: Literal["answered", "predicted"]
    tier: str  # the provider's routing tier
    content_tier: str  # the tier of what this session already holds
    retrieval: RetrievalWhere
    override: str | None
    fallback: tuple[str, ...]


# ---- the session's content tier -----------------------------------------------------------------


def content_tier_of(session: Any) -> str:
    """The most restricted tier of anything this session has held. Controlled content never un-holds."""
    ctx = getattr(session, "context", None) or {}
    return str(ctx.get(CONTENT_TIER_KEY, "any"))


def note_content_tier(session: Any, tier: str) -> None:
    """Record a turn's classified tier. Sticky: once controlled, always controlled for this session."""
    ctx = getattr(session, "context", None)
    if ctx is None:
        return
    current = str(ctx.get(CONTENT_TIER_KEY, "any"))
    if current == _CONTROLLED:
        return
    if tier == _CONTROLLED or current == "any":
        ctx[CONTENT_TIER_KEY] = tier


# ---- the report ---------------------------------------------------------------------------------


def _retrieval_where() -> RetrievalWhere:
    try:
        from .planes import resolve_domain

        plane = resolve_domain()
    except Exception:  # noqa: BLE001 - a report must never raise
        return RetrievalWhere("none", "", "")
    if plane.kind == "served":
        return RetrievalWhere("remote-node", _host_of(plane.source), "served")
    if plane.kind == "local":
        return RetrievalWhere("local", "", "local")
    return RetrievalWhere("none", "", "")


def _host_of(source: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(source if "://" in source else f"//{source}").hostname or source
    except ValueError:
        return source


def where_report(agent: Any) -> WhereReport:
    """Build the report from an agent (duck-typed: it needs ``.gateway`` and ``.session``). Never raises."""
    gateway = agent.gateway
    content = content_tier_of(getattr(agent, "session", None))
    chosen = getattr(gateway, "last_selected", None)
    basis: Literal["answered", "predicted"] = "answered" if chosen is not None else "predicted"
    if chosen is None:
        try:
            chosen = gateway.preview_provider(content)
        except Exception:  # noqa: BLE001
            chosen = None
    override = getattr(gateway, "_provider_override", None)
    # /rag off is a session setting, so it wins over what the planes report about the store.
    session_context = getattr(getattr(agent, "session", None), "context", None) or {}
    retrieval = (
        RetrievalWhere("off", "", "turned off for this session")
        if session_context.get("rag") == "off"
        else _retrieval_where()
    )
    fallback = tuple(
        p.name for p in getattr(gateway, "providers", []) if chosen is None or p.name != chosen.name
    )
    if chosen is None:
        return WhereReport(
            "",
            "",
            ProviderPlacement("remote-node", "", False, "derived"),
            basis,
            "",
            content,
            retrieval,
            override,
            fallback,
        )
    return WhereReport(
        provider=chosen.name,
        model=chosen.model,
        placement=placement_of(chosen),
        basis=basis,
        tier=chosen.routing_tier,
        content_tier=content,
        retrieval=retrieval,
        override=override,
        fallback=fallback,
    )


# ---- renderings (plain text; a surface may colour them) -----------------------------------------


def _retrieval_label(r: RetrievalWhere) -> str:
    if r.kind == "off":
        return "OFF"
    if r.kind == "none":
        return "none"
    if r.kind == "remote-node":
        return f"REMOTE {r.host}" if r.host else "REMOTE"
    return "LOCAL"


def render_indicator(report: WhereReport) -> str:
    """One line for an always-visible spot: the model and where it runs, and the retrieval source."""
    if not report.provider:
        return "no model available"
    mark = "" if report.basis == "answered" else " (next)"
    return f"{report.model} · {report.placement.label()} · RAG {_retrieval_label(report.retrieval)}{mark}"


def render_stamp(
    report: WhereReport, *, cost: float | None = None, latency_ms: float | None = None
) -> str:
    """The line under a reply: what answered it and where, plus cost and latency when known."""
    if not report.provider:
        return "↳ no provider answered"
    parts = [
        f"↳ {report.model}",
        report.placement.label(),
        f"RAG: {_retrieval_label(report.retrieval)}",
    ]
    if cost is not None:
        parts.append(f"${cost:.3f}")
    if latency_ms is not None:
        parts.append(f"{int(latency_ms)}ms")
    return " · ".join(parts)


def render_block(report: WhereReport) -> str:
    """The multi-line block for ``/where`` and the head of ``/status``."""
    if not report.provider:
        return "No model is available: no provider is configured or none has its key."
    basis = (
        "answered the last turn" if report.basis == "answered" else "predicted for the next turn"
    )
    lines = [
        f"Model       {report.model}  ({report.provider}, {basis})",
        f"Runs        {report.placement.label()}"
        + (
            ""
            if report.placement.verified
            else "  (inferred from the endpoint; set placement in the provider config to confirm)"
        ),
        f"Routing     provider tier {report.tier}; this session holds {report.content_tier} content",
        f"Retrieval   {_retrieval_label(report.retrieval)}",
    ]
    if report.override:
        lines.append(f"Override    pinned to {report.override}")
    if report.fallback:
        lines.append(f"Fallback    {', '.join(report.fallback)}")
    return "\n".join(lines)


def label_from_dict(d: dict[str, Any]) -> str:
    """``LOCAL`` / ``CLOUD api.example.com`` / ``REMOTE node.example.edu`` from the structured form."""
    placement = {"local": "LOCAL", "cloud": "CLOUD", "remote-node": "REMOTE"}.get(str(d.get("placement")), "?")
    mark = "" if d.get("verified", True) else "?"
    host = str(d.get("host") or "")
    return placement + mark if placement == "LOCAL" or not host else f"{placement}{mark} {host}"


def retrieval_from_dict(d: dict[str, Any]) -> str:
    r = d.get("retrieval") or {}
    kind, host = r.get("kind", "none"), r.get("host", "")
    if kind == "off":
        return "OFF"
    if kind == "none":
        return "none"
    return "LOCAL" if kind == "local" else (f"REMOTE {host}" if host else "REMOTE")


def stamp_from_dict(d: dict[str, Any]) -> str:
    """``<LABEL> · RAG: <source>``: the part of the stamp after the model, for renderers that print the
    model, tokens and cost themselves."""
    return f"{label_from_dict(d)} · RAG: {retrieval_from_dict(d)}"


def to_dict(report: WhereReport) -> dict[str, Any]:
    """The JSON the MCP tool returns."""
    return {
        "provider": report.provider,
        "model": report.model,
        "placement": report.placement.placement,
        "host": report.placement.host,
        "verified": report.placement.verified,
        "placement_source": report.placement.source,
        "basis": report.basis,
        "tier": report.tier,
        "content_tier": report.content_tier,
        "retrieval": {"kind": report.retrieval.kind, "host": report.retrieval.host},
        "override": report.override,
        "fallback": list(report.fallback),
    }
