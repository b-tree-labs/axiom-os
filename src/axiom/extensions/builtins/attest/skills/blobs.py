# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Operator verbs over the attachment blob store (unit LD-4).

``attest.blobs`` lists a site's blob prefix (hash and size; never bytes, never a location) and
writes a read record, because a listing of hashes is still a read. ``attest.blobs_check`` is the
restore validation: every hash a record names resolves to a blob of that hash, and every blob is
named by a record. Neither is a door: no record's content is served, and both are CLI only.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import attachments, reads
from ._common import fail, resolve_site


def list_blobs(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    try:
        got = attachments.list_blobs(site)
    except Exception as exc:  # noqa: BLE001 - the store's answer, said plainly
        return fail(f"the blob store could not be listed: {exc}")
    reads.note_access(what="blobs", site=site, logbook=None, ids=(), count=len(got),
                      surface=getattr(ctx, "surface", None),
                      person=getattr(getattr(ctx, "principal", None), "handle", None))  # fmt: skip
    return SkillResult(ok=True, value={"site_id": site, "blobs": got, "count": len(got)})


def check(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site = params.get("site") or None
    try:
        findings = attachments.restore_findings(site)
    except Exception as exc:  # noqa: BLE001 - "not checked" is the honest answer
        return SkillResult(ok=False, value=None, errors=[f"attachments were not checked: {exc}"])
    value = {"findings": [f.__dict__ for f in findings], "count": len(findings)}
    if findings:
        return SkillResult(ok=False, value=value, errors=[f.line() for f in findings])
    return SkillResult(ok=True, value=value)


__all__ = ["check", "list_blobs"]
