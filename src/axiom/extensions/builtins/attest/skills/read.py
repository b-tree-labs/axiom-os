# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.show``, ``attest.verify``, ``attest.unacknowledged``, ``attest.cosign_pending``,
``attest.seal_preview`` and ``attest.export``.

``show``, ``verify``, ``unacknowledged``, ``cosign_pending`` and ``seal_preview`` are read-only and
exposed over MCP. ``export`` writes
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
from ..logbooks import LogbookError
from ..service import AttestRefused
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
        # What it authorised and what it is about (unit X4).
        links = service.links(rec["attestation_id"], state_dir=getattr(ctx, "state_dir", None))
        from .. import changes

        # Change since the previous signed value, read now, after corrections (unit L7).
        return SkillResult(
            ok=True, value={"record": rec, "links": links, "changes": changes.for_record(rec)}
        )
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
    # The newest page by when entries happened (an import placed by its own time, #1804), read
    # by the database from its indexes; never the chain then a slice.
    records = wanted.apply(params["logbook"], site, limit=limit, order="occurred")
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


_EXPORT_PARAMS = ("site", "logbook", "out", "format", "formats", "form", "forms", "json")


def _listed(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def _export_package(site: str, params: dict[str, Any], ctx: Any) -> SkillResult:
    """``attest.export`` with formats or filters (unit L6): build, store and record a package and,
    with ``out``, write its files there."""
    import io
    import zipfile

    from .. import exports

    try:
        f = exports.parse_filter(params, extra=_EXPORT_PARAMS)
        formats = _listed(params.get("format") or params.get("formats")) or (
            [] if params.get("form") or params.get("forms") else ["jsonl"]
        )
        forms = _listed(params.get("form") or params.get("forms"))
        out = Path(params["out"]) if params.get("out") else None
        if out is not None and out.exists() and any(out.iterdir()):
            return fail(f"{out} is not empty; export into a new directory")
        who = getattr(getattr(ctx, "principal", None), "handle", None) or "@cli:local"
        row, pkg = exports.create(site, params["logbook"], f, formats=formats, templates=forms,
                                  requested_by=who)  # fmt: skip
    except (exports.ExportRefused, AttestRefused) as exc:
        return fail(str(exc))
    _note(ctx, "export", site, params["logbook"], count=pkg.selected)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(pkg.data)) as z:
            z.extractall(out)
    value = {**row, "head": pkg.manifest["head"], "counts": pkg.manifest["counts"],
             "files": sorted(pkg.manifest["files"]), "out": str(out) if out else None,
             "verify": pkg.manifest["verify"]}  # fmt: skip
    return SkillResult(ok=True, value=value,
                       actions_taken=[f"wrote evidence package {row['export_id']} for {params['logbook']}"])  # fmt: skip


def _wants_package(params: dict[str, Any]) -> bool:
    return any(params.get(k) not in (None, "", [], False) for k in params if k not in ("site", "logbook", "out"))


def export(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Write an evidence package for one logbook to ``out``; with formats or filters, the L6
    package (the whole chain, the filtered views, the verification report)."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    if _wants_package(params):
        return _export_package(site, params, ctx)
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


def export_preview(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """At most 200 of an export's selected records, rendered in memory (unit L6). Writes nothing."""
    from .. import exports

    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    try:
        f = exports.parse_filter(params, extra=_EXPORT_PARAMS)
        value = exports.preview(site, params["logbook"], f, fmt=str(params.get("format") or "text"))
    except (exports.ExportRefused, AttestRefused) as exc:
        return fail(str(exc))
    _note(ctx, "export", site, params["logbook"], count=value["records"])
    return SkillResult(ok=True, value=value)


def sealed_and_packaged(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Sealed and packaged (#1814): each log close-out's anchor and each evidence package, from
    the action ledger, newest first; the receipts console's one read of them."""
    from .. import receipts

    limit = max(1, min(int((params or {}).get("limit") or 50), 200))
    return SkillResult(ok=True, value={"rows": receipts.listed(limit=limit,
                                                               state_dir=getattr(ctx, "state_dir", None))})  # fmt: skip


def export_blank(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """A declared form printed blank, with no records, for a backup binder (#1805). Reads no
    entry of the log, so it is not recorded as a read; writes nothing."""
    from .. import exports, registry

    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook") or not params.get("form"):
        return fail("logbook and form are required")
    try:
        book = registry.get(params["logbook"])
        text = exports.render_blank(site, book, str(params["form"])).decode("utf-8")
        name = exports.template_filename(site, book, str(params["form"]))
        if params.get("out"):
            # The same PDF the web and an automation print (#1805), to a new file; only at the
            # command line, where the file is the person's own.
            if getattr(ctx, "surface", None) not in ("cli", None):
                return fail("a PDF file is written at the command line; elsewhere read the text")
            out = Path(str(params["out"]))
            if out.exists():
                return fail(f"{out} already exists; name a new file")
            out.write_bytes(exports.render_blank_pdf(site, book, str(params["form"])))
            return SkillResult(ok=True, value={"site_id": site, "logbook": book.id,
                                               "form": params["form"], "out": str(out),
                                               "records": 0, "verify": "a blank form; nothing to verify"},
                               actions_taken=[f"wrote the blank {params['form']} form to {out}"])  # fmt: skip
    except (exports.ExportRefused, LogbookError) as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value={"site_id": site, "logbook": book.id, "form": params["form"],
                                       "filename": name, "text": text, "records": 0,
                                       "selected": 0, "truncated": False})  # fmt: skip


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


def seal_due(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The clock periods of a log that ended and are not closed (#1806), for the person asking;
    empty for anyone who may not seal the log."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    viewer = getattr(getattr(ctx, "principal", None), "handle", None)
    if not viewer:
        return fail("no person to read for")
    try:
        due = service.unsealed_periods(site, params["logbook"], viewer=viewer,
                                       state_dir=getattr(ctx, "state_dir", None))  # fmt: skip
    except (AttestRefused, LogbookError) as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value={"site_id": site, "logbook": params["logbook"], "periods": due})


def seal_preview(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """What a close-out of a log's period would state: the range, the chain head and the open
    items, computed and not stored (unit L5)."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    try:
        value = service.seal_preview(
            site, params["logbook"], boundary=params.get("boundary") or None,
            from_seq=params.get("from_seq"), to_seq=params.get("to_seq"),
        )  # fmt: skip
    except AttestRefused as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value={"site_id": site, "logbook": params["logbook"], **value})
