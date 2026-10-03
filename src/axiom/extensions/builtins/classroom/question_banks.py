# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Question banks + rail customization — FW-1 P3b.

Question banks are collections of student-onboarding questions. Rails
are named sequences of questions that auto-apply to new students.
This module provides:

- ``QuestionBank`` — a registered collection of questions
- ``CORE_STARTER_BANK`` — the built-in, domain-agnostic starter bank
  (consent, prior experience, comfort with AI tooling)
- ``list_banks()`` / ``register_bank()`` / ``unregister_bank()`` —
  the runtime registry domain extensions plug into
- ``add_rail_from_bank()`` — create a new rail on a course manifest
  seeded from a registered bank (optionally limited to a subset of
  question ids)
- ``preview_rail()`` — produce a deterministic stub-student preview
  of what a student would see when the rail auto-applies (uses the
  ``@alice:demo`` persona per the P3b design decision)

Axiom core ships with one bank. Domain extensions
will ``register_bank(...)`` additional banks at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class QuestionBank:
    """A collection of onboarding questions contributed by a source."""

    id: str
    description: str
    questions: list[dict[str, Any]] = field(default_factory=list)
    source: str = "axiom-core"

    def question_ids(self) -> list[str]:
        return [q.get("id", "") for q in self.questions]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "source": self.source,
            "question_ids": self.question_ids(),
            "question_count": len(self.questions),
        }


# ---------------------------------------------------------------------------
# Built-in core bank
# ---------------------------------------------------------------------------


CORE_STARTER_BANK = QuestionBank(
    id="axiom-core-starter",
    description=(
        "Generic onboarding questions shipped with Axiom core — AI-tooling "
        "consent, prior experience, preferred learning cadence. Use as a "
        "starting point and augment with a domain extension's bank for "
        "subject-specific questions."
    ),
    source="axiom-core",
    questions=[
        {
            "id": "ai-consent",
            "prompt": (
                "This course uses an AI teaching assistant. Your conversations "
                "will be stored and used to evaluate learning outcomes. Do you "
                "consent to this data use?"
            ),
            "response_type": "yes_no",
            "required": True,
        },
        {
            "id": "prior-ai-tooling",
            "prompt": (
                "How often have you used AI tools (ChatGPT, Claude, Copilot) "
                "for coursework before?"
            ),
            "response_type": "multiple_choice",
            "choices": ["Never", "Once or twice", "Occasionally", "Regularly"],
        },
        {
            "id": "confidence-subject",
            "prompt": (
                "On a 1–5 scale, how confident do you feel about the core "
                "material in this course?"
            ),
            "response_type": "likert",
        },
        {
            "id": "preferred-feedback",
            "prompt": (
                "When you make a mistake, what kind of feedback helps you learn best?"
            ),
            "response_type": "free_text",
        },
    ],
)


# ---------------------------------------------------------------------------
# Runtime registry
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, QuestionBank] = {}


def _ensure_core_registered() -> None:
    """Register the core bank if not already present (idempotent)."""
    if CORE_STARTER_BANK.id not in _REGISTRY:
        _REGISTRY[CORE_STARTER_BANK.id] = CORE_STARTER_BANK


# Register core at import time — users get the built-in bank automatically.
_ensure_core_registered()


def register_bank(bank: QuestionBank) -> None:
    """Register a question bank. Raises ValueError on duplicate id."""
    if bank.id in _REGISTRY:
        raise ValueError(f"bank {bank.id!r} already registered")
    _REGISTRY[bank.id] = bank


def unregister_bank(bank_id: str) -> None:
    """Remove a bank (no-op if absent). Intended primarily for test teardown."""
    _REGISTRY.pop(bank_id, None)


def list_banks() -> list[QuestionBank]:
    """Return all registered banks, with core guaranteed present."""
    _ensure_core_registered()
    return list(_REGISTRY.values())


def get_bank(bank_id: str) -> QuestionBank:
    """Return a bank by id. Raises ValueError if unknown."""
    _ensure_core_registered()
    if bank_id not in _REGISTRY:
        known = sorted(_REGISTRY.keys())
        raise ValueError(
            f"unknown bank {bank_id!r}; registered: {known}"
        )
    return _REGISTRY[bank_id]


# ---------------------------------------------------------------------------
# Rail manipulation on course manifests
# ---------------------------------------------------------------------------


def _find_rail_index(manifest: dict[str, Any], rail_id: str) -> int | None:
    for i, r in enumerate(manifest.get("rails") or []):
        if r.get("id") == rail_id:
            return i
    return None


def add_rail_from_bank(
    manifest: dict[str, Any],
    *,
    rail_id: str,
    bank_id: str,
    question_ids: list[str] | None = None,
    auto_apply_to: str = "all_new_students",
    required: bool = True,
) -> dict[str, Any]:
    """Create (or replace) a rail on ``manifest`` seeded from a bank.

    Args:
        manifest: The course manifest dict to mutate.
        rail_id: Identifier for the new rail.
        bank_id: Bank to pull questions from.
        question_ids: Optional subset — if provided, only these question
            ids are included (in the given order). Raises if any id is
            unknown in the bank. If None, all bank questions are used
            in bank order.
        auto_apply_to: Rail activation policy (default: all new students).
        required: Whether student must complete the rail.

    Returns the newly-added rail dict. Mutates ``manifest`` in place.
    """
    bank = get_bank(bank_id)
    if question_ids is not None:
        bank_by_id = {q.get("id"): q for q in bank.questions}
        missing = [q for q in question_ids if q not in bank_by_id]
        if missing:
            raise ValueError(
                f"question id(s) not found in bank {bank.id!r}: {missing}"
            )
        selected = [bank_by_id[q] for q in question_ids]
    else:
        selected = list(bank.questions)

    rail = {
        "id": rail_id,
        "source": bank.id,
        "auto_apply_to": auto_apply_to,
        "required": required,
        "questions": [dict(q) for q in selected],
    }

    existing_idx = _find_rail_index(manifest, rail_id)
    rails = manifest.setdefault("rails", [])
    if existing_idx is not None:
        rails[existing_idx] = rail
    else:
        rails.append(rail)
    return rail


def remove_rail(manifest: dict[str, Any], rail_id: str) -> bool:
    """Remove a rail by id; returns True if removed."""
    rails = manifest.get("rails")
    if not rails:
        return False
    kept = [r for r in rails if r.get("id") != rail_id]
    if len(kept) == len(rails):
        return False
    manifest["rails"] = kept
    return True


# ---------------------------------------------------------------------------
# Stub-student preview
# ---------------------------------------------------------------------------


STUB_STUDENT_PERSONA = "@alice:demo"


_STUB_RESPONSES = {
    "yes_no": "yes",
    "likert": "3",
    "multiple_choice": "Occasionally",
    "free_text": "I like clear explanations with worked examples.",
}


def _stub_response_for(question: dict[str, Any]) -> str:
    rtype = question.get("response_type", "free_text")
    if rtype == "multiple_choice":
        choices = question.get("choices") or []
        if choices:
            mid = choices[len(choices) // 2]
            return mid
    return _STUB_RESPONSES.get(rtype, "(sample response)")


def preview_rail(
    manifest: dict[str, Any], *, rail_id: str,
) -> dict[str, Any]:
    """Produce a deterministic preview of what a student sees on this rail.

    Uses the ``@alice:demo`` stub persona. Each turn includes the
    prompt, a sample response, and the response type so the instructor
    can gauge whether the wording feels right *without* needing a real
    enrolled student.

    Raises ValueError if ``rail_id`` isn't configured on the manifest.
    """
    rails = manifest.get("rails") or []
    idx = _find_rail_index(manifest, rail_id)
    if idx is None:
        known = [r.get("id") for r in rails]
        raise ValueError(
            f"rail {rail_id!r} not configured; known: {known}"
        )
    rail = rails[idx]

    turns = []
    for q in rail.get("questions", []):
        turns.append(
            {
                "question_id": q.get("id"),
                "prompt": q.get("prompt"),
                "response_type": q.get("response_type"),
                "sample_response": _stub_response_for(q),
            }
        )

    return {
        "rail_id": rail_id,
        "student_persona": STUB_STUDENT_PERSONA,
        "turns": turns,
    }
