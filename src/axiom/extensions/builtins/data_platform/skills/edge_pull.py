# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.edge_pull``: drain an ingest edge's outbox into this node's bronze (ADR-177).

Reads an ``edge`` connector (its URL, this node's credential reference and
an optional source map), resolves the credential from the vault, and pulls
every batch after the stored cursor into the local connector named for each
batch's source. Safe to run on a timer and safe to rerun: the cursor only
advances past a batch that is durable here, and rows deduplicate by hash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = (params.get("connector") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["missing required argument: connector (an `edge` connector)"])

    from ..agents.plinth.connectors import load_connector
    from ..ingest_sink import tabular_sink_for_connector
    from ..sources.edge import EdgeIntegrityError, EdgePuller, FileCursor, HttpEdge
    from ..sources.edge.provider import parse_source_map

    state_dir = Path(params["state_dir"]) if params.get("state_dir") else None
    try:
        config = load_connector(name, state_dir=state_dir)
    except FileNotFoundError:
        return SkillResult(ok=False, errors=[f"no connector named {name!r}"])
    if config.kind != "edge":
        return SkillResult(ok=False, errors=[f"{name!r} is a {config.kind!r} connector, not an edge"])
    url = str(config.params.get("edge_url", "")).strip()
    if not url or not config.credential_ref:
        return SkillResult(ok=False, errors=[f"{name!r} needs edge_url and credential_ref"])

    from axiom.extensions.builtins.secrets import SecretRef, resolve

    # The token is read here and handed to the client; it is never logged,
    # returned or written anywhere.
    with resolve(SecretRef.parse(config.credential_ref)) as secret:
        token = secret.as_str().strip()

    cursor_path = Path(config.bronze_root) / "_edge_cursor.json"
    sinks: dict[str, Any] = {}

    def sink_for(local: str):
        if local not in sinks:
            sinks[local] = tabular_sink_for_connector(local, state_dir=state_dir)
        return sinks[local]

    puller = EdgePuller(
        HttpEdge(url, token=token),
        sink_for=sink_for,
        cursor=FileCursor(cursor_path),
        source_map=parse_source_map(str(config.params.get("source_map", ""))),
    )
    try:
        report = puller.pull()
    except EdgeIntegrityError as exc:
        return SkillResult(ok=False, errors=[f"refused a batch that does not match its hash: {exc}"])
    except FileNotFoundError as exc:
        return SkillResult(ok=False, errors=[f"a pulled batch names a source with no local connector: {exc}"])
    except OSError as exc:
        # Unreachable edge, timeout or HTTP error: what landed is kept and the
        # next run resumes from the cursor.
        return SkillResult(ok=False, errors=[f"pull stopped: {exc}; landed batches are kept, rerun to resume"])

    value = {
        "connector": name,
        "records": report.records,
        "rows_in": report.rows_in,
        "rows_landed": report.rows_landed,
        "rows_duplicate": report.rows_duplicate,
        "cursor": report.after,
    }
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=[
            f"pulled {report.records} batches from {url}: {report.rows_landed} rows landed, "
            f"{report.rows_duplicate} already here; cursor at {report.after}"
        ],
    )
