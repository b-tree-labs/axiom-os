# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Cluster-2 absorb adapter — local structured stores (ADR-087 D8;
harness-memory survey §2).

Structured-text/SQLite readers for products whose auto-memory lives in
app-owned local stores:

- **Codex** per-app SQLite — live-verified schema (2026-07-13):
  ``~/.codex/memories_1.sqlite`` → ``stage1_outputs(thread_id,
  raw_memory, rollout_summary, usage_count, …)``;
  ``~/.codex/goals_1.sqlite`` → ``thread_goals(thread_id, objective,
  status, created_at_ms, updated_at_ms)``. A ``_sqlx_migrations`` table
  rides along — the observed proof that these schemas churn.
- **Goose** category ``.txt`` files: blank-line-separated entries,
  ``#``-prefixed tag headers (survey format; not installed locally).
- **Hermes** ``~/.hermes/memories/MEMORY.md`` + ``USER.md``,
  ``§``-delimited entries (survey format; not installed locally).

Cluster-2 law (D8): these stores are **app-owned** — open read-only,
parse defensively, never depend on, never write. Schema drift degrades
to a skip record (audited on import), never a crash and never a partial
write of the batch.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from axiom.memory.fragment import SourceOrigin

from .base import AbsorbScan, FragmentCandidate, SkippedSource


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Read-only SQLite handle (URI mode) — the no-write guarantee.

    ``immutable=1`` is retried when plain read-only fails (e.g. a WAL
    store whose sidecar files we must not touch).
    """
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def _ms_to_iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _s_to_iso(seconds: Any) -> str:
    try:
        return datetime.fromtimestamp(int(seconds), UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


@dataclass
class StructuredStoreAdapter:
    """Generic engine: a list of defensive readers → one scan.

    Each reader is ``(scan) -> None`` and is individually fenced: any
    :class:`Exception` it raises becomes a skip record for its source,
    and the remaining readers still run — drift in one table/file never
    takes down the batch.
    """

    harness: str
    account: str
    readers: list[tuple[str, Callable[[AbsorbScan], None]]] = field(
        default_factory=list
    )
    expected_locations: list[Path] = field(default_factory=list)

    def scan(self) -> AbsorbScan:
        scan = AbsorbScan()
        for source, reader in self.readers:
            try:
                reader(scan)
            except Exception as exc:  # defensive fence — the point of D8
                scan.skipped.append(
                    SkippedSource(source=source, reason=f"unreadable: {exc}")
                )
        if not scan.candidates and not any(
            loc.exists() for loc in self.expected_locations
        ):
            for loc in self.expected_locations:
                scan.skipped.append(
                    SkippedSource(source=str(loc), reason="missing")
                )
        return scan

    def origin(self, source_ref: str) -> SourceOrigin:
        return SourceOrigin(
            harness=self.harness,
            account=self.account,
            source_ref=source_ref,
            imported_at=_now(),
        )


def _read_sqlite_table(
    adapter: StructuredStoreAdapter,
    scan: AbsorbScan,
    *,
    db_path: Path,
    table: str,
    columns: list[str],
    to_candidate: Callable[[dict], FragmentCandidate | None],
) -> None:
    """Read one app-owned table defensively into candidates.

    Missing file → nothing (the store-level ``missing`` record is the
    scan's job). Missing table / column drift / corrupt file → one skip
    record naming the source, no crash, no partial write.
    """
    if not db_path.is_file():
        return
    source = f"{db_path}/{table}"
    try:
        con = _connect_readonly(db_path)
    except sqlite3.Error as exc:
        scan.skipped.append(
            SkippedSource(source=str(db_path), reason=f"unreadable: {exc}")
        )
        return
    try:
        try:
            rows = con.execute(
                f"SELECT {', '.join(columns)} FROM {table}"
            ).fetchall()
        except sqlite3.Error as exc:
            scan.skipped.append(
                SkippedSource(source=source, reason=f"schema_drift: {exc}")
            )
            return
        for row in rows:
            record = dict(zip(columns, row))
            try:
                cand = to_candidate(record)
            except (KeyError, TypeError, ValueError) as exc:
                scan.skipped.append(
                    SkippedSource(source=source, reason=f"row_invalid: {exc}")
                )
                continue
            if cand is not None:
                scan.candidates.append(cand)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def codex_adapter(
    *, account: str, home: Path | None = None
) -> StructuredStoreAdapter:
    """Codex local memories: staged memory rows + thread goals."""
    base = Path(home) if home is not None else Path.home()
    codex = base / ".codex"
    memories_db = codex / "memories_1.sqlite"
    goals_db = codex / "goals_1.sqlite"

    adapter = StructuredStoreAdapter(
        harness="codex", account=account, expected_locations=[codex],
    )

    def _memory_candidate(rec: dict) -> FragmentCandidate | None:
        text = (rec.get("raw_memory") or "").strip()
        if not text:
            return None
        summary = (rec.get("rollout_summary") or "").strip() or text
        content = {
            "summary": summary,
            "text": text,
            "thread_id": rec.get("thread_id", ""),
            "layer": "auto_memory",
            "fact_kind": "codex_memory",
        }
        if rec.get("usage_count") is not None:
            content["usage_count"] = rec["usage_count"]
        updated = _s_to_iso(rec.get("source_updated_at"))
        if updated:
            content["source_updated_at"] = updated
        return FragmentCandidate(
            content=content,
            cognitive_type="semantic",
            origin=adapter.origin(
                f"{memories_db}/stage1_outputs/{rec.get('thread_id', '')}"
            ),
        )

    def _goal_candidate(rec: dict) -> FragmentCandidate | None:
        objective = (rec.get("objective") or "").strip()
        if not objective:
            return None
        event_time = _ms_to_iso(rec.get("updated_at_ms")) or _ms_to_iso(
            rec.get("created_at_ms")
        )
        if not event_time:
            raise ValueError("thread_goal row without a usable timestamp")
        return FragmentCandidate(
            content={
                "summary": objective,
                "text": objective,
                "status": rec.get("status", ""),
                "event_time": event_time,
                "thread_id": rec.get("thread_id", ""),
                "layer": "auto_memory",
                "fact_kind": "thread_goal",
            },
            cognitive_type="episodic",
            origin=adapter.origin(
                f"{goals_db}/thread_goals/{rec.get('thread_id', '')}"
            ),
        )

    adapter.readers = [
        (
            f"{memories_db}/stage1_outputs",
            lambda scan: _read_sqlite_table(
                adapter, scan,
                db_path=memories_db,
                table="stage1_outputs",
                columns=[
                    "thread_id", "raw_memory", "rollout_summary",
                    "usage_count", "source_updated_at",
                ],
                to_candidate=_memory_candidate,
            ),
        ),
        (
            f"{goals_db}/thread_goals",
            lambda scan: _read_sqlite_table(
                adapter, scan,
                db_path=goals_db,
                table="thread_goals",
                columns=[
                    "thread_id", "goal_id", "objective", "status",
                    "created_at_ms", "updated_at_ms",
                ],
                to_candidate=_goal_candidate,
            ),
        ),
    ]
    return adapter


# ---------------------------------------------------------------------------
# Goose
# ---------------------------------------------------------------------------


def goose_adapter(
    *, account: str, base: Path | None = None
) -> StructuredStoreAdapter:
    """Goose category files: blank-line entries, ``#`` tag headers."""
    root = (
        Path(base)
        if base is not None
        else Path.home() / ".config" / "goose" / "memory"
    )
    adapter = StructuredStoreAdapter(
        harness="goose", account=account, expected_locations=[root],
    )

    def _read_categories(scan: AbsorbScan) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.txt")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                scan.skipped.append(
                    SkippedSource(source=str(path), reason=f"unreadable: {exc}")
                )
                continue
            category = path.stem
            for block in text.split("\n\n"):
                tags: list[str] = []
                body_lines: list[str] = []
                for line in block.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("#") and not body_lines:
                        tags.extend(stripped.lstrip("#").split())
                    else:
                        body_lines.append(stripped)
                body = "\n".join(body_lines).strip()
                if not body:
                    continue
                scan.candidates.append(FragmentCandidate(
                    content={
                        "summary": body_lines[0],
                        "text": body,
                        "category": category,
                        "tags": tags,
                        "layer": "auto_memory",
                        "fact_kind": "goose_memory",
                    },
                    cognitive_type="semantic",
                    origin=adapter.origin(f"{path}#{_sha16(body)}"),
                ))

    adapter.readers = [(str(root), _read_categories)]
    return adapter


# ---------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------


def hermes_adapter(
    *, account: str, home: Path | None = None
) -> StructuredStoreAdapter:
    """Hermes agent: ``MEMORY.md`` + ``USER.md``, §-delimited entries."""
    base = Path(home) if home is not None else Path.home()
    memories = base / ".hermes" / "memories"
    adapter = StructuredStoreAdapter(
        harness="hermes", account=account, expected_locations=[memories],
    )

    def _read_file(path: Path, scan: AbsorbScan) -> None:
        if not path.is_file():
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            scan.skipped.append(
                SkippedSource(source=str(path), reason=f"unreadable: {exc}")
            )
            return
        header: str | None = None
        body_lines: list[str] = []

        def _flush() -> None:
            body = "\n".join(body_lines).strip()
            if header is None and not body:
                return
            summary = (header or "").strip() or body.splitlines()[0]
            entry = f"{header or ''}\n{body}".strip()
            scan.candidates.append(FragmentCandidate(
                content={
                    "summary": summary,
                    "text": body or summary,
                    "path": str(path),
                    "layer": "auto_memory",
                    "fact_kind": "hermes_memory",
                },
                cognitive_type="semantic",
                origin=adapter.origin(f"{path}#{_sha16(entry)}"),
            ))

        for line in text.splitlines():
            if line.startswith("§"):
                if header is not None or body_lines:
                    _flush()
                header = line.lstrip("§").strip()
                body_lines = []
            else:
                body_lines.append(line)
        if header is not None or any(line.strip() for line in body_lines):
            _flush()

    adapter.readers = [
        (
            str(memories / name),
            (lambda p: (lambda scan: _read_file(p, scan)))(memories / name),
        )
        for name in ("MEMORY.md", "USER.md")
    ]
    return adapter


__all__ = [
    "StructuredStoreAdapter",
    "codex_adapter",
    "goose_adapter",
    "hermes_adapter",
]
