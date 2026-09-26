# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Hosted-endpoint runtime — two Axiom nodes in harmony (A4, ADR-087 D10).

The runtime on top of A3's node transport. Two capabilities, both gated at the
SOURCE node before anything leaves it, both riding A3's transport seam (the live
A2A wire is OQ-A3-1; built/tested over the in-process doubles exactly as A3):

- :mod:`.borrow` — **query-time foreign_block borrow.** A user's LOCAL node
  contributes a gated + minimized projection of their personal memory as a
  hosted endpoint's ``foreign_block``, at query time, over the transport. The
  hosting node fuses it (P3 serving fusion) but NEVER persists it.
- :mod:`.shard` — **session-shard hosting.** A per-user, principal-isolated,
  TTL working copy on the hosting node. Chat turns append to it; at the session
  boundary it SYNCS HOME to the user's local node via A3's ``NodeSyncEngine``
  (origin-preserving), and the hosting node TTL-expires its copy.
- :mod:`.endpoint` — **the query surface** that borrows, fuses, and asserts the
  hosting store never persists the borrow.

Reuse-maximal: A3 transport/authorization + P3 gate/fusion + P4 cadence/queue,
no duplication. Every memory write goes through ``CompositionService``.
"""

from .borrow import (
    DEFAULT_BORROW_CHAR_BUDGET,
    DEFAULT_BORROW_K,
    A2ABorrowTransport,
    BorrowRequest,
    BorrowResponse,
    BorrowTransport,
    BorrowUnavailable,
    ForeignBlockBorrower,
    LoopbackBorrowTransport,
    borrow_transport,
    minimize_items,
    render_foreign_block,
)
from .endpoint import HostedAnswer, HostedEndpoint, HostedPersistError
from .shard import SHARD_TTL_DEFAULT, SessionShard, SessionShardManager

__all__ = [
    "DEFAULT_BORROW_CHAR_BUDGET",
    "DEFAULT_BORROW_K",
    "SHARD_TTL_DEFAULT",
    "A2ABorrowTransport",
    "BorrowRequest",
    "BorrowResponse",
    "BorrowTransport",
    "BorrowUnavailable",
    "ForeignBlockBorrower",
    "HostedAnswer",
    "HostedEndpoint",
    "HostedPersistError",
    "LoopbackBorrowTransport",
    "SessionShard",
    "SessionShardManager",
    "borrow_transport",
    "minimize_items",
    "render_foreign_block",
]
