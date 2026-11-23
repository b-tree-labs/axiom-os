# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Verify one chain, whole or a slice (F1 design note, question 6).

A slice from ``from_seq`` starts after the stored digest of record ``from_seq - 1``, as an
anchor does: it proves what is new, and trusts what came before. The whole walk is the call
with no bounds.
"""

from __future__ import annotations

from typing import Any

from axiom.attest.chain import GENESIS, VerifyReport, erased, tombstone_covers, verify_chain
from axiom.extensions.builtins.attest import service, signing

from ._site import site_or_refuse

__all__ = ["VerifyReport", "chain"]


def chain(
    site: str,
    logbook: str,
    from_seq: int | None = None,
    to_seq: int | None = None,
    *,
    public_keys: dict[str, bytes] | None = None,
) -> VerifyReport:
    start = 1 if from_seq is None else from_seq
    if start < 1 or (to_seq is not None and to_seq < start):
        raise ValueError(f"no slice from {from_seq} to {to_seq}")
    site_id = site_or_refuse(site)
    keys = signing.public_keys() if public_keys is None else public_keys
    # The slice and the one record before it, bounded in the query: never the chain from the
    # slice's start to its head (LS-1, #1780).
    rows = service.records(site_id, logbook, from_seq=max(start - 1, 1), to_seq=to_seq)
    prev = GENESIS
    if start > 1:
        if not rows or rows[0]["seq"] != start - 1:
            return VerifyReport(False, 0, None, None, start - 1, "seq_gap")
        prev, rows = rows[0]["digest"], rows[1:]
    covered = _later_tombstones(site_id, logbook, rows, to_seq, keys)
    # A changed byte of an attachment fails the entry whose signed bytes name its hash (LD-4).
    return service.with_attachments(
        verify_chain(rows, keys, start_seq=start, start_prev=prev, covered=covered), rows)


def _later_tombstones(site_id: str, logbook: str, rows: list[dict[str, Any]], to_seq: int | None,
                      keys: dict[str, bytes]) -> dict[str, dict[int, tuple[str, str]]]:  # fmt: skip
    """The tombstones past the slice that cover its content-less records (unit LD-6), each
    verified on its own (its digest and node signature) before it is trusted."""
    if to_seq is None:
        return {}
    out: dict[str, dict[int, tuple[str, str]]] = {}
    for tid in {t for t in (erased(r) for r in rows) if t}:
        rec = service.record(tid)
        if (not rec or rec.get("kind") != "tombstone" or rec.get("site_id") != site_id
                or rec.get("logbook") != logbook or int(rec.get("seq", 0)) <= to_seq):  # fmt: skip
            continue
        alone = verify_chain([rec], keys, start_seq=int(rec["seq"]), start_prev=rec["prev_digest"])
        if alone.ok:
            out[tid] = tombstone_covers(rec)
    return out
