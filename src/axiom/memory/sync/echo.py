# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Echo index — a fragment we wrote out is recognized on read-back (ADR-087 D2).

Sync is hub-and-spoke: Axiom writes a peer's memory into that peer's
instruction file, and the peer's own change detector then reads that file back.
The idempotency key must recognize *our own words* and never re-import them,
or two harnesses would ping-pong a fragment forever (an echo storm).

Two complementary mechanisms give that guarantee:

- **Marker strip** (``detect.strip_managed_block``) — the primary path for the
  markdown write-back layer: our managed block is removed before a source
  change is derived, so it is never a candidate at all.
- **This content-hash echo index** — the general, durable belt-and-suspenders:
  every fragment text we write out is recorded (``sync_echo`` artifacts, keyed
  by content hash) so any candidate whose text hashes to one of ours is
  suppressed even when it arrives outside our markers.

Records ride the artifact registry beside the fragments they describe — the
same durable-record posture P2 dedup uses for the conflict queue.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

SYNC_ECHO_KIND = "sync_echo"

_AGENT = "axi-memory"


def echo_hash(text: str, *, node: str = "") -> str:
    """Content hash of one written-out fragment text — the echo key.

    ``node`` (A3) scopes the key to the peer node the text was synced to, so a
    fragment we pushed across the ``axiom://`` boundary is recognised only on
    the returning direction from *that* node — never confused with independent
    content of the same text from elsewhere. ``node=""`` reproduces the P4
    content-only key byte-for-byte (the harness-to-harness hub-and-spoke case),
    so existing callers are unaffected: this extends the key, it does not
    rewrite it.
    """
    payload = f"{node}\x00{text or ''}" if node else (text or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_echo(
    composition: Any,
    *,
    principal: str,
    target: str,
    fragment_id: str,
    text: str,
    node: str = "",
) -> str | None:
    """Record that we wrote ``text`` (fragment ``fragment_id``) into ``target``.

    Idempotent per content hash: re-writing the same text never grows the
    index. Returns the artifact id, or ``None`` when already recorded. ``node``
    (A3) scopes the key to the peer node the fragment was synced to (see
    :func:`echo_hash`).
    """
    key = echo_hash(text, node=node)
    if composition.artifact_registry.find_by_name(SYNC_ECHO_KIND, key):
        return None
    artifact_id = composition.artifact_registry.register(
        kind=SYNC_ECHO_KIND,
        name=key,
        data={
            "principal": principal,
            "target": target,
            "fragment_id": fragment_id,
            "content_hash": key,
            "node": node,
            "written_at": datetime.now(UTC).isoformat(),
        },
    )
    composition.audit_log.record(
        entry_type="sync_echo_recorded",
        principal_id=principal,
        agent_id=_AGENT,
        fragment_id=fragment_id,
        outcome="ok",
        target=target,
    )
    return artifact_id


def is_echo(composition: Any, *, text: str, node: str = "") -> bool:
    """True when ``text`` is something Axiom itself wrote out (suppress it).

    ``node`` (A3) scopes the check to the peer the text is arriving *from*, so
    a fragment we pushed to that node is recognised when it echoes back. Default
    ``node=""`` is the P4 content-only check.
    """
    return bool(
        composition.artifact_registry.find_by_name(
            SYNC_ECHO_KIND, echo_hash(text, node=node)
        )
    )


__all__ = ["SYNC_ECHO_KIND", "echo_hash", "is_echo", "record_echo"]
