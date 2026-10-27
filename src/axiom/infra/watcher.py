# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The watcher primitive (ADR-162): one shared cursor / debounce / dedup core.

The poll-and-emit shape had grown four bespoke copies in the release
extension alone, each re-implementing cursor state, debounce, and
content-identity dedup, and each inventing its own output. ADR-162 says
Axiom ships **one** watcher primitive and extensions declare *instances*: a
source (the ``fetch`` callable), a credential, and a subject mapping.

This module is that primitive, kept deliberately small:

- :class:`WatchItem` — one captured thing, keyed on **content identity**
  (``identity``), tagged with a bus-subject ``kind``, carrying an opaque
  ``payload`` and a ``landed`` flag (ADR-162 rule 3: landed vs in-flight is
  first-class, so a consumer can report the two distinctly).
- :class:`Watcher` — owns the three shared behaviours and nothing domain-
  specific: **debounce** (skip a poll that is too soon after the last),
  **content-identity dedup** (a commit seen through both an origin and its
  mirror collapses to one item — ADR-162 rule 2), and **cursor advance**
  (an ``updated_after`` high-water mark so the next poll is a delta).
- :class:`WatcherState` / :class:`FileWatcherStore` — the persisted cursor
  and last-poll time, held through ``axiom.infra.state`` so the CLI and a
  scheduled agent can poll the same instance without racing.

The primitive is pure mechanism: it never reaches the network itself (the
instance's injected ``fetch`` does), never interprets a payload beyond its
``identity``/``updated_at``, and names no vendor or domain. The release
extension's own watchers migrate onto it opportunistically, when next
touched (ADR-162 Consequences); this module does not rewrite them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.state import LockedJsonFile

__all__ = [
    "WatchItem",
    "WatcherState",
    "WatchResult",
    "Watcher",
    "FileWatcherStore",
]


# ---------------------------------------------------------------------------
# Captured item
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchItem:
    """One captured observation, keyed on content identity for dedup.

    ``identity`` is the dedup key — a commit SHA, ``issue:42``, ``mr:7`` —
    and two items with the same identity collapse to one however many
    sources produced them. ``kind`` is the bus subject the program extension
    owns (``scm.push``, ``tracker.issue``, …). ``landed`` separates work that
    has landed from work still in flight (ADR-162 rule 3).
    """

    identity: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    landed: bool = True

    @property
    def updated_at(self) -> str | None:
        """The payload's own ``updated_at``, used to advance the cursor."""
        value = self.payload.get("updated_at")
        return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# Cursor + last-poll state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatcherState:
    """A watcher instance's persisted position.

    ``cursor`` is the ``updated_after`` high-water mark the next poll reads
    from; ``last_polled_at`` is when the instance last actually polled (what
    debounce keys on). Both ISO-8601; ``None`` before the first poll.
    """

    cursor: str | None = None
    last_polled_at: str | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> WatcherState:
        if not isinstance(raw, dict):
            return cls()
        cursor = raw.get("cursor")
        last = raw.get("last_polled_at")
        return cls(
            cursor=cursor if isinstance(cursor, str) and cursor else None,
            last_polled_at=last if isinstance(last, str) and last else None,
        )

    def to_raw(self) -> dict[str, Any]:
        return {"cursor": self.cursor, "last_polled_at": self.last_polled_at}


@dataclass(frozen=True)
class WatchResult:
    """What one :meth:`Watcher.poll` produced.

    ``polled`` is ``False`` when debounce skipped the fetch — distinct from a
    poll that ran and found nothing, so a caller never reads "debounced" as
    "no changes". ``deduped`` counts items collapsed by content identity (the
    origin/mirror double-count the primitive exists to prevent).
    """

    items: list[WatchItem]
    state: WatcherState
    polled: bool
    deduped: int = 0


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _now_iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Watcher:
    """A watcher instance: the shared cursor / debounce / dedup core.

    Construct with the instance-specific bits only — a ``name`` (the cursor
    key), a ``fetch`` callable ``(since: str | None) -> Iterable[WatchItem]``
    that does the actual read, and an optional ``debounce_seconds``. The
    primitive owns everything else.
    """

    def __init__(
        self,
        *,
        name: str,
        fetch: Callable[[str | None], Iterable[WatchItem]],
        debounce_seconds: int = 0,
    ) -> None:
        self.name = name
        self._fetch = fetch
        self.debounce_seconds = max(0, int(debounce_seconds))

    def poll(self, state: WatcherState, *, now: datetime | None = None) -> WatchResult:
        """Run one cursor-delta poll against ``state``.

        Debounce first (skip if too soon), then fetch the delta from the
        cursor, collapse items by content identity (keeping the first seen),
        and advance the cursor to the newest ``updated_at`` observed — or to
        ``now`` when nothing carried one — so the next poll is a true delta.
        """
        now = now or datetime.now(UTC)

        if self.debounce_seconds and state.last_polled_at:
            last = _parse_iso(state.last_polled_at)
            if last is not None and (now - last).total_seconds() < self.debounce_seconds:
                return WatchResult(items=[], state=state, polled=False, deduped=0)

        raw = list(self._fetch(state.cursor))

        seen: dict[str, WatchItem] = {}
        deduped = 0
        for item in raw:
            if item.identity in seen:
                deduped += 1
                continue
            seen[item.identity] = item
        items = list(seen.values())

        new_cursor = self._advance_cursor(state.cursor, items, now)
        new_state = WatcherState(cursor=new_cursor, last_polled_at=_now_iso(now))
        return WatchResult(items=items, state=new_state, polled=True, deduped=deduped)

    @staticmethod
    def _advance_cursor(
        cursor: str | None, items: Iterable[WatchItem], now: datetime
    ) -> str | None:
        """The new high-water mark: the newest ``updated_at`` seen, **never
        behind the prior cursor**.

        An empty delta — or one whose items carry no timestamp — leaves the
        cursor where it was. Advancing to ``now`` instead would silently skip
        anything updated in the window ``(newest-seen, now]`` that the source
        had not yet made visible (clock skew, eventual consistency), which is
        the one direction a cursor must never move. Re-reading from an
        unchanged cursor is cheap and correct: the source returns the same
        empty delta next time."""
        best = _parse_iso(cursor)
        for item in items:
            when = _parse_iso(item.updated_at)
            if when is not None and (best is None or when > best):
                best = when
        return _now_iso(best) if best is not None else cursor


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class FileWatcherStore:
    """Per-instance cursor state under ``<dir>/<name>.json``.

    Every read and write goes through ``LockedJsonFile`` so the operator's
    CLI and the scheduled agent can poll the same instance without clobbering
    each other's cursor.
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)

    def _path(self, name: str) -> Path:
        safe = name.replace("/", "_").replace(":", "_")
        return self._dir / f"{safe}.json"

    def load(self, name: str) -> WatcherState:
        path = self._path(name)
        if not path.exists():
            return WatcherState()
        with LockedJsonFile(path) as f:
            return WatcherState.from_raw(f.read())

    def save(self, name: str, state: WatcherState) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with LockedJsonFile(path, exclusive=True) as f:
            f.write(state.to_raw())
