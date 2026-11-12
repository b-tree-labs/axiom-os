# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.show``, ``attest.verify``, ``attest.unacknowledged``, ``attest.cosign_pending`` and
``attest.export``.

``show``, ``verify``, ``unacknowledged`` and ``cosign_pending`` are read-only and exposed over MCP. ``export`` writes
an evidence package to a directory: the records, the public keys, and the
standalone ``verify.py``, so whoever receives it can check it with nothing but
Python (ADR-143).
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path
from typing import Any

from axiom.attest.tools import verify as standalone
from axiom.infra.skills import SkillResult

from .. import filters, intervals, reads, service, signing
from ._common import fail, resolve_site

_brief = service.brief


def _note(
    ctx: Any, what: str, site: str | None, logbook: str | None, *, ids=(), count=None
) -> None:
    """The read this skill made, for its audit record (reads.py; unit X2)."""
    reads.note_access(
        what=what, site=site, logbook=logbook, ids=ids, count=count,
        surface=getattr(ctx, "surface", None),
        person=getattr(getattr(ctx, "principal", None), "handle", None),
    )  # fmt: skip


def show(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """One record by id, or the latest records of a logbook."""
    if params.get("attestation_id"):
        rec = service.record(params["attestation_id"])
        if rec is None:
            return fail(f"no attestation {params['attestation_id']}")
        _note(ctx, "record", rec.get("site_id"), rec.get("logbook"), ids=[rec["attestation_id"]])
        return SkillResult(ok=True, value={"record": rec})
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required (or give an attestation_id)")
    limit = max(1, min(int(params.get("limit") or 20), service.PAGE_LIMIT))
    try:
        wanted = filters.parse_filters(params)
    except ValueError as exc:
        return fail(str(exc))
    # The newest page, read by the database from its indexes; never the chain then a slice.
    records = wanted.apply(params["logbook"], site, limit=limit)
    _note(ctx, "records", site, params["logbook"], count=len(records))
    return SkillResult(
        ok=True,
        value={
            "site_id": site,
            "logbook": params["logbook"],
            "records": service.briefs(records),
        },
    )


def intervals_listing(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Every interval a logbook has run at a site, with its counts and the gaps between them."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    listing = intervals.listing(site, params["logbook"], kind=params.get("kind") or None)
    return SkillResult(ok=True, value={"site_id": site, "logbook": params["logbook"], **listing})


def _latest_anchor(site: str, logbook: str, keys: dict[str, bytes]) -> dict[str, Any] | None:
    """The most recent anchor covering ``logbook``, checked against the store."""
    for a in reversed(service.anchors(site)):
        if any(h["logbook"] == logbook for h in a["heads"]):
            ok, reason = service.verify_site_anchor(a["anchor_id"], keys)
            return {
                "anchor_id": a["anchor_id"],
                "created_at": str(a["created_at"]),
                "root": a["root"],
                "ok": ok,
                "reason": reason,
            }
    return None


def verify(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Walk a logbook's chain and name the first broken record."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    keys = signing.public_keys()
    report = service.verify_logbook(site, params["logbook"], keys)
    _note(ctx, "verify", site, params["logbook"], count=report.checked)
    value = {
        "site_id": site,
        "logbook": params["logbook"],
        "ok": report.ok,
        "checked": report.checked,
        "head_seq": report.head_seq,
        "head_digest": report.head_digest,
        "first_bad_seq": report.first_bad_seq,
        "reason": report.reason,
        "anchor": _latest_anchor(site, params["logbook"], keys),
    }
    anchor = value["anchor"]
    if report.ok and anchor and not anchor["ok"]:
        return SkillResult(
            ok=False,
            value=value,
            errors=[
                f"chain verifies but anchor {anchor['anchor_id']} does not: {anchor['reason']}"
            ],
        )
    if report.ok:
        return SkillResult(ok=True, value=value)
    return SkillResult(
        ok=False,
        value=value,
        errors=[f"chain broken at seq {report.first_bad_seq}: {report.reason}"],
    )


def export(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Write an evidence package for one logbook to ``out``."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook") or not params.get("out"):
        return fail("logbook and out are required")
    out = Path(params["out"])
    if out.exists() and any(out.iterdir()):
        return fail(f"{out} is not empty; export into a new directory")
    records = service.records(site, params["logbook"])
    if not records:
        return fail(f"logbook {params['logbook']!r} has no records at {site}")
    _note(ctx, "export", site, params["logbook"], count=len(records))
    keys = signing.public_keys()
    out.mkdir(parents=True, exist_ok=True)
    (out / "records.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )
    (out / "keys.json").write_text(
        json.dumps({k: base64.b64encode(v).decode("ascii") for k, v in keys.items()}, indent=2),
        encoding="utf-8",
    )
    shutil.copyfile(standalone.__file__, out / "verify.py")
    head = records[-1]
    manifest = {
        "site_id": site,
        "logbook": params["logbook"],
        "records": len(records),
        "head_seq": head["seq"],
        "head_digest": head["digest"],
        "verify": "python verify.py records.jsonl --keys keys.json",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return SkillResult(
        ok=True,
        value={**manifest, "out": str(out)},
        actions_taken=[f"wrote evidence package for {params['logbook']} to {out}"],
    )


def unacknowledged(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The entries of a log that nobody, or a named person, has acknowledged (unit L3)."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    by = params.get("by") or None
    if by == "me":
        by = getattr(getattr(ctx, "principal", None), "handle", None)
        if not by:
            return fail("no person to read for")
    since = params.get("since_seq")
    rows = service.unacknowledged(
        site, params["logbook"], by=by, since_seq=int(since) if since is not None else None
    )
    return SkillResult(
        ok=True,
        value={"site_id": site, "logbook": params["logbook"], "by": by, "records": rows},
    )


def cosign_pending(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The entries of a log waiting for a co-signature (unit L4)."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    roles = params.get("for_roles") or None
    if isinstance(roles, str):
        roles = [r.strip() for r in roles.split(",") if r.strip()]
    older = params.get("older_than")
    rows = service.pending_cosigns(
        site, params["logbook"], for_roles=roles,
        older_than=int(older) if older is not None else None,
    )  # fmt: skip
    return SkillResult(
        ok=True, value={"site_id": site, "logbook": params["logbook"], "records": rows}
    )
