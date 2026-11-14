# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Verify one chain, whole or a slice (F1 design note, question 6).

A slice from ``from_seq`` starts after the stored digest of record ``from_seq - 1``, as an
anchor does: it proves what is new, and trusts what came before. The whole walk is the call
with no bounds.
"""

from __future__ import annotations

from axiom.attest.chain import GENESIS, VerifyReport, verify_chain
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
    rows = service.records(site_id, logbook, from_seq=max(start - 1, 1))
    prev = GENESIS
    if start > 1:
        if not rows or rows[0]["seq"] != start - 1:
            return VerifyReport(False, 0, None, None, start - 1, "seq_gap")
        prev, rows = rows[0]["digest"], rows[1:]
    if to_seq is not None:
        rows = [r for r in rows if r["seq"] <= to_seq]
    return verify_chain(rows, keys, start_seq=start, start_prev=prev)
