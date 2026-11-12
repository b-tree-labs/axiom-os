# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The D2 import primitive for absorbed memories (ADR-087 D2).

Absorb, migrate, and sync are one operation: ``import(fragments,
from_coord → to_coord)`` with origin-preserving provenance. This module
is that primitive for adapter-scanned candidates; the P0 bundle path
(``memory.import`` skill) is the same primitive for signed bundles.

Per candidate:

- ``vault`` never absorbs (ADR-088 invariant asserted inbound) —
  skip-with-audit.
- The ``(harness, account, source_ref)`` idempotency key suppresses
  echo: a source memory that already landed (same key, same content)
  is never re-imported, so absorb → re-absorb is a no-op.
- Same key with *different* content is the conflict tier: the new
  content is written (kept-both — never silent loss, never overwrite)
  and the pair is queued for review.
- Shape-invalid candidates degrade to skip-with-audit, never a crash
  and never a partial write of that candidate.

All writes land through ``CompositionService.write`` with the origin
coordinate stamped — the single door in. Adapters never write.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Iterable

from axiom.memory.fragment import CognitiveType

from .base import FragmentCandidate, SkippedSource

_AGENT = "axi-memory"


@dataclass
class ImportReport:
    """What one ``import_candidates`` run did (or would do, dry-run)."""

    imported: int = 0
    skipped_echo: int = 0
    conflicts_queued: int = 0
    collapsed_exact: int = 0
    merged_near_dup: int = 0
    secrets_vaulted: int = 0
    skipped: list[SkippedSource] = field(default_factory=list)
    fragment_ids: list[str] = field(default_factory=list)
    dry_run: bool = False


def _candidate_text(cand: FragmentCandidate) -> str:
    """Render a candidate to the text the secret classifier inspects — the same
    rendering the serving gate uses, so inbound and outbound agree."""
    from axiom.memory.recall_projection import _render_text

    try:
        ct = CognitiveType.from_string(cand.cognitive_type)
    except ValueError:
        ct = CognitiveType.SEMANTIC
    return _render_text(ct, cand.content)


def _origin_index(composition: Any) -> dict[tuple[str, str, str], list[dict]]:
    """Idempotency-key → stored fragment payloads, live rows only."""
    index: dict[tuple[str, str, str], list[dict]] = {}
    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data or {}
        origin = (data.get("provenance") or {}).get("origin")
        if not origin:
            continue
        key = (
            origin.get("harness", ""),
            origin.get("account", ""),
            origin.get("source_ref", ""),
        )
        index.setdefault(key, []).append(data)
    return index


def import_candidates(
    composition: Any,
    candidates: Iterable[FragmentCandidate],
    *,
    principal: str,
    agent: str = _AGENT,
    accountable_human_id: str | None = None,
    dedup: Any | None = None,
    secret_detector: Callable[[str], str | None] | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Land adapter candidates in the ledger via the D2 primitive.

    ``dedup`` is an optional :class:`axiom.memory.dedup.DedupEngine`;
    when provided, every genuinely-new candidate additionally runs the
    write-time near-neighbor check (exact auto-collapse, reversible
    near-dup merge, ambiguous kept-both + queued).

    ``secret_detector`` wires the OQ6 inbound half (ADR-087; security doc §4):
    a genuinely-new candidate whose rendered text reads as a programmatic
    secret is routed to ``vault`` — retained but unservable and unprojectable
    (ADR-088) — instead of landing as a plain fragment. Adapter-emitted vault
    candidates are still refused (the P2 inbound floor is unchanged); this is
    Axiom's *own* classification of plaintext secrets on the way in. Default
    ``None`` keeps the P2 behavior byte-for-byte.
    """
    report = ImportReport(dry_run=dry_run)
    index = _origin_index(composition)

    def _skip(source: str, reason: str) -> None:
        report.skipped.append(SkippedSource(source=source, reason=reason))
        if not dry_run:
            composition.audit_log.record(
                entry_type="absorb_skipped",
                principal_id=principal,
                agent_id=agent,
                fragment_id="",
                outcome=reason,
                source=source,
            )

    for cand in candidates:
        coord = f"{cand.origin.harness}/{cand.origin.account}/{cand.origin.source_ref}"

        if cand.cognitive_type == CognitiveType.VAULT.value:
            _skip(coord, "vault_never_absorbed")
            continue

        key = cand.origin.idempotency_key
        existing = index.get(key, [])
        if any(e.get("content") == cand.content for e in existing):
            report.skipped_echo += 1
            continue

        conflict_with = [e["id"] for e in existing]

        # OQ6 inbound half: genuinely-new secret-class content routes to vault
        # (unservable, unprojected) — never a plain fragment, never queued into
        # the human-facing conflict review (secret material stays opaque).
        matched = (
            secret_detector(_candidate_text(cand))
            if secret_detector is not None
            else None
        )
        if matched:
            if dry_run:
                report.secrets_vaulted += 1
                continue
            try:
                frag = composition.write(
                    content=dict(cand.content),
                    cognitive_type=CognitiveType.VAULT.value,
                    principal_id=principal,
                    agents={agent},
                    resources=set(),
                    accountable_human_id=(
                        accountable_human_id
                        if accountable_human_id is not None
                        else principal
                    ),
                    origin=cand.origin,
                )
            except ValueError as exc:
                _skip(coord, f"invalid_candidate: {exc}")
                continue
            report.secrets_vaulted += 1
            report.fragment_ids.append(frag.id)
            index.setdefault(key, []).append(frag.to_dict())
            composition.audit_log.record(
                entry_type="absorb_secret_vaulted",
                principal_id=principal,
                agent_id=agent,
                fragment_id=frag.id,
                outcome=matched,
                source=coord,
            )
            continue

        if dry_run:
            report.imported += 1
            if conflict_with:
                report.conflicts_queued += 1
            continue

        try:
            frag = composition.write(
                content=dict(cand.content),
                cognitive_type=cand.cognitive_type,
                principal_id=principal,
                agents={agent},
                resources=set(),
                accountable_human_id=(
                    accountable_human_id
                    if accountable_human_id is not None
                    else principal
                ),
                origin=cand.origin,
            )
        except ValueError as exc:
            _skip(coord, f"invalid_candidate: {exc}")
            continue

        report.imported += 1
        report.fragment_ids.append(frag.id)
        index.setdefault(key, []).append(frag.to_dict())

        if conflict_with:
            from axiom.memory.dedup import queue_conflict

            queue_conflict(
                composition,
                principal=principal,
                fragment_ids=[*conflict_with, frag.id],
                reason="same_source_ref_content_drift",
                sources=[cand.origin.to_dict()],
            )
            report.conflicts_queued += 1
            continue

        if dedup is not None:
            outcome = dedup.write_time_check(
                composition, frag, principal=principal,
            )
            if outcome == "collapsed_exact":
                report.collapsed_exact += 1
            elif outcome == "merged_near_dup":
                report.merged_near_dup += 1
            elif outcome == "conflict_queued":
                report.conflicts_queued += 1

    if not dry_run:
        composition.audit_log.record(
            entry_type="absorb",
            principal_id=principal,
            agent_id=agent,
            fragment_id="",
            outcome="ok",
            imported=report.imported,
            skipped_echo=report.skipped_echo,
            conflicts_queued=report.conflicts_queued,
            collapsed_exact=report.collapsed_exact,
            merged_near_dup=report.merged_near_dup,
            secrets_vaulted=report.secrets_vaulted,
            skipped=len(report.skipped),
        )
    return report


__all__ = ["ImportReport", "import_candidates"]
