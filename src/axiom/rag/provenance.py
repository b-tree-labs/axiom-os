# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Output-provenance gate — the post-synthesis complement to ``grounding``.

The Problem
-----------
``grounding.evaluate_grounding`` runs *before* the LLM and asks "is the
retrieved evidence strong enough?". But even with good retrieval a model
will, on a bad turn, assert a specific quantity it never retrieved and
dress it in a fabricated citation ("I retrieved this from the ``X`` tool")
on a turn where no tool ran. The citation makes the fabrication *harder*
to catch than an honest guess. Lowering temperature does not help — a
deterministic model repeats the same fabrication every time.

The Primitive
-------------
This module runs *after* synthesis and fails closed on two patterns,
both domain-agnostic:

1. **Fabricated provenance** — the answer claims a value came from a tool
   / retrieval, but no tool was dispatched this turn.
2. **Unsupported quantity** — the answer states a specific value matching
   a consumer-supplied quantity pattern whose numeric core appears in
   neither the grounding corpus (retrieved evidence + tool results +
   prior turns) nor the consumer's small set of authoritative constants.

What counts as a "quantity", which constants are stateable without
evidence, and what a blocked answer is replaced with are all supplied by
the caller via :class:`ProvenanceGateConfig`, so this module names no
domain consumer. The reactor / classroom / any consumer layer injects its
own patterns; the module's default config covers only currency and dates.

Bare dates are tracked but never fail-closed on their own — too many
legitimate answers cite a date — while still counting toward the corpus
that supports other claims.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Config + decision shapes
# ---------------------------------------------------------------------------

# Generic, domain-free defaults: currency amounts and ISO dates. Consumers
# extend ``quantity_patterns`` with their own units (a reactor layer adds
# temperatures, energy, reactivity, rod travel; a finance layer adds bps; etc).
_DEFAULT_QUANTITY_PATTERNS: tuple[str, ...] = (
    r"-?\$\s?\d+(?:\.\d+)?",            # $1.23
    r"\b\d+(?:\.\d+)?\s?cents?\b",      # 45 cents
    r"\b20\d{2}-\d{2}-\d{2}\b",         # 2026-07-16 (date; non-gating by default)
)
_DEFAULT_NON_GATING_PATTERNS: tuple[str, ...] = (r"^\s*20\d{2}-\d{2}-\d{2}\s*$",)
_DEFAULT_PROVENANCE_PATTERNS: tuple[str, ...] = (
    r"\bI\s+(?:retrieved|obtained|pulled|queried|fetched|got|accessed|looked\s+up)\b[^.\n]{0,80}\btool\b",
    r"\bby\s+calling\s+the\b[^.\n]{0,40}\btool\b",
    r"\bexact\s+data\s+returned\b",
    r"\bthe\s+tool\s+returned\b",
)
_DEFAULT_ABSTENTION = (
    "I don't have a retrieved, tool-verified value for that on this turn, so I "
    "won't state a number I haven't actually confirmed. Ask me to pull it from "
    "the data tools and I'll show the exact figures with their source."
)

# Pull the bare numeric core out of a matched claim for a looser "is this value
# present in the corpus" test, so "375 C" in an answer matches "375C" in a doc.
_NUM_RE = re.compile(r"20\d{2}-\d{2}-\d{2}|-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ProvenanceGateConfig:
    """Everything the gate needs, injected by the consumer layer.

    Attributes
    ----------
    quantity_patterns
        Regexes matching the specific values a grounded answer must have a
        source for. Each is searched independently (``finditer``), so a
        pattern may contain capturing groups without corrupting the others.
    authoritative_values
        Numeric-core strings the model may state without any evidence
        (published constants / limits). Kept small and exact so it cannot
        launder a fabricated value that merely shares digits with a real
        one — matching is on the extracted numeric core, e.g. ``"1.1"``.
    provenance_claim_patterns
        Regexes for language asserting a value came from a tool / retrieval.
        A match with an empty ``tools_called`` is fabricated provenance.
    non_gating_patterns
        Claim shapes tracked (they support other claims) but never failed
        closed on their own — bare dates by default.
    abstention
        The user-facing replacement for a blocked answer.
    equivalence_scales
        Unit-conversion factors under which a stated value counts as
        supported (e.g. ``(1.0, 100.0, 0.01)`` lets a cents figure match a
        dollars figure in the evidence). Keep the set small: every extra
        scale widens what a fabricated value could accidentally match.
    numeric_abs_tol / numeric_rel_tol
        Tolerances for value-equivalence matching. Support is decided on the
        parsed VALUE, not the string, so conventional formatting ("$2.90"
        vs a serialized ``2.9``) and honest rounding ("$3.40" for 3.396)
        do not read as fabrication. The defaults absorb display-precision
        rounding while still rejecting anything off by more than ~0.2%.
    """

    quantity_patterns: tuple[str, ...] = _DEFAULT_QUANTITY_PATTERNS
    authoritative_values: frozenset[str] = frozenset()
    provenance_claim_patterns: tuple[str, ...] = _DEFAULT_PROVENANCE_PATTERNS
    non_gating_patterns: tuple[str, ...] = _DEFAULT_NON_GATING_PATTERNS
    abstention: str = _DEFAULT_ABSTENTION
    equivalence_scales: tuple[float, ...] = (1.0,)
    numeric_abs_tol: float = 0.006
    numeric_rel_tol: float = 0.002

    def with_patterns(
        self,
        *,
        quantity_patterns: Sequence[str] = (),
        authoritative_values: Iterable[str] = (),
    ) -> ProvenanceGateConfig:
        """Return a copy with consumer patterns/constants appended to the
        defaults. Convenience so a consumer adds units without restating the
        currency/date baseline."""
        from dataclasses import replace

        return replace(
            self,
            quantity_patterns=self.quantity_patterns + tuple(quantity_patterns),
            authoritative_values=self.authoritative_values | frozenset(authoritative_values),
        )


@dataclass(frozen=True)
class ProvenanceDecision:
    """Outcome of the gate. ``grounded`` False means the answer should be
    withheld and replaced with the config's abstention. ``reason`` is
    human-readable for audit logs / dev tools."""

    grounded: bool
    reason: str
    unsupported: tuple[str, ...] = ()
    false_provenance: bool = False


# ---------------------------------------------------------------------------
# Compiled-pattern cache (configs are frozen + hashable-ish; cache by id)
# ---------------------------------------------------------------------------

_PatternSet = list[re.Pattern[str]]
_CompiledSets = tuple[_PatternSet, _PatternSet, _PatternSet]
_COMPILE_CACHE: dict[int, _CompiledSets] = {}


def _compiled(config: ProvenanceGateConfig) -> _CompiledSets:
    key = id(config)
    hit = _COMPILE_CACHE.get(key)
    if hit is None:
        hit = (
            [re.compile(p, re.IGNORECASE) for p in config.quantity_patterns],
            [re.compile(p, re.IGNORECASE) for p in config.provenance_claim_patterns],
            [re.compile(p, re.IGNORECASE) for p in config.non_gating_patterns],
        )
        _COMPILE_CACHE[key] = hit
    return hit


def _norm(s: str) -> str:
    return (s or "").replace(" ", "").lower()


def _numeric_core(claim: str) -> str:
    m = _NUM_RE.search(claim)
    return m.group(0) if m else claim


# Every float-looking token in the corpus, for value-equivalence matching.
_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_MAX_CORPUS_NUMBERS = 20_000  # runaway-input backstop


def _corpus_numbers(texts: Iterable[str]) -> set[float]:
    """Parse every numeric token in the corpus, then augment with the numeric
    CLOSURE of tool-result JSON arrays: for each list of objects, the per-key
    sum and mean. An answer that honestly aggregates a tool table ("total
    $13.58" over four per-rod worths) is then supported even though the total
    appears nowhere verbatim — while a value unrelated to the evidence still
    matches nothing."""
    nums: set[float] = set()
    for t in texts:
        if not t:
            continue
        for m in _FLOAT_RE.finditer(t):
            try:
                nums.add(float(m.group(0)))
            except ValueError:  # pragma: no cover - regex precludes
                continue
            if len(nums) >= _MAX_CORPUS_NUMBERS:
                return nums
        if "{" in t or "[" in t:
            try:
                nums |= _json_array_closure(json.loads(t))
            except (ValueError, RecursionError):
                continue
    return nums


def _json_array_closure(node: object, out: set[float] | None = None) -> set[float]:
    if out is None:
        out = set()
    if isinstance(node, dict):
        for v in node.values():
            _json_array_closure(v, out)
    elif isinstance(node, list):
        dict_items = [x for x in node if isinstance(x, dict)]
        if len(dict_items) >= 2:
            keys: dict[str, list[float]] = {}
            for item in dict_items:
                for k, v in item.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        keys.setdefault(k, []).append(float(v))
            for vals in keys.values():
                if len(vals) >= 2:
                    total = sum(vals)
                    out.add(total)
                    out.add(total / len(vals))
        for x in node:
            _json_array_closure(x, out)
    return out


def _value_supported(claim_core: str, numbers: set[float], cfg: ProvenanceGateConfig) -> bool:
    """True when the claim's parsed value matches any corpus value under a
    configured unit scale, within display-rounding tolerance."""
    if "-" in claim_core[1:]:  # date-shaped core: string matching only
        return False
    try:
        v = float(claim_core)
    except ValueError:
        return False
    tol = max(cfg.numeric_abs_tol, cfg.numeric_rel_tol * abs(v))
    return any(
        abs(c * s - v) <= tol for c in numbers for s in cfg.equivalence_scales
    )


# ---------------------------------------------------------------------------
# evaluate_answer_provenance
# ---------------------------------------------------------------------------


def evaluate_answer_provenance(
    answer: str,
    *,
    grounded_texts: Iterable[str] = (),
    tools_called: Sequence[str] = (),
    config: ProvenanceGateConfig | None = None,
) -> ProvenanceDecision:
    """Decide whether ``answer`` stays within its evidence.

    Parameters
    ----------
    answer
        The synthesized answer text (tool-call markup already stripped).
    grounded_texts
        The corpus that legitimately supports a claim this turn: retrieved
        document text, serialized tool results, and prior-turn conversation.
        The caller assembles this; the gate treats it as one opaque bag.
    tools_called
        Names of tools/verbs dispatched this turn. Non-empty disables the
        fabricated-provenance check (a real dispatch backs the citation).
    config
        Consumer-supplied patterns/constants. Defaults to currency + dates.

    Returns
    -------
    ProvenanceDecision
        ``grounded`` True to serve as-is; False to withhold and abstain.
    """
    cfg = config or ProvenanceGateConfig()
    text = (answer or "").strip()
    if not text:
        return ProvenanceDecision(grounded=True, reason="empty answer")

    quantity_res, provenance_res, non_gating_res = _compiled(cfg)

    # 1) Fabricated provenance: claims a tool sourced a value, none ran.
    if not tools_called:
        for pr in provenance_res:
            if pr.search(text):
                return ProvenanceDecision(
                    grounded=False,
                    reason="asserts tool/retrieval provenance but no tool was dispatched this turn",
                    false_provenance=True,
                )

    # 2) Unsupported quantities. A claim is supported if its numeric core is an
    # authoritative constant, appears verbatim in the grounding corpus, or —
    # value-equivalence — parses to a number matching a corpus value (or an
    # aggregate of a tool-result array) under a configured unit scale within
    # display-rounding tolerance.
    texts = list(grounded_texts)
    corpus = _norm(" ".join(texts))
    numbers: set[float] | None = None  # built lazily; corpora can be large
    seen: set[str] = set()
    unsupported: list[str] = []
    for qr in quantity_res:
        for m in qr.finditer(text):
            claim = m.group(0).strip()
            if claim in seen:
                continue
            seen.add(claim)
            if any(ng.match(claim) for ng in non_gating_res):
                continue  # tracked but never gated on its own (e.g. bare date)
            core = _numeric_core(claim)
            if core in cfg.authoritative_values:
                continue
            if _norm(core) and _norm(core) in corpus:
                continue
            if numbers is None:
                numbers = _corpus_numbers(texts)
            if _value_supported(core, numbers, cfg):
                continue
            unsupported.append(claim)

    if unsupported:
        return ProvenanceDecision(
            grounded=False,
            reason=f"states unsupported value(s) {unsupported[:6]} with no tool result or evidence",
            unsupported=tuple(unsupported),
        )

    return ProvenanceDecision(
        grounded=True, reason="all specific claims supported or tool-grounded"
    )


def enforce_answer_provenance(
    answer: str,
    *,
    grounded_texts: Iterable[str] = (),
    tools_called: Sequence[str] = (),
    config: ProvenanceGateConfig | None = None,
) -> tuple[str, ProvenanceDecision]:
    """Convenience wrapper: return ``(answer_or_abstention, decision)``.

    Fails closed — on an ungrounded decision the original answer is replaced
    with ``config.abstention`` so a fabrication never reaches the user."""
    cfg = config or ProvenanceGateConfig()
    decision = evaluate_answer_provenance(
        answer, grounded_texts=grounded_texts, tools_called=tools_called, config=cfg
    )
    return (answer if decision.grounded else cfg.abstention), decision


__all__ = [
    "ProvenanceGateConfig",
    "ProvenanceDecision",
    "evaluate_answer_provenance",
    "enforce_answer_provenance",
]
