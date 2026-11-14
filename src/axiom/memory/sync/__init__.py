# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Continuous bidirectional cross-harness sync (ADR-087 D2/D10, PRD F6, P4).

The D2 import primitive applied continuously in both directions, hub-and-spoke
with the Axiom store as the single reconciliation point:

- :mod:`.detect` — per-harness change detection (P2 adapters in watch mode).
- :mod:`.echo` — echo suppression: a fragment we wrote out is never re-imported.
- :mod:`.conflict` — streaming last-writer-wins by event time; loser lands in
  the P2 conflict review queue (reused, never duplicated).
- :mod:`.writeback` — per-product rules-file write-back fallbacks over the P3
  authored-instruction-file layer (AGENTS.md primary), session-boundary cadence.
- :mod:`.engine` — the bidirectional sync engine (inbound import + outbound
  write-back), secret-class routed to vault inbound (OQ6), vault never outbound.
- :mod:`.service` — the managed service block the schedule engine ticks
  (event-driven; LeaseManager single-flight; durable queue + fire-log; recovery
  after downtime with no loss and no echo storm).
- :mod:`.transport` — the A2A transport seam (A3): the send/poll/ack contract
  between two Axiom nodes, an in-process double, and the ``node_transport``
  factory that returns the real wire when present.
- :mod:`.node` — node-to-node sync (A3): the D2 primitive across the
  ``axiom://`` hop, reusing the engine + echo (node-scoped) + conflict queue,
  with a default-deny peer authorizer.
"""

from .conflict import loser_fragment_ids, resolve_streaming_conflicts
from .detect import ChangeDetector, DetectedChange
from .echo import is_echo, record_echo
from .engine import OutboundResult, SyncEngine
from .node import (
    NodeCoordinate,
    NodeSyncEngine,
    NodeSyncService,
    PeerAuthorizer,
    PeerNotAuthorized,
    PushResult,
    ReceiveResult,
)
from .service import (
    SYNC_TICK_ACTION,
    SyncExecutor,
    SyncPeer,
    SyncService,
    SyncTickReport,
)
from .transport import (
    A2AFederationTransport,
    LoopbackTransport,
    NodeSyncMessage,
    NodeTransport,
    node_transport,
)
from .writeback import (
    FALLBACK_TARGETS,
    PRIMARY_TARGET,
    RULES_FILE_TARGETS,
    MultiTargetWriteBack,
)

__all__ = [
    "FALLBACK_TARGETS",
    "PRIMARY_TARGET",
    "RULES_FILE_TARGETS",
    "SYNC_TICK_ACTION",
    "A2AFederationTransport",
    "ChangeDetector",
    "DetectedChange",
    "LoopbackTransport",
    "MultiTargetWriteBack",
    "NodeCoordinate",
    "NodeSyncEngine",
    "NodeSyncMessage",
    "NodeSyncService",
    "NodeTransport",
    "OutboundResult",
    "PeerAuthorizer",
    "PeerNotAuthorized",
    "PushResult",
    "ReceiveResult",
    "SyncEngine",
    "SyncExecutor",
    "SyncPeer",
    "SyncService",
    "SyncTickReport",
    "is_echo",
    "loser_fragment_ids",
    "node_transport",
    "record_echo",
    "resolve_streaming_conflicts",
]
