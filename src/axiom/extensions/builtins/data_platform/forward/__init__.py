# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Forward what a node landed to an upstream node: the ``share-upstream`` function.

A node that keeps its own medallion (a partner's local archive) records every
landed batch in its ingest outbox, the same outbox an ingest edge keeps
(ADR-177). The forwarder reads that outbox in order and delivers each batch
upstream, choosing the transport itself so nobody edits a config at cutover:

1. the upstream **intake** (``POST /ingest/rows``) when it is healthy;
2. otherwise an **object-store drop** (today: a Box folder through rclone, with
   the operator's own login), which the upstream node collects;
3. otherwise it **waits**. The outbox and the local medallion hold everything.

Switching uses hysteresis: ``up_after`` consecutive healthy probes to move to
the intake, ``down_after`` consecutive failures to leave it. Every switch is
logged and kept in the status file with its reason.

**Exactly once.** The cursor (the last outbox ``seq`` delivered) advances only
after the intake answered 200 or the drop is confirmed by its hash. Both
transports carry the same request body, and the upstream node lands either one
through the same row sink, which deduplicates by content hash: a batch that
travels both ways, or twice, lands once.

**Sharing policy.** A partner decides what is shared. ``policy.allows(record,
rows)`` returns the rows to send (possibly none) and ``policy.hold_until(record)``
returns a time before which the batch waits. A batch the policy excludes moves
the cursor without sending and is counted. Stopping the forwarder only stops
sending; it never deletes anything upstream.
"""

from __future__ import annotations

from .forwarder import (
    BoxDropTarget,
    Forwarder,
    ForwardReport,
    IntakeTarget,
    LocalOutbox,
    ShareEverything,
    SharePolicy,
    request_body,
)

__all__ = [
    "BoxDropTarget",
    "ForwardReport",
    "Forwarder",
    "IntakeTarget",
    "LocalOutbox",
    "ShareEverything",
    "SharePolicy",
    "request_body",
]
