# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The export-control hook between extraction and retrieval (ADR-213 D9).

Export control attaches to **code**: a small, declared set of controlled
simulation codes and closely held methods. It does not attach to data. That
gives the hook three questions, asked of every item before any of its text
reaches a retrieval corpus:

1. **Is it an input to a controlled code?** An extractor declared ``ec =
   "payload"`` names the code its file feeds (``result.code``). If that code is
   in the controlled-code registry (``[[controlled_codes]]``, declared data, so
   the list changes without a release), the item is **held**: stored, listed,
   registered, not indexed, until a reviewer releases it.
2. **Is it the code itself?** A source-code file whose text matches a
   controlled code's ``source_sniff`` is held the same way.
3. **Does it carry a control marking?** Every extracted text goes through the
   marker screen the retrieval ingest already runs
   (:func:`axiom.rag.ec_screening.screen_document`): ``controlled`` holds,
   ``sensitive`` indexes to the item's own (site or personal) corpus and is
   flagged for review, which is the screen's own recommendation.

Hold is fail-closed for the whole item: if any extractor's text is held, none
of the item's text is indexed, so a generic text extractor cannot carry a
deck past a domain extractor's verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from axiom.extensions.builtins.library import contrib


@dataclass
class ControlledCode:
    code: str
    basis: str = ""
    source_sniff: tuple[str, ...] = ()
    holds: tuple[str, ...] = ("rag",)
    source: str = ""


@dataclass
class Verdict:
    #: ``clear`` | ``sensitive`` | ``held``.
    verdict: str = "clear"
    reasons: list[str] = field(default_factory=list)
    code: str = ""
    markers: list[str] = field(default_factory=list)

    @property
    def holds_rag(self) -> bool:
        return self.verdict == "held"

    def to_json(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reasons": list(self.reasons), "code": self.code, "markers": list(self.markers)}


def controlled_codes() -> dict[str, ControlledCode]:
    out: dict[str, ControlledCode] = {}
    for c in contrib.contributions("controlled_codes"):
        code = str(c.data.get("code") or "").strip().lower()
        if not code:
            continue
        sniff = c.data.get("source_sniff") or ()
        out[code] = ControlledCode(
            code=code,
            basis=str(c.data.get("basis") or ""),
            source_sniff=(sniff,) if isinstance(sniff, str) else tuple(sniff),
            holds=tuple(c.data.get("holds") or ("rag",)),
            source=c.source,
        )
    return out


def assess(
    file_name: str,
    *,
    texts: list[str],
    payload_codes: list[str],
    code_like: bool,
) -> Verdict:
    """The verdict for one item, from all its extractors' text and declared codes."""
    registry = controlled_codes()
    v = Verdict()
    for code in payload_codes:
        entry = registry.get((code or "").lower())
        if entry is not None and "rag" in entry.holds:
            v.verdict, v.code = "held", entry.code
            v.reasons.append(
                f"an input to {entry.code}, a controlled code"
                + (f" ({entry.basis})" if entry.basis else "")
                + "; its text is not indexed until an export-control reviewer releases it"
            )
    if code_like:
        head = "\n".join(t[:20000] for t in texts)
        for entry in registry.values():
            if any(re.search(p, head, re.M) for p in entry.source_sniff):
                v.verdict, v.code = "held", entry.code
                v.reasons.append(f"its source matches {entry.code}, a controlled code; source is never indexed")
    from axiom.rag.ec_screening import screen_document

    joined = "\n".join(texts)
    screen = screen_document(file_name, joined)
    v.markers = list(screen.markers_found)
    if screen.severity == "controlled":
        v.verdict = "held"
        v.reasons.append("it carries a control marking (" + ", ".join(screen.markers_found[:4]) + ")")
    elif screen.severity == "sensitive" and v.verdict == "clear":
        v.verdict = "sensitive"
        v.reasons.append(
            "it carries a distribution marking (" + ", ".join(screen.markers_found[:4]) + "); "
            "indexed only to its own site or personal corpus, flagged for review"
        )
    return v


__all__ = ["ControlledCode", "Verdict", "assess", "controlled_codes"]
