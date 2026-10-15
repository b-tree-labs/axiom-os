# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Change detection — the P2 absorb adapters run in watch mode (ADR-087 D2).

A :class:`ChangeDetector` wraps one read-only absorb adapter and turns its
periodic scan into *source-native change events*: it diffs a per-source
content-hash against a baseline and emits a :class:`DetectedChange` for each
added/edited source (a changed instruction file, a new structured-store row).
mtime is the cheap trigger the caller may consult; the content-hash is the
authority, so a touch that does not change bytes is not a change.

Two invariants hold here:

- **Read-only (D8).** The detector only scans; it never writes the source.
- **Echo suppression (marker half).** Before a candidate's hash is taken, the
  Axiom-managed write-back region is stripped (:func:`strip_managed_block`),
  so a fragment we wrote out is never read back as an inbound edit. A file that
  holds *only* our managed block yields no change at all.

Cloud stays skeleton: there is no live cloud polling here (the P2 cloud adapter
remains a skeleton), matching the P4 scope note.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from axiom.memory.absorb.base import AbsorbAdapter, FragmentCandidate
from axiom.memory.rendering import strip_managed_block


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _summary_of(text: str) -> str:
    """First heading, else first non-empty line, of a body — mirrors the
    markdown adapter's title derivation so a cleaned candidate reflects only
    the real source content that survives the managed-block strip."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _clean_candidate(cand: FragmentCandidate) -> FragmentCandidate:
    """Strip our managed write-back region out of a candidate's body text.

    Returns the candidate unchanged when there is nothing to strip. When a
    managed block *was* stripped, the ``summary`` is recomputed from the
    surviving text so a block-derived heading never lingers as source content.
    """
    text = cand.content.get("text")
    if not isinstance(text, str):
        return cand
    stripped = strip_managed_block(text)
    if stripped == text:
        return cand
    new_content = dict(cand.content)
    new_content["text"] = stripped
    if "summary" in new_content:
        new_content["summary"] = _summary_of(stripped)
    return dataclasses.replace(cand, content=new_content)


def _is_empty(cand: FragmentCandidate) -> bool:
    """True when a candidate carries no real source content after stripping."""
    text = cand.content.get("text")
    summary = cand.content.get("summary")
    has_text = isinstance(text, str) and text.strip() != ""
    has_summary = isinstance(summary, str) and summary.strip() != ""
    return not (has_text or has_summary)


def content_hash(cand: FragmentCandidate) -> str:
    """Stable content-hash of a candidate (cleaned, canonical JSON)."""
    blob = json.dumps(cand.content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DetectedChange:
    """One added/edited source, ready to feed the D2 import primitive.

    ``candidates`` are already cleaned (managed block stripped). ``change_id``
    is the idempotency handle the sync service uses so a change lands exactly
    once even across a kill/restart.
    """

    harness: str
    account: str
    source_ref: str
    content_hash: str
    candidates: tuple[FragmentCandidate, ...]
    detected_at: str

    @property
    def change_id(self) -> str:
        key = f"{self.harness}|{self.account}|{self.source_ref}|{self.content_hash}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict:
        return {
            "harness": self.harness,
            "account": self.account,
            "source_ref": self.source_ref,
            "content_hash": self.content_hash,
            "detected_at": self.detected_at,
            "candidates": [
                {"content": c.content, "cognitive_type": c.cognitive_type,
                 "origin": c.origin.to_dict()}
                for c in self.candidates
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> DetectedChange:
        from axiom.memory.fragment import SourceOrigin

        cands = tuple(
            FragmentCandidate(
                content=c["content"],
                cognitive_type=c["cognitive_type"],
                origin=SourceOrigin(**c["origin"]),
            )
            for c in data.get("candidates", [])
        )
        return cls(
            harness=data["harness"],
            account=data["account"],
            source_ref=data["source_ref"],
            content_hash=data["content_hash"],
            candidates=cands,
            detected_at=data["detected_at"],
        )


@dataclass
class ChangeDetector:
    """One adapter in watch mode: scan → per-source hash diff → change events.

    ``baseline`` maps ``source_ref → content_hash`` and advances on each poll,
    so a running process does not re-emit an unchanged source. A restart starts
    with an empty baseline and re-detects everything — safe because inbound
    import is idempotent and the pending queue dedups by ``change_id``.
    """

    adapter: AbsorbAdapter
    now_fn: Callable[[], str] = _now
    baseline: dict[str, str] = field(default_factory=dict)

    @property
    def harness(self) -> str:
        return self.adapter.harness

    def poll(self) -> list[DetectedChange]:
        """Return a change event per added/edited source; advance the baseline."""
        scan = self.adapter.scan()
        current: dict[str, str] = {}
        changes: list[DetectedChange] = []
        for raw in scan.candidates:
            cand = _clean_candidate(raw)
            if _is_empty(cand):
                continue  # purely our managed block — not a source
            ref = cand.origin.source_ref
            digest = content_hash(cand)
            current[ref] = digest
            if self.baseline.get(ref) == digest:
                continue  # unchanged
            changes.append(
                DetectedChange(
                    harness=cand.origin.harness,
                    account=cand.origin.account,
                    source_ref=ref,
                    content_hash=digest,
                    candidates=(cand,),
                    detected_at=self.now_fn(),
                )
            )
        self.baseline = current
        return changes


__all__ = ["ChangeDetector", "DetectedChange", "content_hash"]
