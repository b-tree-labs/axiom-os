# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.activity`` — what the RAG ingested, per connector, since a time ago.

Answers "what new connector documents has the RAG processed since <when>?"
off the ``chunks.indexed_at`` timestamp. Connector attribution prefers the
``data_source`` column when present (the canonical schema), and falls back to
the ``source_path`` top-folder otherwise (robust to schema drift). Exposed as
a CLI verb (``axi data activity --since 24h``) and through the SkillRegistry
(so the same capability is reachable via MCP).
"""

from __future__ import annotations

import os
import re
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

# Folder → registered-connector label map. Used only for friendly display;
# attribution itself is by data_source/folder. The map is site configuration
# (folder names are a site's vocabulary, not the platform's): axiom ships no
# entries, and sites supply theirs through the ``connector_labels`` knob
# declared in this extension's ``config.schema.json`` (ADR-065). Folders
# without an entry pass through unchanged.
_CONFIG_KEY = "data_platform.connector_labels"
_CONFIG_FILENAME = "data_platform.toml"

_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def connector_by_folder() -> dict[str, str]:
    """Return the site-configured folder → connector-label map.

    Resolution order (never raises; empty map when unconfigured):

    1. The ``axiom.infra.config`` registry (the ADR-065 five-verb
       facade) — honors watcher- or API-written values.
    2. The installed config file ``<config-dir>/data_platform.toml``
       (the operator-durable home — same layering the notifications
       extension uses for ``herald.toml``), read through the config
       primitive's own ``load_config_file`` so the on-disk shape is
       exactly what the watcher would apply.

    Entries missing either key (or that aren't tables) are skipped.
    """
    entries: Any = None
    try:
        from axiom.infra.config import get_value

        entries = get_value(_CONFIG_KEY)
    except Exception:
        entries = None
    if not entries:
        try:
            from axiom.infra.config import default_config_dir, load_config_file

            path = default_config_dir() / _CONFIG_FILENAME
            entries = load_config_file(path).get(_CONFIG_KEY)
        except Exception:
            entries = None

    labels: dict[str, str] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        folder = entry.get("folder")
        connector = entry.get("connector")
        if folder and connector:
            labels[str(folder)] = str(connector)
    return labels


def _to_interval(since: str) -> str:
    """Normalize '24h' / '7d' / '90m' / '2w' → a Postgres interval string.
    Pass through anything that already looks like an interval."""
    s = (since or "24h").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([mhdw])", s)
    if m:
        return f"{m.group(1)} {_UNIT[m.group(2)]}"
    return s  # assume already a valid interval (e.g. '36 hours')


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    since = params.get("since", "24h")
    interval = _to_interval(since)
    connector = params.get("connector")  # optional filter

    dsn = os.environ.get("DP1_RAG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        return SkillResult(ok=False, errors=["DP1_RAG_DSN / DATABASE_URL unset"])

    try:
        import psycopg2
    except ImportError:
        return SkillResult(ok=False, errors=["psycopg2 not installed"])

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            # data_source column present (canonical schema) or drifted away?
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='chunks' AND column_name='data_source'"
            )
            has_ds = cur.fetchone() is not None
            key = ("data_source" if has_ds
                   else "split_part(source_path, '/', 2)")
            sql = (
                f"SELECT {key} AS connector, "
                "count(DISTINCT source_path) AS docs, count(*) AS chunks, "
                "max(indexed_at) AS latest "
                "FROM chunks "
                "WHERE indexed_at >= now() - %s::interval "
                "GROUP BY 1 ORDER BY 2 DESC"
            )
            cur.execute(sql, (interval,))
            rows = cur.fetchall()
    finally:
        conn.close()

    labels = connector_by_folder() if not has_ds else {}
    items = []
    for name, docs, chunks, latest in rows:
        label = labels.get(name, name) if not has_ds else name
        if connector and label != connector and name != connector:
            continue
        items.append({"connector": label, "docs": int(docs),
                      "chunks": int(chunks), "latest": str(latest)})

    total_docs = sum(i["docs"] for i in items)
    total_chunks = sum(i["chunks"] for i in items)
    return SkillResult(
        ok=True,
        value={"since": since, "interval": interval,
               "attribution": "data_source" if has_ds else "source_path",
               "total_docs": total_docs, "total_chunks": total_chunks,
               "items": items},
        actions_taken=[
            f"RAG ingest activity since {since}: {total_docs} docs / "
            f"{total_chunks} chunks across {len(items)} connector(s)"
        ],
    )
