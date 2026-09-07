# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Learning-mode registry + classroom permission policy.

Phase C1 of the Master-Educator-reviewed design: modes are
first-class policy objects, not ad-hoc flag combinations. Each
``LearningMode`` bundles:

- ``llm_constraint``  what the LLM is allowed to do
      "unrestricted" — normal synthesis
      "socratic"     — guide by questions only, never answer directly
      "summary-only" — summarize passages, no elaboration
      "none"         — LLM not invoked at all (closed-book)
- ``retrieval_policy`` what retrieval is allowed
      "full" — normal hybrid vector + FTS
      "peek" — retrieval fires but results don't go in the response
      "none" — no retrieval at all (quiz mode)
- ``student_writes_first`` — student produces text before any system
  response (reflection / quiz / journal)
- ``system_prompt_overlay`` — appended to the base prompt when the
  mode's LLM constraint allows a call
- ``description`` — student-facing one-liner

The classroom policy layer adds instructor control: which modes are
available to students + optional forced override. Effective mode is
computed as ``forced → student_pref bounded by allowed → fallback``.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearningMode:
    name: str
    description: str
    llm_constraint: str  # "unrestricted" | "socratic" | "summary-only" | "none"
    retrieval_policy: str  # "full" | "peek" | "none"
    student_writes_first: bool = False
    system_prompt_overlay: str = ""


# ---------------------------------------------------------------------------
# Registry — keep the surface tiny for now; more modes land as we
# learn what teachers actually reach for. Order here is also the
# "safe-fallback priority" when the policy disallows every preferred
# pick.
# ---------------------------------------------------------------------------


_SOCRATIC_OVERLAY = (
    "You are a Socratic tutor. Under no circumstances answer the "
    "student's question directly. Instead, respond with ONE short "
    "guiding question or observation that helps the student think "
    "through the problem themselves. Point at the relevant passage "
    "by title, but do not quote it. If the student is clearly stuck, "
    "offer a small hint framed as a question. Never give the final "
    "answer — productive struggle is the goal."
)

_SUMMARY_OVERLAY = (
    "Summarize the provided passages in 2-3 sentences. Do not "
    "elaborate beyond the passages. Cite by title."
)


_REGISTRY: dict[str, LearningMode] = {
    "ask": LearningMode(
        name="ask",
        description="Ask a question, get a grounded answer with citations.",
        llm_constraint="unrestricted",
        retrieval_policy="full",
    ),
    "tutor": LearningMode(
        name="tutor",
        description=(
            "Socratic tutor — the system refuses to answer directly, "
            "asks guiding questions so you think it through."
        ),
        llm_constraint="socratic",
        retrieval_policy="peek",
        system_prompt_overlay=_SOCRATIC_OVERLAY,
    ),
    "quiz": LearningMode(
        name="quiz",
        description=(
            "Closed-book retrieval practice — you answer from memory, "
            "no materials, no LLM. Scored after."
        ),
        llm_constraint="none",
        retrieval_policy="none",
        student_writes_first=True,
    ),
    "reflect": LearningMode(
        name="reflect",
        description=(
            "Metacognition journaling — you write what clicked, what's "
            "fuzzy. No retrieval, no LLM."
        ),
        llm_constraint="none",
        retrieval_policy="none",
        student_writes_first=True,
    ),
    "review": LearningMode(
        name="review",
        description=(
            "Concept overview — summary of the passages, then a self-"
            "check prompt."
        ),
        llm_constraint="summary-only",
        retrieval_policy="full",
        system_prompt_overlay=_SUMMARY_OVERLAY,
    ),
}


# Tuple of mode names in fallback order — earlier = safer default.
_FALLBACK_ORDER = ("ask", "review", "reflect", "tutor", "quiz")


MODE_REGISTRY = _REGISTRY  # publicly visible


def list_modes() -> list[LearningMode]:
    return list(_REGISTRY.values())


def get_mode(name: str) -> LearningMode:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown learning mode: {name!r}") from exc


# ---------------------------------------------------------------------------
# Classroom policy — instructor envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassroomModePolicy:
    """Instructor-owned policy governing which modes students may use.

    ``allowed_modes`` is the envelope students choose within.
    ``forced_mode``, when set, overrides student choice entirely — the
    "it's quiz week" knob.
    """

    allowed_modes: frozenset[str]
    forced_mode: str | None

    @classmethod
    def default(cls) -> ClassroomModePolicy:
        return cls(
            allowed_modes=frozenset(_REGISTRY),
            forced_mode=None,
        )

    def is_allowed(self, mode_name: str) -> bool:
        return mode_name in self.allowed_modes

    # ---- (de)serialization ----

    def to_dict(self) -> dict:
        return {
            "allowed_modes": sorted(self.allowed_modes),
            "forced_mode": self.forced_mode,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> ClassroomModePolicy:
        """Tolerant constructor — missing fields default to the most
        permissive setting (all modes allowed, no forced override).

        This lets older cohort files without a mode policy load
        cleanly; instructors opt in to restrictions by explicitly
        setting them."""
        allowed = raw.get("allowed_modes")
        if allowed is None:
            allowed = set(_REGISTRY)
        else:
            allowed = set(allowed) & set(_REGISTRY)  # drop any stale names
            if not allowed:
                allowed = set(_REGISTRY)  # empty = all (least surprising)
        return cls(
            allowed_modes=frozenset(allowed),
            forced_mode=raw.get("forced_mode"),
        )


# ---------------------------------------------------------------------------
# Effective-mode resolution
# ---------------------------------------------------------------------------


def effective_mode(
    *,
    policy: ClassroomModePolicy,
    student_preference: str | None,
) -> str:
    """Resolve to the concrete mode name the student's session uses.

    Precedence:
      1. ``policy.forced_mode`` (instructor override — not negotiable)
      2. ``student_preference`` if set and allowed
      3. "ask" if allowed (the natural default)
      4. The first mode in ``_FALLBACK_ORDER`` that the policy permits
      5. If nothing is allowed (shouldn't happen), arbitrary pick
    """
    if policy.forced_mode:
        return policy.forced_mode
    if student_preference and policy.is_allowed(student_preference):
        return student_preference
    for candidate in _FALLBACK_ORDER:
        if policy.is_allowed(candidate):
            return candidate
    # Defensive — policy with empty allowed_modes shouldn't exist per
    # from_dict/default, but stay non-crashing.
    return next(iter(policy.allowed_modes), "ask")


__all__ = [
    "ClassroomModePolicy",
    "LearningMode",
    "MODE_REGISTRY",
    "effective_mode",
    "get_mode",
    "list_modes",
]
