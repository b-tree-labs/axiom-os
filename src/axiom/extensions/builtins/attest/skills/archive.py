# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A sealed period archived as a verifiable package (unit LD-5, ADR-251 D7).

- ``attest.archives``: a log's archived periods, metadata only (a door, by the one check).
- ``attest.archive`` (CLI only): package the latest seal (or ``seal_id``), store it, verify it
  offline and commit the switch, so reads of the period come from the package. For a person who
  may close out the log.
- ``attest.archive_retire`` (CLI only): the retire, a person's explicit act through the switch
  runner, naming the package digest it retires.
- ``attest.seal_schedule`` (CLI only): put each log's sealing on PULSE at its declared cadence.
- ``attest.rehydrate``: an archived period read again from its package through the log's door,
  recorded, and its records searchable in full until the warm window ends (unit LD-8).
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import archives, registry
from ..logbooks import LogbookError
from ..service import AttestRefused
from ._common import fail, resolve_site


def _who(ctx: Any) -> str | None:
    handle = getattr(getattr(ctx, "principal", None), "handle", None)
    return str(handle) if handle else None


def _may_close(handle: str, site: str, logbook: str, ctx: Any) -> str | None:
    """None when ``handle`` may close out (and so archive) ``logbook`` at ``site``."""
    from ..roles import roles_for

    try:
        book = registry.get(logbook)
    except LogbookError as exc:
        return str(exc)
    if book.seal is None:
        return f"{logbook} declares no seal, so it has no sealed period to archive"
    if not set(roles_for(handle, site, state_dir=getattr(ctx, "state_dir", None))) & set(book.seal.by_roles):
        return f"archiving {logbook} needs a role that may close it out ({', '.join(book.seal.by_roles)})"
    return None


def listing(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    return SkillResult(ok=True, value={"site_id": site, "logbook": params["logbook"],
                                       "archives": archives.listed(site, params["logbook"])})  # fmt: skip


def rehydrate(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """An archived period from its package, through this door (unit LD-8). An archive of another
    log or site is not named here: it answers as one that does not exist."""
    from .. import summary_index

    site, err = resolve_site(params)
    if err:
        return fail(err)
    logbook, archive_id = params.get("logbook"), str(params.get("archive_id") or "").strip()
    if not logbook or not archive_id:
        return fail("logbook and archive_id are required")
    if summary_index.period_of(site, logbook, archive_id) is None:
        return fail(f"no archived period {archive_id} in {logbook} at {site}")
    try:
        return SkillResult(ok=True, value=summary_index.rehydrate(archive_id))
    except (archives.ArchiveRefused, OSError) as exc:
        return fail(str(exc))


def archive(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    logbook, who = params.get("logbook"), _who(ctx)
    if not logbook:
        return fail("logbook is required")
    if not who:
        return fail("no person to archive as")
    why = _may_close(who, site, logbook, ctx)
    if why:
        return fail(why)
    try:
        built = archives.build(site, logbook, by=who, seal_id=params.get("seal_id") or None,
                               target_name=params.get("target") or "default")  # fmt: skip
        done = archives.commit(built["archive_id"], owner=who)
    except AttestRefused as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - the runner's refusal (no gate, busy) says why
        return fail(f"{type(exc).__name__}: {exc}")
    return SkillResult(ok=True, value=done, actions_taken=[
        f"archived #{done['from_seq']} to #{done['last_seq']} of {logbook} as package {done['package_digest']}; "
        "reads of the period come from it. Retire the hot copy with attest.archive_retire once the soak passes."])


def retire(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    who = _who(ctx)
    archive_id, digest = params.get("archive_id"), params.get("package_digest")
    if not who:
        return fail("no person to retire as; a retire is a person's explicit act")
    if not archive_id or not digest:
        return fail("name the archive_id and the package_digest you are retiring")
    try:
        a = archives.get(archive_id)
    except AttestRefused as exc:
        return fail(str(exc))
    if digest != a["package_digest"]:
        return fail("package_digest is not this archive's package; nothing was retired")
    why = _may_close(who, a["site_id"], a["logbook"], ctx)
    if why:
        return fail(why)
    try:
        done = archives.retire(archive_id, approver=who, override_soak=params.get("override_soak") or None)
    except AttestRefused as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - the runner's refusal says why
        return fail(f"{type(exc).__name__}: {exc}")
    return SkillResult(ok=True, value=done, actions_taken=[f"retired the hot copy of archive {archive_id}"])


def schedule(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    logbook = params.get("logbook")
    try:
        got = archives.schedule_seals(site, [logbook] if logbook else None)
    except (ValueError, LogbookError) as exc:  # a refusal, or a site with no time zone
        return fail(str(exc))
    return SkillResult(ok=True, value=got)


__all__ = ["archive", "listing", "retire", "schedule"]
