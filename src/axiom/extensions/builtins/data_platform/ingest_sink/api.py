# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""FastAPI front door for the push :class:`~.core.IngestSink`.

``POST /ingest`` accepts a JSON body ``{source, items:[...]}`` and routes
each item through the shared :class:`IngestSink` core (the same core the
``data.ingest_push`` skill calls). The per-facility egress agent (PRD
RDQ-001) is the canonical client: it POSTs outbound, no inbound holes.

This module only builds the router/app — it never binds a port. The
``serve`` runner (``axiom.extensions.builtins.http.server.run_server``)
is what a deployment calls to bind; tests drive the app with FastAPI's
``TestClient`` (in-process, no socket).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from axiom.extensions.builtins.http.server import create_app

from .core import IngestSink, PushItem, decode_content
from .summary import build_ingest_summary_router
from .tabular import PushRowBatch, TabularIngestSink
from .tenancy import IngestGrant, TenancyPolicy, TenancyRefused

if TYPE_CHECKING:
    from axiom.infra.ratelimit import BucketRegistry


# Request-shape caps (DoS bound). The endpoint accepts external pushes, so an
# unbounded `items` list or per-item `content` lets one POST exhaust memory.
# Reject oversized requests at validation (422) before decode/ingest. Tune via
# the env knobs for high-throughput egress agents; the defaults suit a single
# facility agent batching modest documents.
_MAX_ITEMS = int(os.environ.get("AXIOM_INGEST_MAX_ITEMS", "1000"))
# ~8 MB of (possibly base64) content per item; base64 inflates ~4/3, so the
# decoded payload is ~6 MB. Generous for documents, bounded against abuse.
_MAX_CONTENT_CHARS = int(os.environ.get("AXIOM_INGEST_MAX_CONTENT_CHARS", str(8 * 1024 * 1024)))

# Row-lane caps (ADR-106 §6), the tabular peers of the two above. Enforced in the
# handler rather than as pydantic field bounds so an operator (or a test) can
# retune them without re-importing the module — the row lane's producers are
# long-lived streams whose batching window varies by source.
_MAX_BATCHES = int(os.environ.get("AXIOM_INGEST_MAX_BATCHES", "500"))
_MAX_ROWS_PER_BATCH = int(os.environ.get("AXIOM_INGEST_MAX_ROWS_PER_BATCH", "50000"))


def _tenant_metadata(
    policy: TenancyPolicy, grant: IngestGrant, metadata: dict[str, str]
) -> dict[str, str]:
    """§5.2 at the face: refuse a foreign site or an over-ceiling tier, stamp the
    credential's site. Pure policy → HTTP status."""
    try:
        return policy.check(grant, metadata)
    except TenancyRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


class IngestItemModel(BaseModel):
    """One item on the push request body."""

    item_id: str = Field(max_length=1024)
    content: str = Field(default="", max_length=_MAX_CONTENT_CHARS)
    content_encoding: str = Field(default="text", description="'text' or 'base64'")
    content_type: str | None = Field(default=None, max_length=255)
    source_path: str | None = Field(default=None, max_length=4096)
    display_name: str | None = Field(default=None, max_length=1024)
    metadata: dict[str, str] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    source: str = Field(max_length=255)
    items: list[IngestItemModel] = Field(max_length=_MAX_ITEMS)


def build_ingest_router(
    sink: IngestSink | None = None,
    *,
    sink_resolver: Callable[[str], IngestSink] | None = None,
    tenancy: TenancyPolicy | None = None,
) -> APIRouter:
    """Build the ``/ingest`` router.

    Pass either a static ``sink`` (one writer for all sources) or a
    ``sink_resolver`` that maps the request's ``source`` (connector name) to a
    connector-specific :class:`IngestSink`. The resolver path is preferred for
    the composed serving mount so HTTP pushes land in the same bronze tree under
    the same provenance rules as the pull/CDC/scheduled paths (no split brain). A
    resolver ``KeyError`` (unknown connector) becomes a loud 422 rather than a
    silent quarantine into a rule-less tree.

    ``tenancy`` (default: :meth:`TenancyPolicy.from_env`) resolves the site and
    tier ceiling from the request's principal and refuses a payload outside that
    grant (spec §5.2) — the item's ``metadata.site`` is stamped from the
    credential, never trusted from the body.
    """
    if sink is None and sink_resolver is None:
        raise ValueError("build_ingest_router requires a sink or a sink_resolver")
    policy = tenancy if tenancy is not None else TenancyPolicy.from_env()
    router = APIRouter()

    def _resolve(source: str) -> IngestSink:
        if sink_resolver is not None:
            try:
                return sink_resolver(source)
            except KeyError as exc:
                raise HTTPException(
                    status_code=422, detail=f"unknown connector/source: {source!r}"
                ) from exc
        return sink  # type: ignore[return-value]

    @router.post("/ingest")
    def ingest(req: IngestRequest, request: Request) -> dict:
        if not req.source:
            raise HTTPException(status_code=422, detail="source is required")
        grant = policy.grant_for(request)
        target = _resolve(req.source)
        try:
            push_items = [
                PushItem(
                    item_id=m.item_id,
                    content=decode_content(m.content, encoding=m.content_encoding),
                    content_type=m.content_type,
                    source_path=m.source_path,
                    display_name=m.display_name,
                    metadata=_tenant_metadata(policy, grant, m.metadata),
                )
                for m in req.items
            ]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        result = target.ingest(req.source, push_items)
        return asdict(result)

    return router


def create_ingest_app(sink: IngestSink) -> FastAPI:
    """Standalone app exposing only the ingest endpoint."""
    app = create_app(
        title="Axiom Data Platform — IngestSink",
        version="0.1.0",
        description="Push-first bronze ingest endpoint (ADR-079 §8.4.1).",
    )
    app.include_router(build_ingest_router(sink))
    return app


# -- row lane (ADR-106): the tabular peer of everything above ---------------


class PushRowBatchModel(BaseModel):
    """One tabular batch on a push request body."""

    item_id: str = Field(max_length=1024)
    schema_ref: str = Field(max_length=1024, description="declared schema id these rows fill")
    rows: list[dict]
    etag: str | None = Field(default=None, max_length=1024)
    source_path: str | None = Field(default=None, max_length=4096)
    metadata: dict[str, str] = Field(default_factory=dict)


class TabularIngestRequest(BaseModel):
    source: str = Field(max_length=255)
    batches: list[PushRowBatchModel]



#: Requests a site may burst, and the sustained rate it refills at.
#:
#: Deployment-tunable and deliberately generous: this exists to stop a looping
#: producer or a large backlog drain from taking the node's disk down, not to
#: ration a partner's normal traffic. A site under the rate never sees it.
_INGEST_BURST = float(os.environ.get("AXIOM_INGEST_BURST", "120"))
_INGEST_PER_SECOND = float(os.environ.get("AXIOM_INGEST_RATE_PER_SECOND", "20"))


def _default_bucket_registry():
    """One registry per router, from the environment.

    Per router rather than a module global so two mounts in one process — a
    test and a real one, or two tenants' faces — cannot spend each other's
    budget through a shared dict.
    """
    from axiom.infra.ratelimit import BucketRegistry

    return BucketRegistry(
        capacity=_INGEST_BURST, refill_per_second=_INGEST_PER_SECOND
    )


def build_tabular_ingest_router(
    sink: TabularIngestSink | None = None,
    *,
    sink_resolver: Callable[[str], TabularIngestSink] | None = None,
    tenancy: TenancyPolicy | None = None,
    limits: BucketRegistry | None = None,
) -> APIRouter:
    """Build the ``/ingest/rows`` router — the row lane's front door.

    The document lane's peer (:func:`build_ingest_router`), with the same
    static-sink / per-source-resolver choice and the same loud 422 for an unknown
    connector. Batches are shaped into :class:`PushRowBatch` and driven through
    :class:`TabularIngestSink`, so a pushed batch lands through the same
    ``TabularBronzeWriter`` — same provenance gate, same row ``content_hash``
    dedup — as one the pull/CDC path fetched. No second write path.

    Delivery is at-least-once and idempotent by that dedup (ADR-106 §4): a
    producer replaying its spool after an outage lands its rows once.

    Tenancy (spec §5.2) is the document lane's: ``site`` and the tier ceiling
    come from the credential via ``tenancy``; a batch asserting another site or
    a tier above the ceiling is 403 before anything is resolved or written.
    """
    if sink is None and sink_resolver is None:
        raise ValueError("build_tabular_ingest_router requires a sink or a sink_resolver")
    policy = tenancy if tenancy is not None else TenancyPolicy.from_env()
    buckets = limits if limits is not None else _default_bucket_registry()
    router = APIRouter()

    def _resolve(source: str) -> TabularIngestSink:
        if sink_resolver is not None:
            try:
                return sink_resolver(source)
            except KeyError as exc:
                raise HTTPException(
                    status_code=422, detail=f"unknown connector/source: {source!r}"
                ) from exc
        return sink  # type: ignore[return-value]

    @router.post("/ingest/rows")
    def ingest_rows(
        req: TabularIngestRequest, request: Request, response: Response
    ) -> dict:
        if not req.source:
            raise HTTPException(status_code=422, detail="source is required")
        # Bound the request shape before any resolve or write, so an oversized
        # push costs a validation error rather than memory.
        if len(req.batches) > _MAX_BATCHES:
            raise HTTPException(
                status_code=422,
                detail=f"too many batches: {len(req.batches)} > {_MAX_BATCHES}",
            )
        for b in req.batches:
            if len(b.rows) > _MAX_ROWS_PER_BATCH:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"batch {b.item_id!r} has too many rows: "
                        f"{len(b.rows)} > {_MAX_ROWS_PER_BATCH}"
                    ),
                )

        # Tenancy before resolve: a foreign-site or over-ceiling batch never
        # touches a sink (§5.3 orders the refusals before any write).
        grant = policy.grant_for(request)

        # Admission control AFTER the grant, because the budget is per site and
        # the site comes from the credential — keying it on anything in the
        # body would let a caller spend another site's budget by claiming to be
        # them. Before any resolve or write, because the whole point is to cost
        # a refused caller nothing.
        #
        # The headers ride on EVERY answer, not only a refusal: a caller that
        # first learns its budget when rejected has already been rejected, and
        # our own transmitter paces on the published number.
        admitted, limit_headers = buckets.check(grant.site or "<unattributed>")
        if not admitted:
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded for this site",
                headers=limit_headers,
            )
        response.headers.update(limit_headers)

        # Heartbeats ride the same authenticated path but are about the node,
        # not data: kept per site and node, never written as rows.
        from .heartbeat import HEARTBEAT_SCHEMA, HeartbeatStore

        beats = [b for b in req.batches if b.schema_ref == HEARTBEAT_SCHEMA]
        if beats:
            store = HeartbeatStore.from_env()
            site_key = grant.site or "<unattributed>"
            for b in beats:
                for row in b.rows:
                    try:
                        store.record(site_key, row)
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=f"heartbeat: {exc}") from exc
            data_batches = [b for b in req.batches if b.schema_ref != HEARTBEAT_SCHEMA]
            n_beats = sum(len(b.rows) for b in beats)
            if not data_batches:
                return {"source": req.source, "accepted": 0, "landed": 0, "excluded": 0,
                        "errored": 0, "rows_in": 0, "rows_landed": 0, "rows_duplicate": 0,
                        "funnel": None, "heartbeats": n_beats}
            req = TabularIngestRequest(source=req.source, batches=data_batches)
        else:
            n_beats = 0

        stamped = [_tenant_metadata(policy, grant, b.metadata) for b in req.batches]
        target = _resolve(req.source)
        push_batches = [
            PushRowBatch(
                item_id=b.item_id,
                schema_ref=b.schema_ref,
                rows=b.rows,
                etag=b.etag,
                source_path=b.source_path,
                metadata=meta,
            )
            for b, meta in zip(req.batches, stamped, strict=True)
        ]
        result = asdict(target.ingest_rows(req.source, push_batches))
        if n_beats:
            result["heartbeats"] = n_beats
        return result

    @router.get("/ingest/heartbeat")
    def heartbeat_seen(request: Request) -> dict:
        """What this platform last heard from the caller's site's nodes.

        The sending side's evidence that its beats arrive: the site comes from
        the credential, as for a write, so a caller sees its own site only.
        """
        from .heartbeat import HeartbeatStore, liveness

        grant = policy.grant_for(request)
        site_key = grant.site or "<unattributed>"
        nodes = HeartbeatStore.from_env().nodes(site_key)
        return {
            "site": grant.site,
            "nodes": [{**n, "state": liveness(n)} for n in nodes],
        }

    return router


def create_tabular_ingest_app(
    sink: TabularIngestSink,
    *,
    summarize: Callable[[str, float | None], dict[str, Any]] | None = None,
    count_channels: Callable[..., list[dict[str, Any]]] | None = None,
) -> FastAPI:
    """The row lane, and the read side that answers for it.

    The summary router is mounted here rather than left to a caller. It was
    written, unit-tested and included by nothing: the only reference to
    `build_ingest_summary_router` outside its own module was its own test, so
    `GET /ingest/summary` existed in the tree and on no running app.

    The cost of that fell on the last rung of a partner's walk. `neut daq
    verify` asks this endpoint whether rows actually landed — accepted is not
    landed — and against any face built here it got a 404. Worse, it explains
    a 404 by saying the face "predates the read side", which would have sent a
    partner to upgrade a node that was already current.

    `summarize` and `count_channels` are passed through so a caller can supply
    the queries; the endpoint's own job is tenancy and shape.
    """
    app = create_app(
        title="Axiom Data Platform — TabularIngestSink",
        version="0.1.0",
        description="Push-first bronze row ingest endpoint (ADR-106).",
    )
    app.include_router(build_tabular_ingest_router(sink))
    app.include_router(
        build_ingest_summary_router(summarize=summarize, count_channels=count_channels)
    )
    return app


__all__ = [
    "IngestItemModel",
    "IngestRequest",
    "PushRowBatchModel",
    "TabularIngestRequest",
    "build_ingest_router",
    "build_tabular_ingest_router",
    "create_ingest_app",
    "create_tabular_ingest_app",
]
