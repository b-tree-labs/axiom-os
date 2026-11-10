# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What makes two rows the same reading, whichever instance of a sender sent it.

A sender updated with no downtime runs two instances side by side for a while
(the old one and the new one, each reading the same source), and each signs
its rows into its own hash chain. The chain fields (``seq``, ``prev_hash``,
``content_hash``) differ between the two for the very same reading, so a
dedupe over the whole row lands every reading twice.

A reading's identity is the row without those fields, with the instance
suffix removed from ``producer_id`` (``daq-x@site#green`` → ``daq-x@site``).
Bronze dedupes on it and realtime consumers drop repeats on it
(:class:`RealtimeDedupe`), so two instances overlapping is invisible
downstream. A row without chain fields (a tabular row from a file, say) hashes
exactly as it always did.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

#: Fields that belong to one instance's chain, not to the reading.
INSTANCE_FIELDS = ("seq", "prev_hash", "content_hash")
#: Separates a producer's stable name from the instance that sent it.
INSTANCE_SEP = "#"


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def stable_producer(producer_id: str) -> str:
    return producer_id.split(INSTANCE_SEP, 1)[0]


def row_identity(row: dict) -> str:
    """The reading's identity: the same for every instance that sent it."""
    r = {k: v for k, v in row.items() if k not in INSTANCE_FIELDS}
    pid = r.get("producer_id")
    if isinstance(pid, str) and INSTANCE_SEP in pid:
        r["producer_id"] = stable_producer(pid)
    return hashlib.sha256(_canonical(r)).hexdigest()


def legacy_row_hash(row: dict) -> str:
    """The whole-row hash bronze used before reading identity; still honoured as seen."""
    return hashlib.sha256(_canonical(row)).hexdigest()


class RealtimeDedupe:
    """A consumer's memory of recent reading keys, so a repeat is dropped.

    Bounded: a key is remembered for the last ``window`` keys, far longer than
    two instances can disagree about the same reading.
    """

    def __init__(self, window: int = 50_000):
        self.window = window
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.dropped = 0

    def first_time(self, key: str) -> bool:
        if key in self._seen:
            self.dropped += 1
            return False
        self._seen[key] = None
        if len(self._seen) > self.window:
            self._seen.popitem(last=False)
        return True


__all__ = ["INSTANCE_FIELDS", "INSTANCE_SEP", "RealtimeDedupe", "legacy_row_hash", "row_identity", "stable_producer"]
