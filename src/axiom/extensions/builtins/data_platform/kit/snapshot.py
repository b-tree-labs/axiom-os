# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A snapshot is a point-in-time subset, kept after the sandbox is dropped.

ADR-195. The subset is a site, a list of channels and a time window. It is
written under ``data/snapshots/`` and ``kit-down`` does not remove that
directory. Materialize loads one snapshot into the sandbox ``kit-up`` stamped.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from .medallion import connect, dsn, require_stamp
from .project import KitError, KitProject

_ID = re.compile(r"[^a-z0-9-]+")

#: The ``schema_ref`` every materialized row carries. kit-try's fresh conform
#: deletes the rows it wrote earlier and keeps these.
SNAPSHOT_SCHEMA_REF = "kit:snapshot"


def _snapshot_id(site: str, window: dict[str, str]) -> str:
    raw = f"{site}-{window.get('start') or 'all'}-{window.get('end') or 'open'}"
    cleaned = _ID.sub("-", raw.lower()).strip("-")
    return cleaned or "snapshot"


def _in_window(ts: str, window: dict[str, str]) -> bool:
    """ISO-8601 strings compare in time order. ``end`` is exclusive."""
    start = window.get("start") or ""
    end = window.get("end") or ""
    if start and ts < start:
        return False
    if end and ts >= end:
        return False
    return True


def rows_to_load(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows a materialize writes, in a stable order."""
    rows = list(doc.get("rows") or [])
    return sorted(rows, key=lambda row: (str(row.get("ts")), str(row.get("channel")), str(row.get("feed"))))


def take_snapshot(
    project: KitProject,
    *,
    site: str,
    channels: list[str],
    rows: list[dict[str, Any]],
    window: dict[str, str] | None = None,
    snapshot_id: str = "",
) -> dict[str, Any]:
    """Keep the rows for ``site`` and ``channels`` inside ``window``.

    ``rows`` is the subset already pulled (a factory sample, or the answer of
    a data read). This function does not open a database and does not open
    the network.
    """
    if not site:
        raise KitError("a snapshot names the site it was pulled from")
    span = dict(window or {})
    chosen = [str(c) for c in channels]
    kept = [
        row
        for row in rows
        if str(row.get("site")) == site
        and (not chosen or str(row.get("channel")) in chosen)
        and _in_window(str(row.get("ts") or ""), span)
    ]
    if not kept:
        raise KitError(
            f"no rows for site {site!r}"
            + (f" channels {', '.join(chosen)}" if chosen else "")
            + " in that window"
        )
    ident = _ID.sub("-", (snapshot_id or _snapshot_id(site, span)).lower()).strip("-")
    if not ident:
        raise KitError("a snapshot needs an id of letters, digits and hyphens")
    doc = {
        "id": ident,
        "site": site,
        "channels": chosen,
        "window": span,
        "pulled_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": rows_to_load({"rows": kept}),
    }
    folder = project.folder("snapshots")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ident}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return doc


def load_snapshot(project: KitProject, snapshot_id: str) -> dict[str, Any]:
    path = project.folder("snapshots") / f"{snapshot_id}.json"
    if not path.is_file():
        raise KitError(f"no snapshot {snapshot_id!r} under {project.folder('snapshots')}")
    return json.loads(path.read_text(encoding="utf-8"))


def _row_hash(row: dict[str, Any]) -> str:
    packed = "|".join(
        str(row.get(key) or "") for key in ("site", "feed", "channel", "ts", "value", "unit")
    )
    return hashlib.sha256(packed.encode()).hexdigest()


def materialize(project: KitProject, snapshot_id: str) -> dict[str, Any]:
    """Load one snapshot into the sandbox ``kit-up`` stamped. The file stays."""
    doc = load_snapshot(project, snapshot_id)
    rows = rows_to_load(doc)
    target = dsn(project)
    require_stamp(target, "materialize")
    with connect(target) as conn, conn.cursor() as cur:
        for row in rows:
            # ADR-128 E4: a kept snapshot loaded into the sandbox's silver.
            cur.execute(
                """INSERT INTO silver.signals
                     (site, feed, channel, ts, value, unit, schema_ref, row_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (
                    row.get("site"),
                    row.get("feed") or "snapshot",
                    row.get("channel"),
                    row.get("ts"),
                    row.get("value"),
                    row.get("unit") or "",
                    SNAPSHOT_SCHEMA_REF,
                    _row_hash(row),
                ),
            )
    return {"snapshot": doc["id"], "rows": len(rows), "site": doc["site"]}
