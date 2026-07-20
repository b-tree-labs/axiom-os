# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-student brief engine + store.

Tier C2 — each student gets a per-period narrative summary of their
own learning, drawn from the coordinator's interaction log (shipped
in Tier B). Instructor curates (adds a note, approves) before
release; students on demand see their latest approved brief.

Design principles (Master-Educator-reviewed):

- The brief is **metacognitive**, not evaluative. It reflects back
  what the student engaged with, what they skipped, what they asked
  and what went unanswered — it does NOT grade or rank.
- Deterministic sections ALWAYS ship (counts, mode mix, unanswered
  list). The LLM narrative is an enhancement, not a gate. A brief
  must be producible offline.
- Instructor curation is mandatory by default. Students see the
  instructor-approved version, not a raw LLM dump. This is the
  "report card" review loop.

Storage layout under ``<base_dir>``::

    <base_dir>/briefs/<student_id>/<generated_at_iso>.json

Each file is the full ``StudentBrief`` serialized. The per-student
subdirectory keeps listing cheap and makes "delete this student's
briefs" trivial.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .classroom_interaction import InteractionRecord

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudentBrief:
    student_id: str
    classroom_id: str
    period_start: str
    period_end: str
    generated_at: str
    sections: dict
    review_status: str = "draft"  # "draft" | "approved" | "released"
    instructor_note: str = ""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


LLMFn = Callable[..., str | None]


_SYSTEM_PROMPT = (
    "You are a supportive, honest tutor writing a short reflection "
    "for a student about their own learning this week. Rules:\n"
    "1. Be specific — reference what they actually asked.\n"
    "2. Note what's unvisited, gently, as a prompt (not a criticism).\n"
    "3. Never evaluate or grade. Never predict outcomes.\n"
    "4. 3-5 sentences. No bullets. Warm but not syrupy.\n"
)


def _build_user_prompt(
    student_id: str,
    records: list[InteractionRecord],
    mode_counts: dict[str, int],
    unanswered: list[str],
) -> str:
    lines = [
        f"Student: {student_id}",
        f"Questions asked this period: {len(records)}",
        f"Mode usage: {dict(mode_counts)}",
    ]
    if unanswered:
        lines.append(f"Questions with no match in class materials ({len(unanswered)}):")
        for q in unanswered[:5]:
            lines.append(f"  - {q}")
    if records:
        lines.append("Sampled questions:")
        for r in records[:8]:
            lines.append(f"  - {r.question}")
    return "\n".join(lines)


def generate_brief(
    *,
    student_id: str,
    classroom_id: str,
    interactions: list[InteractionRecord],
    llm: LLMFn | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> StudentBrief:
    """Produce a brief for ``student_id`` from the given records.

    The deterministic sections always ship; the LLM narrative is
    added to ``sections['narrative']`` when a provider is available
    and the call succeeds.
    """
    my_records = [r for r in interactions if r.student_id == student_id]
    now = datetime.now(UTC).isoformat()

    mode_counts: Counter[str] = Counter()
    for r in my_records:
        mode_counts[r.mode or "ask"] += 1
    unanswered = [r.question for r in my_records if not r.had_answer]

    # Deterministic sections — always present.
    sections: dict = {
        "activity_summary": _activity_summary(my_records, mode_counts),
        "mode_mix": dict(mode_counts),
        "unanswered_questions": unanswered,
        "suggested_next_prompts": _suggested_next_prompts(my_records),
    }

    # LLM narrative — best-effort enrichment.
    if llm is not None and my_records:
        user_prompt = _build_user_prompt(
            student_id, my_records, mode_counts, unanswered,
        )
        try:
            narrative = llm(user_prompt, system=_SYSTEM_PROMPT)
        except Exception:
            narrative = None
        if narrative:
            sections["narrative"] = narrative.strip()

    return StudentBrief(
        student_id=student_id,
        classroom_id=classroom_id,
        period_start=period_start or "",
        period_end=period_end or now,
        generated_at=now,
        sections=sections,
        review_status="draft",
        instructor_note="",
    )


def _activity_summary(
    records: list[InteractionRecord],
    mode_counts: Counter,
) -> str:
    if not records:
        return (
            "No questions asked yet this period. "
            "Try starting with `axi classroom ask` on a topic you want to explore."
        )
    n = len(records)
    q_word = "question" if n == 1 else "questions"
    unanswered = sum(1 for r in records if not r.had_answer)
    modes_used = ", ".join(sorted(mode_counts)) or "ask"
    parts = [
        f"Asked {n} {q_word} this period across {modes_used} mode(s)."
    ]
    if unanswered:
        parts.append(
            f"{unanswered} went unanswered — those topics aren't "
            "covered in the class materials yet."
        )
    return " ".join(parts)


def _suggested_next_prompts(records: list[InteractionRecord]) -> list[str]:
    """Pick a few prompts the student should try next.

    Heuristic for now: surface their own unanswered questions as a
    "revisit after more materials land" hint, plus a generic prompt
    if they haven't used tutor mode yet.
    """
    out: list[str] = []
    for r in records:
        if not r.had_answer and r.question not in out:
            out.append(r.question)
        if len(out) >= 3:
            break
    if not any((r.mode or "") == "tutor" for r in records):
        out.append(
            "Try tutor mode (`ask --mode tutor`) for a topic you want "
            "to think through, not just look up."
        )
    return out[:5]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class BriefStore:
    base_dir: Path

    # ---- Paths ----

    def _student_dir(self, student_id: str) -> Path:
        safe = student_id.replace("/", "_").replace("\\", "_")
        return self.base_dir / "briefs" / safe

    def _brief_path(self, student_id: str, generated_at: str) -> Path:
        safe_ts = generated_at.replace(":", "_").replace("/", "_")
        return self._student_dir(student_id) / f"{safe_ts}.json"

    # ---- Public API ----

    def save(self, brief: StudentBrief) -> None:
        path = self._brief_path(brief.student_id, brief.generated_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(brief), indent=2))

    def latest_for_student(self, student_id: str) -> StudentBrief | None:
        briefs = self.list_for_student(student_id)
        return briefs[0] if briefs else None

    def latest_approved_for_student(
        self, student_id: str,
    ) -> StudentBrief | None:
        for b in self.list_for_student(student_id):
            if b.review_status in {"approved", "released"}:
                return b
        return None

    def list_for_student(self, student_id: str) -> list[StudentBrief]:
        d = self._student_dir(student_id)
        if not d.is_dir():
            return []
        briefs: list[StudentBrief] = []
        for path in d.glob("*.json"):
            try:
                raw = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            briefs.append(_brief_from_dict(raw))
        # Newest first.
        briefs.sort(key=lambda b: b.generated_at, reverse=True)
        return briefs

    def list_student_ids(self) -> list[str]:
        root = self.base_dir / "briefs"
        if not root.is_dir():
            return []
        return sorted(
            child.name for child in root.iterdir()
            if child.is_dir()
        )

    def approve(
        self,
        student_id: str,
        generated_at: str,
        *,
        note: str = "",
    ) -> None:
        path = self._brief_path(student_id, generated_at)
        if not path.is_file():
            raise KeyError(
                f"no brief for {student_id!r} at {generated_at!r}"
            )
        raw = json.loads(path.read_text())
        current = _brief_from_dict(raw)
        updated = replace(
            current,
            review_status="approved",
            instructor_note=note or current.instructor_note,
        )
        path.write_text(json.dumps(asdict(updated), indent=2))


def _brief_from_dict(raw: dict) -> StudentBrief:
    return StudentBrief(
        student_id=str(raw["student_id"]),
        classroom_id=str(raw["classroom_id"]),
        period_start=str(raw.get("period_start", "")),
        period_end=str(raw.get("period_end", "")),
        generated_at=str(raw["generated_at"]),
        sections=dict(raw.get("sections") or {}),
        review_status=str(raw.get("review_status", "draft")),
        instructor_note=str(raw.get("instructor_note", "")),
    )


__all__ = [
    "BriefStore",
    "LLMFn",
    "StudentBrief",
    "generate_brief",
]
