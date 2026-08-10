# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Cluster-3 absorb adapter — vector/passage stores (ADR-087 D8;
harness-memory survey §3).

Passage ingest with provenance mapping. Letta self-hosted is the first
target and the reference: archival passages carry the field's richest
per-passage provenance (own id, created_at, agent binding, metadata),
plus persona/human core blocks. Table names churn across releases
(``agent_passages`` / ``source_passages`` currently; ``passages`` in
older installs), so the reader probes for known shapes and degrades to
skip-with-record when none match.

Two laws specific to this cluster:

- **Provenance maps, embeddings don't.** The passage's own id becomes
  ``source_ref``; timestamps/agent/metadata ride into content. Stored
  vectors are disposable projections (D6) — they are never selected,
  never absorbed; the destination re-embeds from authoritative text.
- Read-only, like every adapter: URI ``mode=ro`` handles only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .base import AbsorbScan, FragmentCandidate, SkippedSource
from .structured_store import StructuredStoreAdapter, _connect_readonly

_PASSAGE_TABLES = ("agent_passages", "source_passages", "passages")
_BLOCK_TABLES = ("block", "blocks")

_PASSAGE_OPTIONAL = ("created_at", "agent_id", "source_id", "metadata_", "metadata")


def _first_line(text: str, limit: int = 120) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def letta_adapter(
    *, account: str, db_path: Path | None = None
) -> StructuredStoreAdapter:
    """Letta self-hosted SQLite: archival passages + core blocks."""
    db = (
        Path(db_path)
        if db_path is not None
        else Path.home() / ".letta" / "sqlite.db"
    )
    adapter = StructuredStoreAdapter(
        harness="letta", account=account, expected_locations=[db],
    )

    def _tables_and_columns(
        con: sqlite3.Connection,
    ) -> dict[str, list[str]]:
        names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        out: dict[str, list[str]] = {}
        for name in names:
            cols = [
                row[1]
                for row in con.execute(f"PRAGMA table_info({name})")
            ]
            out[name] = cols
        return out

    def _passage_candidate(table: str, rec: dict) -> FragmentCandidate | None:
        text = (rec.get("text") or "").strip()
        pid = rec.get("id")
        if not text or not pid:
            return None
        content = {
            "summary": _first_line(text),
            "text": text,
            "layer": "auto_memory",
            "fact_kind": "letta_passage",
        }
        if rec.get("agent_id"):
            content["agent_id"] = rec["agent_id"]
        if rec.get("source_id"):
            content["source_id"] = rec["source_id"]
        created = rec.get("created_at")
        if isinstance(created, str) and created:
            content["event_time"] = created
        raw_meta = rec.get("metadata_") or rec.get("metadata")
        if isinstance(raw_meta, str) and raw_meta:
            try:
                parsed = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    content["metadata"] = parsed
            except json.JSONDecodeError:
                pass
        return FragmentCandidate(
            content=content,
            cognitive_type="semantic",
            origin=adapter.origin(f"{db}/{table}/{pid}"),
        )

    def _block_candidate(table: str, rec: dict) -> FragmentCandidate | None:
        value = (rec.get("value") or "").strip()
        bid = rec.get("id")
        if not value or not bid:
            return None
        return FragmentCandidate(
            content={
                "summary": _first_line(value),
                "text": value,
                "label": rec.get("label", ""),
                "layer": "auto_memory",
                "fact_kind": "letta_block",
            },
            cognitive_type="core",
            origin=adapter.origin(f"{db}/{table}/{bid}"),
        )

    def _read(scan: AbsorbScan) -> None:
        if not db.is_file():
            return
        try:
            con = _connect_readonly(db)
        except sqlite3.Error as exc:
            scan.skipped.append(
                SkippedSource(source=str(db), reason=f"unreadable: {exc}")
            )
            return
        try:
            try:
                schema = _tables_and_columns(con)
            except sqlite3.Error as exc:
                scan.skipped.append(
                    SkippedSource(source=str(db), reason=f"unreadable: {exc}")
                )
                return

            matched = False
            for table in _PASSAGE_TABLES:
                cols = schema.get(table)
                if not cols or "id" not in cols or "text" not in cols:
                    continue
                matched = True
                selected = ["id", "text"] + [
                    c for c in _PASSAGE_OPTIONAL if c in cols
                ]
                try:
                    rows = con.execute(
                        f"SELECT {', '.join(selected)} FROM {table}"
                    ).fetchall()
                except sqlite3.Error as exc:
                    scan.skipped.append(SkippedSource(
                        source=f"{db}/{table}",
                        reason=f"schema_drift: {exc}",
                    ))
                    continue
                for row in rows:
                    cand = _passage_candidate(table, dict(zip(selected, row)))
                    if cand is not None:
                        scan.candidates.append(cand)

            for table in _BLOCK_TABLES:
                cols = schema.get(table)
                if not cols or "id" not in cols or "value" not in cols:
                    continue
                matched = True
                selected = ["id", "value"] + (
                    ["label"] if "label" in cols else []
                )
                try:
                    rows = con.execute(
                        f"SELECT {', '.join(selected)} FROM {table}"
                    ).fetchall()
                except sqlite3.Error as exc:
                    scan.skipped.append(SkippedSource(
                        source=f"{db}/{table}",
                        reason=f"schema_drift: {exc}",
                    ))
                    continue
                for row in rows:
                    cand = _block_candidate(table, dict(zip(selected, row)))
                    if cand is not None:
                        scan.candidates.append(cand)

            if not matched:
                scan.skipped.append(SkippedSource(
                    source=str(db),
                    reason=(
                        "no known passage or block tables found "
                        "(schema drift — update the survey + adapter)"
                    ),
                ))
        finally:
            con.close()

    adapter.readers = [(str(db), _read)]
    return adapter


__all__ = ["letta_adapter"]
