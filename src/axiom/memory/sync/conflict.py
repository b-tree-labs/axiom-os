# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Streaming conflict policy — last-writer-wins by event time (ADR-087 OQ2, F6).

Continuous sync needs a conflict default (ADR-087 open question 2): P4 picks
**last-writer-wins by event time**. When two harnesses edit the same logical
memory, the later event time wins and propagates outbound; the earlier one is
the *loser*.

Crucially this reuses the P2 conflict review queue rather than building a
second one: the conflicting fragments are already kept-both and queued by the
importer / dedup engine (never silent loss). This module layers a durable
*resolution* record on top of an open conflict — naming the winner and the
loser(s) and the policy — so the outbound path can suppress the loser while the
full pair remains in the human-reviewable queue. No fragment is ever deleted.

Event time is read in priority order: an explicit ``event_time`` in content
(episodic), else the source's ``imported_at`` (when the edit was absorbed), else
the fragment's write timestamp. Ties break deterministically on fragment id.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from axiom.memory.dedup import list_conflicts
from axiom.memory.fragment import MemoryFragment, fragment_from_dict

SYNC_RESOLUTION_KIND = "memory_conflict_resolution"

_AGENT = "axi-memory"


def _pair_key(fragment_ids: list[str]) -> str:
    joined = "|".join(sorted(fragment_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def event_time(fragment: MemoryFragment) -> str:
    """The LWW ordering key for a fragment (see module docstring)."""
    ev = fragment.content.get("event_time")
    if isinstance(ev, str) and ev.strip():
        return ev
    origin = fragment.provenance.origin
    if origin is not None and origin.imported_at:
        return origin.imported_at
    return fragment.provenance.timestamp


def _load(composition: Any, fragment_id: str) -> MemoryFragment | None:
    arts = composition.artifact_registry.find_by_name("fragment", fragment_id)
    if not arts:
        return None
    return fragment_from_dict(arts[-1].data)


def resolve_streaming_conflicts(
    composition: Any, *, principal: str, now_fn=None
) -> list[dict]:
    """Attach an LWW resolution to every open, unresolved conflict.

    Idempotent: a conflict that already carries a resolution is skipped, so
    re-running across ticks (or after a restart) never re-resolves or spams.
    Returns the resolution records created this pass.
    """
    now = now_fn() if now_fn is not None else datetime.now(UTC).isoformat()
    created: list[dict] = []

    for entry in list_conflicts(composition, principal=principal):
        if entry.get("status") != "open":
            continue
        fragment_ids = list(entry.get("fragment_ids") or [])
        if len(fragment_ids) < 2:
            continue
        name = _pair_key(fragment_ids)
        if composition.artifact_registry.find_by_name(SYNC_RESOLUTION_KIND, name):
            continue  # already resolved

        frags = [f for f in (_load(composition, fid) for fid in fragment_ids) if f]
        if len(frags) < 2:
            continue
        ranked = sorted(frags, key=lambda f: (event_time(f), f.id))
        winner = ranked[-1]
        losers = ranked[:-1]

        record = {
            "principal": principal,
            "conflict_key": name,
            "fragment_ids": sorted(fragment_ids),
            "winner_id": winner.id,
            "winner_event_time": event_time(winner),
            "loser_ids": [f.id for f in losers],
            "loser_event_times": [event_time(f) for f in losers],
            "policy": "lww_by_event_time",
            "resolved_at": now,
        }
        composition.artifact_registry.register(
            kind=SYNC_RESOLUTION_KIND, name=name, data=record,
        )
        composition.audit_log.record(
            entry_type="sync_conflict_resolved",
            principal_id=principal,
            agent_id=_AGENT,
            fragment_id=winner.id,
            outcome="lww_by_event_time",
            losers=",".join(f.id for f in losers),
        )
        created.append(record)

    return created


def list_resolutions(composition: Any, *, principal: str) -> list[dict]:
    """All LWW resolution records for a principal (latest per conflict)."""
    latest: dict[str, dict] = {}
    for artifact in composition.artifact_registry.list(kind=SYNC_RESOLUTION_KIND):
        data = artifact.data or {}
        if data.get("principal") != principal:
            continue
        latest[data.get("conflict_key", artifact.name)] = data
    return list(latest.values())


def loser_fragment_ids(composition: Any, *, principal: str) -> set[str]:
    """Fragment ids that lost an LWW resolution — never propagated outbound."""
    losers: set[str] = set()
    for res in list_resolutions(composition, principal=principal):
        losers.update(res.get("loser_ids") or [])
    return losers


__all__ = [
    "SYNC_RESOLUTION_KIND",
    "event_time",
    "list_resolutions",
    "loser_fragment_ids",
    "resolve_streaming_conflicts",
]
