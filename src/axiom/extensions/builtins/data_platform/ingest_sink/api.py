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

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from axiom.extensions.builtins.http.server import create_app

from .core import IngestSink, PushItem, decode_content

# Request-shape caps (DoS bound). The endpoint accepts external pushes, so an
# unbounded `items` list or per-item `content` lets one POST exhaust memory.
# Reject oversized requests at validation (422) before decode/ingest. Tune via
# the env knobs for high-throughput egress agents; the defaults suit a single
# facility agent batching modest documents.
_MAX_ITEMS = int(os.environ.get("AXIOM_INGEST_MAX_ITEMS", "1000"))
# ~8 MB of (possibly base64) content per item; base64 inflates ~4/3, so the
# decoded payload is ~6 MB. Generous for documents, bounded against abuse.
_MAX_CONTENT_CHARS = int(os.environ.get("AXIOM_INGEST_MAX_CONTENT_CHARS", str(8 * 1024 * 1024)))


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
) -> APIRouter:
    """Build the ``/ingest`` router.

    Pass either a static ``sink`` (one writer for all sources) or a
    ``sink_resolver`` that maps the request's ``source`` (connector name) to a
    connector-specific :class:`IngestSink`. The resolver path is preferred for
    the composed serving mount so HTTP pushes land in the same bronze tree under
    the same provenance rules as the pull/CDC/Dagster paths (no split brain). A
    resolver ``KeyError`` (unknown connector) becomes a loud 422 rather than a
    silent quarantine into a rule-less tree.
    """
    if sink is None and sink_resolver is None:
        raise ValueError("build_ingest_router requires a sink or a sink_resolver")
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
    def ingest(req: IngestRequest) -> dict:
        if not req.source:
            raise HTTPException(status_code=422, detail="source is required")
        target = _resolve(req.source)
        try:
            push_items = [
                PushItem(
                    item_id=m.item_id,
                    content=decode_content(m.content, encoding=m.content_encoding),
                    content_type=m.content_type,
                    source_path=m.source_path,
                    display_name=m.display_name,
                    metadata=m.metadata,
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


__all__ = ["IngestRequest", "IngestItemModel", "build_ingest_router", "create_ingest_app"]
