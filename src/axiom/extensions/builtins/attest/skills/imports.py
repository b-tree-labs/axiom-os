# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Importing history onto a log's chain (#1804, ADR-262).

- ``attest.import`` (CLI only): ``log L import FILE`` is a dry run; ``--commit DIGEST`` writes,
  asked at the prompt, for a person holding the importer role for the log.
- ``attest.import_preview`` (also MCP and agent tools): a dry run of text it is handed. Software
  prepares; it never commits.
- ``attest.import_withdraw`` (CLI only): withdraws a whole batch with a reason, at the prompt.
- ``attest.imports``: the log's import batches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillResult

from .. import imports
from ..service import AttestRefused
from ._common import fail, resolve_site


def _yes(answer: str) -> bool:
    return (answer or "").strip().lower() in ("y", "yes")


def run(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """A dry run of FILE, or with ``commit`` (the dry run's digest) the import itself."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    logbook = params.get("logbook")
    path = params.get("path")
    if not logbook or not path:
        return fail("give the log and the file to import")
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        return fail(f"cannot read {path}: {exc.strerror or exc}")
    source = params.get("source")
    digest = params.get("commit")
    try:
        if not digest:
            out = imports.dry_run(site, logbook, content, source=source)
            return SkillResult(ok=True, value=out)
        if ctx is None or getattr(ctx, "user_prompt", None) is None:
            return fail("an import is committed by a person at the prompt; nothing was written")
        dry = imports.dry_run(site, logbook, content, source=source)
        if dry["digest"] != digest:
            return fail(f"the file's digest is {dry['digest']}, not {digest}: it changed since its "
                        "dry run; nothing was written")  # fmt: skip
        c = dry["counts"]
        answer = ctx.user_prompt(
            f"Import {c['new']} entries into {logbook} as {ctx.principal.handle}? They are placed "
            f"on the chain as imported, not signed. {c['skipped']} already there, "
            f"{c['warnings']} warning(s). yes or no? "
        )
        if not _yes(answer):
            return SkillResult(ok=True, value={**dry, "status": "not_imported"})
        out = imports.commit(site, logbook, content, digest=digest, by=ctx.principal.handle,
                             source=source, via="cli", state_dir=Path(ctx.state_dir))  # fmt: skip
    except AttestRefused as exc:
        return fail(f"not imported: {exc}")
    return SkillResult(ok=True, value=out,
                       actions_taken=[f"imported {out['written']} entries into {logbook}"])  # fmt: skip


def preview(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """A dry run of mapped history handed as text. Writes nothing."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    try:
        out = imports.dry_run(site, params["logbook"], str(params.get("content") or "").encode("utf-8"),
                              source=params.get("source"))  # fmt: skip
    except AttestRefused as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value=out)


def withdraw(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if ctx is None or getattr(ctx, "user_prompt", None) is None:
        return fail("a batch is withdrawn by a person at the prompt; nothing was withdrawn")
    batch, logbook = params.get("batch"), params.get("logbook")
    answer = ctx.user_prompt(
        f"Withdraw import batch {batch} from {logbook}? Its entries stay on the chain, marked "
        "withdrawn, and can be imported again. yes or no? "
    )
    if not _yes(answer):
        return SkillResult(ok=True, value={"batch": batch, "status": "not_withdrawn"})
    try:
        out = imports.withdraw(site, logbook, batch, reason=params.get("reason") or "",
                               by=ctx.principal.handle, state_dir=Path(ctx.state_dir))  # fmt: skip
    except AttestRefused as exc:
        return fail(f"not withdrawn: {exc}")
    return SkillResult(ok=True, value=out, actions_taken=[f"withdrew import batch {batch}"])


def list_batches(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    return SkillResult(ok=True, value={"site_id": site, "logbook": params["logbook"],
                                       "batches": imports.batches(site, params["logbook"])})  # fmt: skip
