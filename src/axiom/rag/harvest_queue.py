# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The durable hand-off between "a record was written" and "it is retrievable".

Harvesting cannot happen inside the transaction that writes the record. Two
reasons, and both are fatal rather than awkward:

The write must not depend on retrieval being healthy. Embedding calls an
external service; if a save fails because an embedding endpoint is slow, the
product has traded a working database for a broken one.

And the record is not real until it commits. Rendering at flush time and writing
to the corpus would publish rows that a rollback then erased — the corpus would
answer questions from data the database does not have, which is worse than not
answering at all.

So intent is recorded durably at commit and acted on afterwards. This file is
that record.

It is an append-only JSONL log through ``locked_append_jsonl`` — the primitive
this codebase already uses for exactly this class of work, including RAG's own
ingest checkpoints — rather than a new table. Appending is lock-held only for
the write, so a busy writer never blocks on a slow drain, and it needs no
migration and no opinion about which backend the store runs on.

Entries are deduplicated when the queue is read, not when it is written. A
record touched five times in a burst should be harvested once, at its final
state, and deciding that at read time keeps the write path to a single append.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

OP_UPSERT = "upsert"
OP_DELETE = "delete"


@dataclass(frozen=True)
class HarvestIntent:
    """One record's pending harvest, as written at commit time."""

    entity_type: str
    entity_id: str
    op: str
    corpus: str
    owner: str | None = None
    enqueued_at: str = ""
    #: The rendered card, captured at flush rather than rebuilt at drain.
    #: Rendering is string formatting and costs nothing in the write path;
    #: embedding is the expensive part and stays in the drain. Capturing here
    #: also means the card is exactly the state that committed, and it removes
    #: the need to reload the record later — which matters most for a delete,
    #: where by drain time there is nothing left to load.
    title: str = ""
    text: str = ""

    def key(self) -> tuple[str, str, str]:
        """Identity for deduplication — type, id and corpus, not the op.

        Keyed without the op on purpose: an upsert followed by a delete must
        collapse to the delete, not to two conflicting instructions.
        """
        return (self.entity_type, self.entity_id, self.corpus)


def enqueue(queue_path: str | Path, intent: HarvestIntent) -> None:
    """Record one pending harvest. Never raises into the caller.

    The caller is a database commit that has already succeeded. Failing it now
    would roll back real work because a retrieval side-effect could not be
    queued — so a queue failure is logged loudly and the write stands.
    """
    from axiom.infra.state import locked_append_jsonl

    payload = asdict(intent)
    if not payload.get("enqueued_at"):
        payload["enqueued_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        locked_append_jsonl(queue_path, payload)
    except Exception:  # noqa: BLE001 - a committed write must not be undone
        log.error(
            "could not queue %s/%s for harvest: it will not become retrievable "
            "until the record is written again or a backfill runs. The database "
            "write itself succeeded and stands.",
            intent.entity_type, intent.entity_id, exc_info=True,
        )


def read_pending(queue_path: str | Path) -> list[HarvestIntent]:
    """Every queued intent, deduplicated to the last one per record.

    A malformed line is skipped and reported rather than aborting the drain: one
    bad append must not strand every other record behind it. Silence would be
    worse than either — a queue that quietly drops work looks exactly like a
    queue with no work.
    """
    path = Path(queue_path)
    if not path.exists():
        return []

    latest: dict[tuple[str, str, str], HarvestIntent] = {}
    skipped = 0
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            intent = HarvestIntent(**data)
        except (json.JSONDecodeError, TypeError) as exc:
            skipped += 1
            log.warning(
                "harvest queue %s line %d is unreadable and was skipped (%s)",
                path, line_number, exc,
            )
            continue
        latest[intent.key()] = intent

    if skipped:
        log.error(
            "%d unreadable entries in %s were skipped; those records will not "
            "become retrievable until they are written again", skipped, path,
        )
    return list(latest.values())


def clear(queue_path: str | Path) -> None:
    """Drop the queue after a successful drain.

    Called only once every intent read has been acted on. Truncating before
    acting would lose the work on a crash mid-drain, which is the failure the
    queue exists to prevent.
    """
    path = Path(queue_path)
    if path.exists():
        path.write_text("", encoding="utf-8")


__all__ = [
    "OP_DELETE",
    "OP_UPSERT",
    "HarvestIntent",
    "clear",
    "enqueue",
    "read_pending",
]
