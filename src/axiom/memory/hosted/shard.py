# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session-shard hosting — transient copy on the host, durable home local (A4).

A per-user session shard lives on the HOSTING node: owned by the user,
principal-isolated, a TTL working copy. Chat turns append to it (via
:class:`~axiom.memory.composition.CompositionService`, origin-stamped as the
hosting node) for continuity. At the session boundary the shard SYNCS HOME to
the user's local node via A3's :class:`~axiom.memory.sync.node.NodeSyncEngine`,
and the hosting node TTL-expires its copy. The local node is the durable home;
the hosting node holds only a transient copy.

Every leg reuses shipped primitives:

- **Append** rides the one door in (``CompositionService.write``), origin-stamped
  ``axiom://<hosting-node>`` with the user's account, so the shard is
  self-describing as a hosting-session working copy and lands in the user's own
  account domain for the sync-home gate.
- **Sync-home** is A3, unchanged: a per-user ``NodeSyncEngine.push_to(home)``
  runs the fragment through the serving gate (vault / secret / cross-account /
  tier never cross) + LWW filter, over A3's ``NodeTransport``; the home node's
  import is origin-preserving (harness ``axiom://<hosting-node>``) and
  echo-suppressed. Nothing about the D2 primitive is re-implemented.
- **Expiry** past TTL forgets the hosting copy via ``CompositionService.forget``
  (tombstone), leaving the durable home copy — a *different store* — untouched.

The TTL is driven by an injectable clock (``now_fn``) — no wall-clock in logic,
so tests are deterministic.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from axiom.memory.fragment import SourceOrigin
from axiom.memory.sync.engine import SyncEngine
from axiom.memory.sync.node import (
    NodeCoordinate,
    NodeSyncEngine,
    NodeSyncService,  # noqa: F401 (re-exported convenience parity)
    PeerAuthorizer,
    PushResult,
)
from axiom.memory.sync.transport import NodeTransport

SHARD_TTL_DEFAULT = 3600  # seconds — one hour working copy by default
_AGENT = "axi"


# ---------------------------------------------------------------------------
# The shard descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionShard:
    """A principal-scoped, TTL'd working copy on the hosting node."""

    principal: str
    account: str
    session_id: str
    hosting_node_id: str
    created_at: datetime
    ttl_seconds: int = SHARD_TTL_DEFAULT

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.ttl_seconds)

    def is_expired(self, now: datetime) -> bool:
        """True at or past the expiry instant (boundary counts as expired)."""
        return now >= self.expires_at


# ---------------------------------------------------------------------------
# The hosting-node shard manager
# ---------------------------------------------------------------------------


@dataclass
class SessionShardManager:
    """Owns the hosting node's transient session shards.

    One instance serves every hosted user; each user's shard is reconciled home
    through its own single-principal :class:`NodeSyncEngine` (A3 is inherently
    single-principal), so users never blend.
    """

    node: NodeCoordinate
    composition: Any
    transport: NodeTransport
    authorizer: PeerAuthorizer
    now_fn: Callable[[], datetime]
    accountable_human_id: str | None = None
    ttl_seconds: int = SHARD_TTL_DEFAULT

    _shards: dict[tuple[str, str], SessionShard] = field(default_factory=dict)
    _fragment_ids: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    # ---- lifecycle ---------------------------------------------------------

    def open(
        self,
        *,
        principal: str,
        account: str,
        session_id: str,
        ttl_seconds: int | None = None,
    ) -> SessionShard:
        """Open (or return) a principal-scoped shard for a hosting session."""
        key = (principal, session_id)
        existing = self._shards.get(key)
        if existing is not None:
            return existing
        shard = SessionShard(
            principal=principal,
            account=account,
            session_id=session_id,
            hosting_node_id=self.node.node_id,
            created_at=self.now_fn(),
            ttl_seconds=self.ttl_seconds if ttl_seconds is None else ttl_seconds,
        )
        self._shards[key] = shard
        self._fragment_ids.setdefault(key, [])
        return shard

    def append_turn(
        self,
        *,
        principal: str,
        account: str,
        session_id: str,
        text: str,
        cognitive_type: str = "episodic",
    ):
        """Append one chat turn to the shard (the one door in).

        Origin-stamped ``axiom://<hosting-node>`` with the user's account and a
        content-stable ``source_ref`` — the fragment records that it was authored
        in this hosting session, and lands in the user's account domain so the
        sync-home gate treats it as the user's own memory.
        """
        key = (principal, session_id)
        if key not in self._shards:
            self.open(principal=principal, account=account, session_id=session_id)
        now_iso = self.now_fn().isoformat()
        origin = SourceOrigin(
            harness=self.node.uri,
            account=account,
            source_ref="turn-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            imported_at=now_iso,
        )
        content = {"summary": text}
        # Episodic turns are session events — carry the event time (the store
        # requires it, and recall time-filtering keys on it).
        if cognitive_type == "episodic":
            content["event_time"] = now_iso
        frag = self.composition.write(
            content=content,
            cognitive_type=cognitive_type,
            principal_id=principal,
            agents={_AGENT},
            resources=set(),
            accountable_human_id=self.accountable_human_id or principal,
            session_id=session_id,
            origin=origin,
        )
        self._fragment_ids.setdefault(key, []).append(frag.id)
        return frag

    def sync_home(
        self, *, principal: str, account: str, home: NodeCoordinate
    ) -> PushResult:
        """Session boundary: push the shard HOME via A3's node sync (gated + LWW)."""
        return self._engine_for(principal, account).push_to(home)

    def expire(
        self,
        *,
        principal: str,
        session_id: str,
        now: datetime | None = None,
    ) -> list[str]:
        """Drop the hosting node's transient copy once past TTL.

        A no-op before expiry (the working copy is still live). Past TTL, the
        shard's fragments are forgotten (tombstoned) on the hosting node; the
        durable home copy — a different store — is never touched. Returns the
        forgotten fragment ids.
        """
        key = (principal, session_id)
        shard = self._shards.get(key)
        if shard is None:
            return []
        moment = now if now is not None else self.now_fn()
        if not shard.is_expired(moment):
            return []
        ids = self._live_fragment_ids(key)
        if ids:
            self.composition.forget(
                ids, requester=principal, agent=_AGENT, reason="session_shard_ttl",
            )
        self._shards.pop(key, None)
        self._fragment_ids.pop(key, None)
        return ids

    # ---- introspection -----------------------------------------------------

    def fragment_ids(self, principal: str, session_id: str) -> list[str]:
        return list(self._fragment_ids.get((principal, session_id), []))

    # ---- internals ---------------------------------------------------------

    def _engine_for(self, principal: str, account: str) -> NodeSyncEngine:
        """A single-principal A3 sync engine for this hosted user's shard."""
        iso = lambda: self.now_fn().isoformat()  # noqa: E731
        engine = SyncEngine(
            composition=self.composition,
            principal=principal,
            account_set=frozenset({account}),
            now_fn=iso,
        )
        local = NodeCoordinate(
            node_id=self.node.node_id,
            account=account,
            deployment_tier=self.node.deployment_tier,
        )
        return NodeSyncEngine(
            engine=engine,
            local_node=local,
            transport=self.transport,
            authorizer=self.authorizer,
            now_fn=iso,
        )

    def _live_fragment_ids(self, key: tuple[str, str]) -> list[str]:
        """Tracked shard ids still live in the store (skip any already gone)."""
        live: list[str] = []
        for fid in self._fragment_ids.get(key, []):
            if self.composition.artifact_registry.find_by_name("fragment", fid):
                live.append(fid)
        return live


__all__ = [
    "SHARD_TTL_DEFAULT",
    "SessionShard",
    "SessionShardManager",
]
