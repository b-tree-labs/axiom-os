# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""M-O drift detection for the cached MCP surface.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §10.

Two cooperating pieces live here:

- :func:`check_mcp_surface_drift` — pure function. Given a node root
  and the live extension list, return a :class:`DriftFinding` when the
  cached ``surface.json`` content hash no longer matches the fresh
  hash; ``None`` otherwise. Called by ``hygiene/M-O`` on every
  heartbeat sweep.

- :class:`DriftProposer` — stateful. Wraps the bare drift check with
  spec-§10.2 debounce (require divergence to persist across two
  consecutive heartbeats before proposing) AND the
  ``feedback_raci_automation_escalation`` "3 nos = stop asking"
  escalation. State persists at ``<node_root>/mcp/drift_state.json``
  so the rule survives daemon restarts.

The proposal payload deliberately mirrors the RIVET-style RACI
``Proposal`` shape (previously BURN-E; folded into RIVET 2026-06-01)
rather than coupling to a class that doesn't yet
exist in this worktree (``axiom.agents.raci`` is on the
post-Prague queue per project memory). When a richer RACI substrate
lands the dataclass becomes a thin wrapper over it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


SURFACE_CACHE_PATH = "mcp/surface.json"
DRIFT_STATE_PATH = "mcp/drift_state.json"

# How many divergent heartbeats must accumulate before we propose. The
# spec calls for two consecutive heartbeats; one is treated as transient
# (cache write race, install in flight, etc.).
DEBOUNCE_THRESHOLD = 2

# RACI escalation: three back-to-back denials silence the proposer. The
# silence window matches the RIVET lifecycle precedent (previously BURN-E).
DENIAL_LIMIT = 3
SILENCE_HOURS = 24


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """RACI-style proposal handed to the user when drift fires.

    ``preapproval_pattern`` lets a user accept "always" for any future
    occurrence matching the pattern — see spec §10.3.
    """

    action: str
    description: str
    preapproval_pattern: str = ""


@dataclass(frozen=True)
class DriftFinding:
    """One drift observation. Returned by :func:`check_mcp_surface_drift`."""

    cached_hash: str | None
    fresh_hash: str
    kind: str = "mcp.surface.stale"
    severity: str = "info"
    proposal: Proposal | None = None


@dataclass
class AcceptOutcome:
    """Receipt for :func:`accept_proposal`: the new content hash + cache path."""

    new_hash: str
    cache_path: Path


# ---------------------------------------------------------------------------
# Stateless drift check (spec §10.1)
# ---------------------------------------------------------------------------


def _load_cached_surface(node_root: Path) -> dict | None:
    cache = node_root / SURFACE_CACHE_PATH
    if not cache.exists():
        return None
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — corrupt cache → treat as absent
        log.warning("mcp.drift: cache unreadable (%s): %s", cache, exc)
        return None


def _build_fresh_surface(extensions: Iterable[Any] | None):
    """Build a fresh surface; defer to ``AggregationRegistry`` for the hash."""
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    if extensions is None:
        return AggregationRegistry.from_node().build()
    return AggregationRegistry(extensions=list(extensions)).build()


def check_mcp_surface_drift(
    node_root: Path,
    extensions: Iterable[Any] | None = None,
) -> DriftFinding | None:
    """Return a :class:`DriftFinding` when the cache is stale; ``None`` if clean.

    ``extensions`` is injectable so tests can pin a synthetic set; in
    production the M-O heartbeat passes ``None`` and the registry
    walks discovery itself.
    """
    cached = _load_cached_surface(node_root)
    fresh = _build_fresh_surface(extensions)
    fresh_hash = fresh.content_hash
    cached_hash = cached.get("content_hash") if isinstance(cached, dict) else None

    if cached_hash == fresh_hash:
        return None

    return DriftFinding(
        cached_hash=cached_hash,
        fresh_hash=fresh_hash,
        kind="mcp.surface.stale",
        severity="info",
        proposal=Proposal(
            action="axi mcp regenerate",
            description="MCP surface cache is stale; regen recommended",
            preapproval_pattern="mcp.surface.regen.*",
        ),
    )


# ---------------------------------------------------------------------------
# Stateful proposer (debounce + 3-nos-stop, spec §10.2 + RACI escalation)
# ---------------------------------------------------------------------------


@dataclass
class _DriftState:
    """Persisted state guarding the proposer against noise + spam.

    Stored as JSON at ``<node_root>/mcp/drift_state.json``. The shape
    is intentionally flat so a human can hand-edit it during recovery.
    """

    consecutive_divergences: int = 0
    last_seen_fresh_hash: str = ""
    consecutive_denials: int = 0
    silenced_until: str = ""  # ISO timestamp; empty = not silenced

    @classmethod
    def load(cls, path: Path) -> _DriftState:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        return cls(
            consecutive_divergences=int(data.get("consecutive_divergences", 0)),
            last_seen_fresh_hash=str(data.get("last_seen_fresh_hash", "")),
            consecutive_denials=int(data.get("consecutive_denials", 0)),
            silenced_until=str(data.get("silenced_until", "")),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "consecutive_divergences": self.consecutive_divergences,
                    "last_seen_fresh_hash": self.last_seen_fresh_hash,
                    "consecutive_denials": self.consecutive_denials,
                    "silenced_until": self.silenced_until,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def is_silenced(self, now: datetime) -> bool:
        if not self.silenced_until:
            return False
        try:
            until = datetime.fromisoformat(self.silenced_until)
        except ValueError:
            return False
        return now < until


class DriftProposer:
    """M-O-side drift handler: debounce + escalation on top of the bare check."""

    def __init__(self, node_root: Path) -> None:
        self.node_root = node_root
        self._state_path = node_root / DRIFT_STATE_PATH

    # ------------------------------------------------------------------ public
    def maybe_propose(
        self,
        extensions: Iterable[Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> Proposal | None:
        """Return a proposal iff drift has persisted past debounce AND not silenced.

        Side-effects: bumps ``consecutive_divergences`` on each divergent
        heartbeat; resets to zero on a clean check; clears denial state
        only when the user accepts (caller fires :func:`accept_proposal`).
        """
        now = now or datetime.now(UTC)
        state = _DriftState.load(self._state_path)

        # Silence window — back off entirely until it expires.
        if state.is_silenced(now):
            return None

        finding = check_mcp_surface_drift(self.node_root, extensions=extensions)
        if finding is None:
            # Clean — drop counter; do NOT clear denial state (the user's
            # earlier "no" still counts toward escalation if drift returns).
            if state.consecutive_divergences:
                state.consecutive_divergences = 0
                state.last_seen_fresh_hash = ""
                state.save(self._state_path)
            return None

        # Divergent. Reset counter when the *target* hash changed underneath
        # us — that's a fresh divergence, not the same one persisting.
        if state.last_seen_fresh_hash != finding.fresh_hash:
            state.consecutive_divergences = 0
            state.last_seen_fresh_hash = finding.fresh_hash
        state.consecutive_divergences += 1
        state.save(self._state_path)

        if state.consecutive_divergences < DEBOUNCE_THRESHOLD:
            # Still in the debounce window — observe but stay silent.
            return None
        return finding.proposal

    def record_denial(
        self, _proposal: Proposal, *, now: datetime | None = None
    ) -> None:
        """The user said "no" to a proposal — bump the denial counter.

        After :data:`DENIAL_LIMIT` back-to-back denials the proposer
        goes silent for :data:`SILENCE_HOURS`; the next acceptance (or
        explicit reset) clears the counter.
        """
        now = now or datetime.now(UTC)
        state = _DriftState.load(self._state_path)
        state.consecutive_denials += 1
        if state.consecutive_denials >= DENIAL_LIMIT:
            silence_until = now + timedelta(hours=SILENCE_HOURS)
            state.silenced_until = silence_until.isoformat()
        state.save(self._state_path)

    def reset(self) -> None:
        """Wipe persisted drift state — silence window, denials, debounce."""
        self._state_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Acceptance — regenerate the cache + clear denial counters
# ---------------------------------------------------------------------------


def accept_proposal(
    node_root: Path,
    extensions: Iterable[Any] | None = None,
) -> AcceptOutcome:
    """Apply the proposal: rebuild the surface, rewrite the cache, clear denials.

    Returns the new content hash + cache path so the caller can log /
    surface confirmation. Mirrors what ``axi mcp regenerate`` does so
    automation paths and CLI paths share one persisted shape.
    """
    surface = _build_fresh_surface(extensions)
    cache = node_root / SURFACE_CACHE_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(surface.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )

    state = _DriftState.load(node_root / DRIFT_STATE_PATH)
    state.consecutive_divergences = 0
    state.last_seen_fresh_hash = ""
    state.consecutive_denials = 0
    state.silenced_until = ""
    state.save(node_root / DRIFT_STATE_PATH)

    return AcceptOutcome(new_hash=surface.content_hash, cache_path=cache)


__all__ = [
    "AcceptOutcome",
    "DriftFinding",
    "DriftProposer",
    "Proposal",
    "accept_proposal",
    "check_mcp_surface_drift",
]
