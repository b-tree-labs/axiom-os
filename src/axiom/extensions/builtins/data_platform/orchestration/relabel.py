# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Relabel what a connector already indexed with the labels it now declares (#1971).

Before #1971 ingest wrote no access labels, so every chunk a connector landed
reads as public and unclassified whatever the source is. Declaring
``access_tier`` / ``classification`` on the connector fixes what it lands from
now on; this backfill brings what it already landed in line.

It is a reviewable dry run first: :func:`plan_relabel` reports, per document,
what would change and whether the change tightens or loosens. :func:`apply_relabel`
writes only the tightening changes unless the caller explicitly allows
loosening, because a loosened label shows a document to people who could not
see it before, and that is never done silently.

The documents come from the connector's bronze manifests (the provenance of
what it landed). Interim shortcut, like ``data.reindex``: on the designed route
(ADR-079) labels are applied by gold curation and this backfill is a projection
rebuild.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from axiom.rag.store import ACCESS_TIERS

UNCHANGED = "unchanged"
TIGHTEN = "tighten"
LOOSEN = "loosen"
NOT_INDEXED = "not_indexed"


class _LabelStore(Protocol):
    def document_labels(self, path: str, corpus: str) -> tuple[str, str] | None: ...

    def set_document_labels(
        self, path: str, corpus: str, *, access_tier: str, classification: str
    ) -> int: ...


@dataclass(frozen=True)
class LabelChange:
    source_path: str
    corpus: str
    current: tuple[str, str] | None
    declared: tuple[str, str]
    kind: str


@dataclass
class RelabelPlan:
    source: str
    declared: tuple[str, str] | None
    changes: list[LabelChange] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = Counter(ch.kind for ch in self.changes)
        return {k: c.get(k, 0) for k in (TIGHTEN, LOOSEN, UNCHANGED, NOT_INDEXED)}

    def to_dict(self, *, examples: int = 5) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "declared": (
                {"access_tier": self.declared[0], "classification": self.declared[1]}
                if self.declared
                else None
            ),
            "counts": self.counts,
        }
        for kind in (TIGHTEN, LOOSEN):
            shown = [c for c in self.changes if c.kind == kind][:examples]
            out[f"{kind}_examples"] = [
                {
                    "source_path": c.source_path,
                    "corpus": c.corpus,
                    "from": list(c.current) if c.current else None,
                    "to": list(c.declared),
                }
                for c in shown
            ]
        return out


def classify_change(current: tuple[str, str], declared: tuple[str, str]) -> str:
    """Whether moving ``current`` to ``declared`` tightens, loosens or changes nothing.

    A wider tier, dropping a classification back to ``unclassified``, or swapping
    one classification for another all count as loosening: anything that might
    show the document to someone who could not see it is the cautious case.
    """
    if current == declared:
        return UNCHANGED
    cur_tier, cur_cls = current
    new_tier, new_cls = declared
    cur_rank = ACCESS_TIERS.index(cur_tier) if cur_tier in ACCESS_TIERS else len(ACCESS_TIERS)
    new_rank = ACCESS_TIERS.index(new_tier)
    if new_rank < cur_rank:
        return LOOSEN
    if cur_cls != new_cls and cur_cls != "unclassified":
        return LOOSEN
    return TIGHTEN


def plan_relabel(
    manifests: Iterable[dict[str, Any]],
    store: _LabelStore,
    *,
    source: str,
    declared: dict[str, str] | None,
    default_corpus: str = "rag-community",
) -> RelabelPlan:
    """Compare each indexed document of ``source`` with the labels it declares. Writes nothing."""
    if not declared:
        return RelabelPlan(source=source, declared=None)
    target = (declared["access_tier"], declared["classification"])
    plan = RelabelPlan(source=source, declared=target)
    seen: set[tuple[str, str]] = set()
    for rec in manifests:
        if rec.get("disposition", "allow") != "allow":
            continue
        path = rec.get("source_path") or f"{rec.get('source_name', source)}/{rec.get('item_id')}"
        corpus = rec.get("tier") or default_corpus
        if (path, corpus) in seen:
            continue
        seen.add((path, corpus))
        current = store.document_labels(path, corpus)
        kind = NOT_INDEXED if current is None else classify_change(current, target)
        plan.changes.append(LabelChange(path, corpus, current, target, kind))
    return plan


def apply_relabel(
    plan: RelabelPlan, store: _LabelStore, *, allow_loosen: bool = False
) -> dict[str, int]:
    """Write the plan's tightening changes, and its loosening ones only when allowed."""
    out = {"documents": 0, "chunks": 0, "loosen_withheld": 0}
    if plan.declared is None:
        return out
    tier, classification = plan.declared
    for ch in plan.changes:
        if ch.kind == LOOSEN and not allow_loosen:
            out["loosen_withheld"] += 1
            continue
        if ch.kind not in (TIGHTEN, LOOSEN):
            continue
        out["chunks"] += store.set_document_labels(
            ch.source_path, ch.corpus, access_tier=tier, classification=classification
        )
        out["documents"] += 1
    return out


__all__ = [
    "LOOSEN",
    "NOT_INDEXED",
    "TIGHTEN",
    "UNCHANGED",
    "LabelChange",
    "RelabelPlan",
    "apply_relabel",
    "classify_change",
    "plan_relabel",
]
