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

**Sources** say, in words, where the data and the documents behind an answer come from, and whether
this is a real site at all. A bound laptop prints ``BOUND · <site> · data and documents from <host>``
when chat starts and names both sources under every reply; a machine on invented sample data
(``AXIOM_SAMPLE_DATA=1``) is marked TEST MODE at the start and on every reply.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from axiom.llm.placement import ProviderPlacement, placement_of

CONTENT_TIER_KEY = "content_tier"
_CONTROLLED = "export_controlled"

#: The one signal that this install answers from invented sample data rather than a real site: set it
#: to ``1`` (or ``true``/``yes``/``on``) on a demo, rehearsal or test machine. It wins over a binding
#: and a local store, because a sample answer that looks real is the failure worth preventing.
SAMPLE_DATA_ENV = "AXIOM_SAMPLE_DATA"

NO_DOCUMENTS = "none configured (bind to a site or set rag.database_url)"
NO_DATA = "none configured (bind to a site or set a database URL)"

BANNER_BOUND = "BOUND · {site} · data and documents from {host}"
BANNER_LOCAL = "LOCAL · this machine's data platform"
BANNER_TEST = "LOCAL TEST MODE · invented sample data, not a real site"


@dataclass(frozen=True)
class RetrievalWhere:
    kind: Literal["none", "local", "remote-node", "off"]
    host: str
    source: str  # "served" | "local" | "" (what the planes report called it)
    site: str = ""  # the bound site, when the site binding chose this store


@dataclass(frozen=True)
class Sources:
    """Where an answer's data and documents come from, in words a person can act on."""

    mode: Literal["bound", "local", "test"]
    data: str
    documents: str
    site: str = ""
    host: str = ""


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
    sources: Sources = field(default_factory=lambda: Sources("local", NO_DATA, NO_DOCUMENTS))


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
        try:
            from axiom.rag.store_factory import resolve_store_url

            site = resolve_store_url().site
        except Exception:  # noqa: BLE001
            site = ""
        return RetrievalWhere("remote-node", _host_of(plane.source), "served", site)
    if plane.kind == "local":
        return RetrievalWhere("local", "", "local")
    return RetrievalWhere("none", "", "")


def _host_of(source: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(source if "://" in source else f"//{source}").hostname or source
    except ValueError:
        return source


def sample_data_mode() -> bool:
    """True when this install declares it runs on invented sample data (:data:`SAMPLE_DATA_ENV`)."""
    return (os.environ.get(SAMPLE_DATA_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _data_where() -> tuple[str, str, str]:
    """``(description, site, host)`` for where a data read goes. Configuration only; never raises."""
    try:
        from axiom.extensions.builtins.data_platform.read_target import (
            holds_medallion,
            resolve_read_target,
        )

        target = resolve_read_target()
        if target.where == "binding":
            host = _host_of(target.face_url)
            return f"{target.site} via {host}", target.site, host
        if holds_medallion():
            return "this machine", "", ""
    except Exception:  # noqa: BLE001 - a report must never raise
        pass
    return NO_DATA, "", ""


def _documents_label(r: RetrievalWhere) -> str:
    if r.kind == "off":
        return "off for this session (/rag on)"
    if r.kind == "remote-node":
        return r.host or "a served retrieval endpoint"
    if r.kind == "local":
        return "this machine"
    return NO_DOCUMENTS


def sources_of(retrieval: RetrievalWhere | None = None) -> Sources:
    """Where data and documents come from, and whether this is a real site. Never raises."""
    retrieval = retrieval if retrieval is not None else _retrieval_where()
    data, site, host = _data_where()
    documents = _documents_label(retrieval)
    if sample_data_mode():
        return Sources("test", "invented sample data", documents)
    if site or retrieval.site:
        return Sources(
            "bound", data, documents, site or retrieval.site, host or retrieval.host
        )
    return Sources("local", data, documents)


def banner_line(sources: Sources | None = None) -> str:
    """The one line chat prints when it starts: BOUND, LOCAL or LOCAL TEST MODE."""
    sources = sources if sources is not None else sources_of()
    if sources.mode == "test":
        return BANNER_TEST
    if sources.mode == "bound":
        return BANNER_BOUND.format(site=sources.site, host=sources.host or "?")
    return BANNER_LOCAL


def _sources_text(mode: str, data: str, documents: str) -> list[str]:
    parts = [f"data: {data}", f"documents: {documents}"]
    return (["TEST MODE (invented sample data)"] + parts) if mode == "test" else parts


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
    sources = sources_of(retrieval)
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
            sources,
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
        sources=sources,
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


def _model_where(placement: str, host: str, verified: bool) -> str:
    """``LOCAL`` / ``CLOUD host`` / ``REMOTE host``, with a guess said in words rather than a bare ``?``."""
    word = {"local": "LOCAL", "cloud": "CLOUD", "remote-node": "REMOTE"}.get(placement, "model host unknown")
    text = word if placement == "local" or not host else f"{word} {host}"
    return text if verified else f"{text} (unverified)"


def render_stamp(
    report: WhereReport, *, cost: float | None = None, latency_ms: float | None = None
) -> str:
    """The line under a reply: what answered it and where, where its data and documents came from,
    plus cost and latency when known. Test mode leads the line."""
    if not report.provider:
        return "↳ no provider answered"
    s = report.sources
    documents = "off for this session (/rag on)" if report.retrieval.kind == "off" else s.documents
    model = report.model
    where = _model_where(report.placement.placement, report.placement.host, report.placement.verified)
    told = _sources_text(s.mode, s.data, documents)
    parts = [told[0], model, where, *told[1:]] if s.mode == "test" else [model, where, *told]
    parts[0] = f"↳ {parts[0]}"
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
        f"Sources     {banner_line(report.sources)}",
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
    """``<where the model runs> · data: <source> · documents: <source>``: the part of the stamp after the
    model, for renderers that print the model, tokens and cost themselves. Test mode leads the line."""
    model = _model_where(str(d.get("placement")), str(d.get("host") or ""), bool(d.get("verified", True)))
    sources = d.get("sources")
    if not isinstance(sources, dict):  # a dict from before sources were reported
        return f"{model} · RAG: {retrieval_from_dict(d)}"
    mode = str(sources.get("mode") or "local")
    documents = str(sources.get("documents") or NO_DOCUMENTS)
    if (d.get("retrieval") or {}).get("kind") == "off":
        documents = "off for this session (/rag on)"
    parts = _sources_text(mode, str(sources.get("data") or NO_DATA), documents)
    if mode == "test":
        return " · ".join([parts[0], model, *parts[1:]])
    return " · ".join([model, *parts])


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
        "retrieval": {
            "kind": report.retrieval.kind,
            "host": report.retrieval.host,
            "site": report.retrieval.site,
        },
        "sources": {
            "mode": report.sources.mode,
            "data": report.sources.data,
            "documents": report.sources.documents,
            "site": report.sources.site,
            "host": report.sources.host,
        },
        "override": report.override,
        "fallback": list(report.fallback),
    }
