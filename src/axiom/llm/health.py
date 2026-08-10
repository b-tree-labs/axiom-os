# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Model-coherence gate (axiom-os #499).

``routing_health`` answers "is the endpoint reachable AND is the configured
model pulled?". Both can be green while the *served model itself* emits
garbage — the field incident that motivated this primitive: a degenerate
``bonsai-1.7b`` looped incoherent completions and drifted undetected for 68
days. This module adds the missing quality layer:

- :func:`score_coherence` — a deterministic, model-free degeneracy floor
  (repetition loops, low token diversity, gibberish/token-salad). No second
  model required, so it can gate the very model under test.
- :func:`check_model_coherence` — runs a small known-answer probe battery
  against a ``generate`` callable and returns a :class:`CoherenceReport`.

Per the routing_health tolerance guarantee, the gate NEVER raises: a backend
that errors yields a well-formed unhealthy report so operator surfaces can call
it without a try/except wrapper. Following Ben's "deterministic safety floors
UNDER LLM judgment" principle (cf. TIDY) — heuristics here are a floor that
catches the obviously-broken; a richer LLM-judged probe can layer on top later.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Degeneracy thresholds (the floor). Tuned to pass terse-but-valid answers
# ("Paris", "4", "OK") while failing loops/gibberish.
# ---------------------------------------------------------------------------

_MIN_DISTINCT_RATIO = 0.35  # distinct tokens / total; loops crater this
_MAX_REPETITION_RATIO = 0.5  # fraction of tokens inside a repeated run/bigram
_MAX_GIBBERISH_RATIO = 0.5  # fraction of tokens that look non-linguistic
_LONG_TOKEN_CHARS = 30  # a "word" longer than this is token salad
_SHORT_TEXT_TOKENS = 4  # below this, only the empty/gibberish checks apply

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_VOWEL_RE = re.compile(r"[aeiouyAEIOUY]")


@dataclass(frozen=True)
class CoherenceScore:
    """Result of the deterministic degeneracy floor for one text."""

    text: str
    coherent: bool
    distinct_ratio: float
    repetition_ratio: float
    gibberish_ratio: float
    reasons: tuple[str, ...] = ()


def _max_consonant_run(token: str) -> int:
    run = best = 0
    for c in token:
        if c.isalpha() and not _VOWEL_RE.match(c):
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _looks_gibberish(token: str) -> bool:
    """Heuristic: a single token that is not a plausible word/number."""
    if token.isdigit():
        return False
    if len(token) > _LONG_TOKEN_CHARS:
        return True
    alpha = [c for c in token if c.isalpha()]
    if len(alpha) >= 5 and not _VOWEL_RE.search(token):
        # Long all-consonant run — "qwrtzxv".
        return True
    if len(alpha) >= 5 and _max_consonant_run(token) >= 4:
        # Keyboard-mash with stray vowels — "asdkfj", "qweptzxv".
        return True
    return False


def score_coherence(
    text: str,
    *,
    min_distinct_ratio: float = _MIN_DISTINCT_RATIO,
    max_repetition_ratio: float = _MAX_REPETITION_RATIO,
    max_gibberish_ratio: float = _MAX_GIBBERISH_RATIO,
) -> CoherenceScore:
    """Score *text* against the model-free degeneracy floor.

    A ``coherent=False`` result means the text is *obviously* broken (empty,
    looping, or token-salad). Passing the floor is necessary, not sufficient,
    for correctness — known-answer probes (see :func:`check_model_coherence`)
    add the semantic check.
    """
    reasons: list[str] = []
    stripped = (text or "").strip()
    if not stripped:
        return CoherenceScore(text or "", False, 0.0, 1.0, 0.0, ("empty",))

    # Punctuation-only / no-word content is gibberish.
    tokens = _WORD_RE.findall(stripped)
    if not tokens:
        return CoherenceScore(text, False, 0.0, 0.0, 1.0, ("no_word_content",))

    lowered = [t.lower() for t in tokens]
    n = len(lowered)
    distinct_ratio = len(set(lowered)) / n

    # Repetition: longest consecutive run of one token, and repeated-bigram mass.
    longest_run = 1
    cur = 1
    for a, b in zip(lowered, lowered[1:]):
        cur = cur + 1 if a == b else 1
        longest_run = max(longest_run, cur)
    bigrams = list(zip(lowered, lowered[1:]))
    repeated_bigram_mass = 0.0
    if bigrams:
        seen: dict[tuple[str, str], int] = {}
        for bg in bigrams:
            seen[bg] = seen.get(bg, 0) + 1
        repeated = sum(c for c in seen.values() if c > 1)
        repeated_bigram_mass = repeated / len(bigrams)
    repetition_ratio = max(longest_run / n, repeated_bigram_mass)

    gibberish_ratio = sum(1 for t in tokens if _looks_gibberish(t)) / n

    # Short answers: only the hard signals (empty/gibberish) apply — don't
    # punish "Paris" / "4" / "OK" for low distinctness or single tokens.
    if n < _SHORT_TEXT_TOKENS:
        if gibberish_ratio > max_gibberish_ratio:
            reasons.append("gibberish")
        coherent = not reasons
        return CoherenceScore(
            text, coherent, distinct_ratio, repetition_ratio, gibberish_ratio, tuple(reasons)
        )

    if repetition_ratio > max_repetition_ratio:
        reasons.append(f"repetition({repetition_ratio:.2f})")
    if distinct_ratio < min_distinct_ratio:
        reasons.append(f"low_distinct({distinct_ratio:.2f})")
    if gibberish_ratio > max_gibberish_ratio:
        reasons.append(f"gibberish({gibberish_ratio:.2f})")

    return CoherenceScore(
        text,
        not reasons,
        distinct_ratio,
        repetition_ratio,
        gibberish_ratio,
        tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Probe battery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoherenceProbe:
    """A canary prompt with an optional known-answer check.

    ``must_contain`` substrings are matched case-insensitively against the
    response; an empty tuple means "coherence only, no semantic check".
    """

    id: str
    prompt: str
    must_contain: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeResult:
    probe_id: str
    response: str
    coherent: bool  # passed the degeneracy floor
    answered: bool  # contained the known answer (True if no must_contain)
    score: CoherenceScore | None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.coherent and self.answered and self.error is None


@dataclass(frozen=True)
class CoherenceReport:
    healthy: bool
    pass_rate: float
    probes: tuple[ProbeResult, ...]
    summary: str = ""


# Known-answer canaries: arithmetic, world fact, instruction-following. A
# degenerate model fails the coherence floor on all three; a coherent-but-broken
# model fails the must_contain checks.
DEFAULT_PROBES: tuple[CoherenceProbe, ...] = (
    CoherenceProbe("arithmetic", "What is 2 + 2? Reply with just the number.", ("4",)),
    CoherenceProbe("world_fact", "What is the capital of France? Answer in one word.", ("paris",)),
    CoherenceProbe("instruction", "Reply with exactly the word: OK", ("ok",)),
)


def check_model_coherence(
    generate: Callable[[str], str],
    probes: tuple[CoherenceProbe, ...] = DEFAULT_PROBES,
    *,
    min_pass_rate: float = 0.66,
) -> CoherenceReport:
    """Run the probe battery against *generate* and gate on the pass rate.

    *generate* maps a prompt to a completion string. The gate is tolerant: any
    exception from *generate* becomes a failed :class:`ProbeResult` with an
    ``error`` rather than propagating, so operator surfaces never need a
    try/except wrapper.
    """
    results: list[ProbeResult] = []
    for probe in probes:
        try:
            response = generate(probe.prompt)
        except Exception as exc:  # noqa: BLE001 — tolerance is the contract
            results.append(
                ProbeResult(probe.id, "", False, False, None, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        score = score_coherence(response or "")
        if probe.must_contain:
            low = (response or "").lower()
            answered = any(sub.lower() in low for sub in probe.must_contain)
        else:
            answered = True
        results.append(ProbeResult(probe.id, response or "", score.coherent, answered, score))

    passed = sum(1 for r in results if r.passed)
    pass_rate = passed / len(results) if results else 0.0
    healthy = pass_rate >= min_pass_rate
    failed = [r.probe_id for r in results if not r.passed]
    summary = (
        f"{passed}/{len(results)} probes passed (rate {pass_rate:.2f}); "
        + ("healthy" if healthy else f"DEGRADED — failed: {', '.join(failed)}")
    )
    return CoherenceReport(healthy, pass_rate, tuple(results), summary)
