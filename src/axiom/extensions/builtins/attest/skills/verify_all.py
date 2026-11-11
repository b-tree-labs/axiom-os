# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.verify_all``: every chain at every served site from sequence one (unit X2).

Run weekly by the manifest's ``[[extension.schedule]]`` entry and on demand with
``axi attest verify --all``. It records each result in ``verified_heads.json``, so the
heartbeat's incremental check continues from a full verification, and names the first break."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import integrity, reads


def verify_all(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    state_dir = getattr(ctx, "state_dir", None)
    if state_dir is None:
        from axiom.infra.paths import get_user_state_dir

        state_dir = get_user_state_dir()
    try:
        report = integrity.verify_all(state_dir)
    except Exception as exc:  # noqa: BLE001 - "not checked" is the honest answer
        return SkillResult(
            ok=False, value=None, errors=[f"attestation chains were not checked: {exc}"]
        )
    for fact in report["logbooks"]:
        reads.note_access(what="verify", site=fact["site"], logbook=fact["logbook"],
                          count=fact["checked"], surface=getattr(ctx, "surface", None),
                          person=getattr(getattr(ctx, "principal", None), "handle", None))  # fmt: skip
    if report["ok"]:
        return SkillResult(
            ok=True,
            value=report,
            actions_taken=[f"verified {len(report['logbooks'])} chain(s) from sequence 1"],
        )
    b = report["first_break"]
    where = (
        f"chain breaks at sequence {b['first_bad_seq']} ({b['reason']})"
        if b["first_bad_seq"] is not None
        else f"anchor {b['anchor_id']} does not verify ({b['anchor_reason']})"
    )
    return SkillResult(
        ok=False,
        value=report,
        errors=[f"logbook {b['logbook']} at {b['site']}: {where}"],
    )
