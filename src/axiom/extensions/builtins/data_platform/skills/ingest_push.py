# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``data.ingest_push`` — push items into bronze via the IngestSink core.

The MCP front door for the same push capability the HTTP ``POST /ingest``
endpoint exposes (ADR-079 "shared core, two views"). An agent (or any
MCP client) hands a ``source`` + a list of ``items``; each is routed
through the existing provenance-gated :class:`BronzeWriter`. Per-item
dispositions come back in ``value``.

Distinct from ``data.ingest`` (the *pull* path: poll a connector's source
and fetch). This skill is the *push* path: bytes are supplied inline.
"""

from __future__ import annotations

from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..ingest_sink import IngestSink, PushItem, decode_content, sink_for_connector


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    source = params.get("source")
    if not source:
        return SkillResult(ok=False, errors=["missing required param: source"])

    raw_items = params.get("items")
    if not raw_items:
        return SkillResult(ok=False, errors=["missing required param: items (non-empty list)"])

    # Build the sink. A caller may inject one (tests); otherwise resolve it
    # from a connector config (same bronze root + rules as the pull path).
    sink: IngestSink | None = params.get("sink")
    if sink is None:
        connector = params.get("connector")
        if not connector:
            return SkillResult(
                ok=False,
                errors=["missing param: connector (or inject a prebuilt 'sink')"],
            )
        sink = sink_for_connector(connector, state_dir=ctx.state_dir, store=params.get("store"))

    try:
        push_items = [
            PushItem(
                item_id=it["item_id"],
                content=decode_content(
                    it.get("content", ""), encoding=it.get("content_encoding", "text")
                ),
                content_type=it.get("content_type"),
                source_path=it.get("source_path"),
                display_name=it.get("display_name"),
                metadata=it.get("metadata") or {},
            )
            for it in raw_items
        ]
    except (KeyError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"malformed item: {exc}"])

    actions: list[str] = []
    with _authz.action(
        verb="ingest_push",
        resource=f"data-platform://ingest/{source}",
        classification=Classification.INTERNAL,
        actor=params.get("actor"),
    ) as act:
        actions.append(f"audit-receipt: {act.receipt_id}")
        result = sink.ingest(source, push_items)

    actions.append(
        f"push: accepted={result.accepted} landed={result.landed} "
        f"quarantined={result.quarantined} excluded={result.excluded} "
        f"errored={result.errored}"
    )

    return SkillResult(
        ok=result.errored == 0,
        value={
            "source": result.source,
            "accepted": result.accepted,
            "landed": result.landed,
            "quarantined": result.quarantined,
            "excluded": result.excluded,
            "errored": result.errored,
            "items": [
                {
                    "item_id": d.item_id,
                    "disposition": d.disposition,
                    "reason": d.reason,
                    "content_hash": d.content_hash,
                    "matched_rule": d.matched_rule,
                    "indexed": d.indexed,
                    "embed_skipped_reason": d.embed_skipped_reason,
                }
                for d in result.items
            ],
        },
        actions_taken=actions,
        errors=[f"{d.item_id}: {d.reason}" for d in result.items if d.disposition == "error"],
    )
