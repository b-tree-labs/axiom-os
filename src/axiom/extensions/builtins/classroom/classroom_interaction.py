# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Coordinator-side classroom interaction log.

Tier B piece. Every student question submitted via `axi classroom
ask` is (best-effort) reported back to the coordinator, which logs
it in an append-only JSONL. CHALKE's daily brief (Ondrej's morning
"who's stuck?" surface at Prague) reads these records to derive:

- Question volume this week
- Top topics / concept clusters
- Quiet students (zero questions) vs noisy students (many questions)
- Misconception candidates (questions with no answer)

The log is intentionally plain JSONL — one record per line — so an
instructor can ``cat`` / ``jq`` the file directly when debugging,
and the format is forward-compatible: new fields can be added to
``InteractionRecord`` without breaking old records.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InteractionRecord:
    student_id: str
    question: str
    had_answer: bool
    citations_count: int
    timestamp: str  # ISO 8601 with tz
    # Optional fields — versioning knobs. Always set defaults so older
    # log entries without these fields still decode cleanly.
    classroom_id: str | None = None
    mode: str | None = None  # learning mode used for the ask

    @property
    def interaction_id(self) -> str:
        """Stable, deterministic ID for this record.

        Hash-based so existing records on disk get the same ID every
        time without needing migration. Twelve hex chars is plenty of
        entropy for a single classroom (at v0 scale, ~10^4 records).
        """
        return _compute_interaction_id(
            student_id=self.student_id,
            timestamp=self.timestamp,
            question=self.question,
        )


def _compute_interaction_id(
    *, student_id: str, timestamp: str, question: str,
) -> str:
    raw = f"{student_id}|{timestamp}|{question}".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class ClassroomInteractionStore:
    base_dir: Path
    # ADR-033 Stage 1: optional dual-write into the canonical L1 memory
    # layer. When provided, every appended interaction is also written
    # as a MemoryFragment via the supplied callable. Production wires
    # this through CompositionService.write; tests inject a list-append
    # spy. Default None preserves existing single-write behaviour.
    memory_writer: Callable[[InteractionRecord, str], None] | None = None
    # Scope id passed to the memory writer so the L1 fragment carries
    # the cohort/classroom origin. Defaults to the directory name when
    # not set; production setups override with the actual classroom_id.
    scope_id: str | None = None

    @property
    def _path(self) -> Path:
        return self.base_dir / "interactions.jsonl"

    def append(self, record: InteractionRecord) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(json.dumps(asdict(record)) + "\n")
        # Dual-write to L1 — best effort; never fails the JSONL primary
        # write. Stage 2+ promotes this to a hard contract once L1 is
        # the source of truth.
        if self.memory_writer is not None:
            scope = self.scope_id or self.base_dir.name
            try:
                self.memory_writer(record, scope)
            except Exception:
                # Intentionally swallowed during migration. The JSONL
                # remains authoritative; the L1 mirror is nice-to-have
                # until Stage 4. Logging hook is a follow-up.
                pass

    def list(self) -> list[InteractionRecord]:
        records, _ = self._read_raw()
        forgotten_ids = self._forgotten_ids()
        return [
            r for r in records if r.interaction_id not in forgotten_ids
        ]

    def _read_raw(self) -> tuple[list[InteractionRecord], list[dict]]:
        """Return (records, tombstones) without any filtering applied.

        Tombstones are stored as separate JSONL records with
        ``{"_tombstone": true, "student_id": ..., "interaction_id": ...,
        "tombstoned_at": ...}``. The instructor can grep them out of the
        raw file if they want — e.g., to count retractions.
        """
        if not self._path.is_file():
            return [], []
        records: list[InteractionRecord] = []
        tombstones: list[dict] = []
        for raw in self._path.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                # Corrupt line — skip rather than fail the whole read.
                continue
            if isinstance(obj, dict) and obj.get("_tombstone") is True:
                tombstones.append(obj)
                continue
            records.append(InteractionRecord(
                student_id=str(obj.get("student_id", "")),
                question=str(obj.get("question", "")),
                had_answer=bool(obj.get("had_answer", False)),
                citations_count=int(obj.get("citations_count", 0)),
                timestamp=str(obj.get("timestamp", "")),
                classroom_id=obj.get("classroom_id"),
                mode=obj.get("mode"),
            ))
        return records, tombstones

    def _forgotten_ids(self) -> set[str]:
        _, tombstones = self._read_raw()
        return {str(t.get("interaction_id", "")) for t in tombstones}

    def forget(self, *, student_id: str, interaction_id: str) -> dict:
        """Tombstone a single interaction record so it stops surfacing.

        Append-only JSONL means we don't physically delete — we write a
        tombstone line that ``list()`` filters out on read. The
        instructor sees a "forgotten count" via ``forgotten_count``
        without seeing the retracted content.

        Idempotent: tombstoning an already-tombstoned record is a no-op
        success. Returns ``{"forgotten": bool, "interaction_id": str,
        "error"?: str}``.
        """
        records, _ = self._read_raw()
        target = next(
            (
                r for r in records
                if r.interaction_id == interaction_id
                and r.student_id == student_id
            ),
            None,
        )
        if target is None:
            return {
                "forgotten": False,
                "interaction_id": interaction_id,
                "error": "no matching interaction for that student + id",
            }

        # Already tombstoned — return idempotent without writing a duplicate.
        if interaction_id in self._forgotten_ids():
            return {
                "forgotten": True,
                "interaction_id": interaction_id,
                "idempotent": True,
            }

        tombstone = {
            "_tombstone": True,
            "student_id": student_id,
            "interaction_id": interaction_id,
            "tombstoned_at": datetime.now(UTC).isoformat(),
        }
        self.base_dir.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(json.dumps(tombstone) + "\n")
        return {"forgotten": True, "interaction_id": interaction_id}

    def forgotten_count(self, student_id: str | None = None) -> int:
        """Count of tombstoned records, optionally scoped to one student."""
        _, tombstones = self._read_raw()
        if student_id is None:
            return len(tombstones)
        return sum(
            1 for t in tombstones if t.get("student_id") == student_id
        )

    def by_student(self, student_id: str) -> list[InteractionRecord]:
        return [r for r in self.list() if r.student_id == student_id]

    def distinct_students(self) -> int:
        return len({r.student_id for r in self.list() if r.student_id})

    def quiet_students(self, *, roster: list[str]) -> list[str]:
        seen = {r.student_id for r in self.list()}
        return [s for s in roster if s not in seen]

    def summary_for_student(
        self,
        student_id: str,
        *,
        recent_n: int = 5,
        topics_n: int = 5,
    ) -> dict:
        """Build a memory-transparency view of what's logged for a student.

        Returned shape is the wire format the student-side CLI fetches via
        ``GET /classroom/memory/{student_id}`` and renders. Keep it
        flat + JSON-friendly so older clients can still parse if fields
        are added later.

        The point is to make the coordinator's memory of the student
        legible to the student themselves: *what is on file, in their
        own words*. This is the primary mitigation for "wrong things
        ended up in my classroom memory" anxiety.
        """
        records = self.by_student(student_id)
        modes = Counter((r.mode or "ask") for r in records)
        return {
            "student_id": student_id,
            "question_count": len(records),
            "answered_count": sum(1 for r in records if r.had_answer),
            "unanswered_count": sum(1 for r in records if not r.had_answer),
            "forgotten_count": self.forgotten_count(student_id),
            "modes_used": dict(modes),
            "topics": topic_histogram(records, top_n=topics_n),
            "recent_questions": [
                {
                    "interaction_id": r.interaction_id,
                    "question": r.question,
                    "mode": r.mode or "ask",
                    "timestamp": r.timestamp,
                    "had_answer": r.had_answer,
                }
                for r in sorted(
                    records, key=lambda x: x.timestamp, reverse=True,
                )[:recent_n]
            ],
        }


# ---------------------------------------------------------------------------
# Topic histogram — crude keyword cluster so "hot topics" surface
# ---------------------------------------------------------------------------


# Words we ignore when clustering question topics.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "is", "are", "was", "were",
    "what", "who", "where", "why", "how", "when", "this", "that", "these",
    "those", "does", "do", "did", "in", "on", "for", "with", "by", "from",
    "as", "at", "be", "can", "it", "its", "if", "so", "such", "not", "no",
    "tell", "me", "my", "your", "you", "we", "us", "about",
}


def topic_histogram(
    records: list[InteractionRecord],
    *,
    top_n: int = 5,
) -> list[tuple[str, int]]:
    """Count non-stopword tokens across all question texts.

    Returns ``[(token, count), ...]`` sorted by count descending,
    truncated to ``top_n``. Deliberately naive — a follow-up can swap
    in the embedding-based clustering that `axiom.rag` already uses.
    """
    counter: Counter[str] = Counter()
    for r in records:
        for tok in _tokenize(r.question):
            if tok and tok not in _STOPWORDS and len(tok) >= 3:
                counter[tok] += 1
    return counter.most_common(top_n)


_WORD = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


__all__ = [
    "ClassroomInteractionStore",
    "InteractionRecord",
    "topic_histogram",
]
