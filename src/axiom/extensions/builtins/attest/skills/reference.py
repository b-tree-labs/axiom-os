# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.reference``: one signed record as of gold, for a memory reference's pointer (LD-3).

The door a reference is re-resolved through when chat, MCP or the CLI follows it. The one check
has already admitted the caller (``doors.admitted_skill``) and the read is recorded there; this
reads the gold view and answers the record as of the gold watermark with its deep link, or the
record's tombstone when gold no longer holds its content. Memory recall reaches the same door
outside a skill (:mod:`..references`).
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import references
from ._common import fail


def run(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    aid = str(params.get("attestation_id") or "").strip()
    if not aid:
        return fail("attestation_id is required: the record a reference points at")
    row = references._gold_row(attestation_id=aid)
    if row is None:
        return fail("not found")
    pointer = params.get("ref")
    if pointer and pointer != f"axiom://{references.SCHEME}/sha256:{row['digest']}":
        return fail("the pointer does not name this record")
    return SkillResult(ok=True, value=references.served(row))


__all__ = ["run"]
