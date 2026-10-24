# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Salience gate for conversation-turn capture.

A ledger that stores every turn is a transcript, not a memory. The point
of cross-tool capture is that a later session can recover *what was
decided and why* — so the write path asks this module whether a turn
carries something worth recovering before it spends a fragment on it.

Deterministic by construction (per the deterministic-vs-model-mediated
split): scoring is pure, offline, and cheap enough to run on every turn
of a backfill sweep. An LLM-backed summarizer is an injection point at
the call site, never a hidden network hop in here.

Two surfaces:

- :func:`score_turn` — should this turn be written at all?
- :func:`summarize_turn` — the compact line that future sessions read,
  built from the turn's salient signals rather than a blind truncation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

#: Turns whose user side is only one of these are conversational glue —
#: they carry no state a later session needs.
_ACK_ONLY = frozenset({
    "ok", "okay", "k", "kk", "sure", "yes", "y", "yeah", "yep", "no", "n",
    "nope", "thanks", "thank you", "ty", "thx", "cool", "nice", "great",
    "perfect", "continue", "go on", "go ahead", "proceed", "keep going",
    "next", "do it", "please", "stop", "wait", "hmm", "ah", "oh", "done",
    "good", "right", "correct", "exactly", "agreed", "sounds good", "lgtm",
    "+1", "👍", "resume", "carry on", "and?", "?", "..",
    "hi", "hey", "hello", "yo", "morning", "test", "ping",
    "merged", "pushed", "shipped", "same", "both", "all of them",
})

#: Harness placeholders that stand in for content we do not have. An image
#: reference is not a statement; a paste placeholder whose body was never
#: retained is not one either.
_PLACEHOLDER = re.compile(
    r"^\[(?:image|screenshot|pasted text)[^\]]*\]$", re.IGNORECASE
)

#: The user stating a rule, preference, or reversal. These are the
#: highest-value fragments in the ledger — they are why capture exists.
_DECISION_MARKERS = (
    r"\binstead of\b", r"\brather than\b", r"\bnot\s+\w+,\s*use\b",
    r"\balways\b", r"\bnever\b", r"\bmust\b", r"\bshould\b", r"\bprefer\b",
    r"\bwe (?:decided|agreed|settled)\b", r"\bdecision\b", r"\bpolicy\b",
    r"\bfrom now on\b", r"\bgoing forward\b", r"\bstop (?:doing|using)\b",
    r"\bdon'?t\b", r"\bstandard(?:ise|ize)?\b", r"\bconvention\b",
    r"\brequirement\b", r"\bcanonical\b", r"\bthe rule is\b",
)

#: Durable context about people, systems, plans, commitments.
_CONTEXT_MARKERS = (
    r"\bbecause\b", r"\bso that\b", r"\bthe reason\b", r"\broot cause\b",
    r"\bdue (?:by|on)\b", r"\bdeadline\b", r"\bcommitted\b", r"\bowns?\b",
    r"\bblocked (?:on|by)\b", r"\bdepends on\b", r"\btrade-?off\b",
    r"\bwe need\b", r"\bthe plan\b", r"\barchitecture\b", r"\bdesign\b",
)

_SLASH_COMMAND = re.compile(r"^/[a-z][\w:-]*\s*$", re.IGNORECASE)

#: A turn needs this much score to earn a fragment.
SALIENCE_THRESHOLD = 2


@dataclass(frozen=True)
class SalienceVerdict:
    """Why a turn was kept or dropped.

    ``reasons`` is ordered and stable so callers can log or aggregate it;
    ``score`` is exposed for tuning the threshold against a real ledger.
    """

    salient: bool
    score: int
    reasons: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:  # ergonomic: `if score_turn(...)`
        return self.salient


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _is_ack_only(text: str) -> bool:
    """True when the user side is pure acknowledgement or a bare command."""
    stripped = text.strip()
    if not stripped:
        return True
    if _SLASH_COMMAND.match(stripped):
        return True
    if _PLACEHOLDER.match(stripped):
        return True
    normalized = re.sub(r"[.!,\s]+$", "", stripped.lower())
    return normalized in _ACK_ONLY


def score_turn(
    *,
    user_input: str = "",
    assistant_output: str = "",
    tools_used: list[str] | None = None,
    prompt_only: bool = False,
) -> SalienceVerdict:
    """Decide whether this turn is worth a fragment.

    The gate is deliberately generous about *technical substance* and
    strict about *glue*: a long answer to a real question is kept even
    with no decision markers, while "ok" → "Done." is dropped no matter
    how much tool work happened in between.

    ``prompt_only`` loosens the bar for a recovered prompt whose answer
    is permanently gone (history backfill). The scoring below is built
    around a full exchange, where a substantive answer carries most of
    the signal; applied to a bare prompt it penalises exactly the terse,
    high-value statements worth keeping. A 2026-08-20 audit of the live
    history found it dropping "make this a system dependency", "this
    needs to go into our FAQ" and "I will work with <person> on it" —
    decisions, assignments and facts, gone for good. Where the prompt is
    all that survives, the question is only whether it is more than
    acknowledgement.
    """
    user = (user_input or "").strip()
    assistant = (assistant_output or "").strip()

    # A turn with nothing on either side is the hollow-fragment bug.
    if not user and not assistant:
        return SalienceVerdict(False, 0, ("empty",))

    # Glue turns: the user said nothing that constrains future work.
    if _is_ack_only(user):
        return SalienceVerdict(False, 0, ("ack_only",))

    # A recovered prompt is the only surviving record of that turn. Having
    # cleared the glue gate above, it is worth keeping — losing a terse
    # decision permanently is a far worse error than storing a thin one.
    if prompt_only:
        if len(user) >= 12:
            return SalienceVerdict(True, SALIENCE_THRESHOLD, ("prompt_only",))
        return SalienceVerdict(False, 0, ("prompt_only", "too_short"))

    score = 0
    reasons: list[str] = []
    combined = f"{user}\n{assistant}"

    if _matches(_DECISION_MARKERS, combined):
        score += 3
        reasons.append("decision")
    if _matches(_CONTEXT_MARKERS, combined):
        score += 2
        reasons.append("context")

    # Substance proxies. Deliberately graduated rather than one high bar:
    # a short real question with a real answer ("What is k_eff?" → a
    # definition) is exactly the kind of turn worth recovering, so it
    # must clear the threshold on prompt + answer alone. Over-filtering
    # loses content permanently; a little noise is only noise.
    if len(user) >= 12:
        score += 1
        reasons.append("real_prompt")
    if len(user) >= 40:
        score += 1
        reasons.append("substantive_prompt")
    if len(assistant) >= 40:
        score += 1
        reasons.append("answered")
    if len(assistant) >= 200:
        score += 1
        reasons.append("substantive_answer")
    if "?" in user:
        score += 1
        reasons.append("question")

    # Work actually happened — weak signal on its own, never sufficient.
    if tools_used:
        score += 1
        reasons.append("tool_work")

    salient = score >= SALIENCE_THRESHOLD
    if not salient:
        reasons.append("below_threshold")
    return SalienceVerdict(salient, score, tuple(reasons))


def summarize_turn(
    *,
    user_input: str = "",
    assistant_output: str = "",
    tools_used: list[str] | None = None,
    max_chars: int = 240,
) -> str:
    """Build the recall line a future session actually reads.

    Prefers the sentence carrying the decision over the opening words,
    which is the difference between "User: what's this replacing? …" and
    a line that still means something a month later.
    """
    user = " ".join((user_input or "").split())
    assistant = " ".join((assistant_output or "").split())
    if not user and not assistant:
        return ""

    def _key_sentence(text: str) -> str:
        """First sentence bearing a decision/context marker, else the first."""
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if not sentences:
            return ""
        for sentence in sentences:
            if _matches(_DECISION_MARKERS + _CONTEXT_MARKERS, sentence):
                return sentence
        return sentences[0]

    parts: list[str] = []
    if user:
        parts.append(f"User: {_key_sentence(user)}")
    if assistant:
        parts.append(f"Assistant: {_key_sentence(assistant)}")
    if tools_used:
        shown = ", ".join(dict.fromkeys(tools_used))[:60]
        parts.append(f"[tools: {shown}]")

    summary = " → ".join(parts[:2]) + (f" {parts[2]}" if len(parts) > 2 else "")
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary
