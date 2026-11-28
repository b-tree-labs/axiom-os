# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routine archiving and tombstoning by a log's declared rule, run by the schedule (ADR-290 D8).

For each sealed period of a log, the driver does what the declaration says is due and nothing else:

- past ``live_days``: package the period, verify the stored package, and commit the archive switch
  (reads of the period then come from the package; reversible with ``log archive rollback``);
- past ``delete_after_days`` (never inside the platform's retention floor, which :func:`retention.effective`
  applies) and archived: record the tombstone and commit it, so every read hides the period's content
  (reversible with ``log retention rollback``).

The acts are signed as the human the site names as accountable for the scheduler, or by the scheduler
itself when none is named (:mod:`.accountability`). **Erasing stays a named approver's act**: the driver
never destroys content. A log under a hold is archived and never tombstoned, an act already done is not
done again, and one period's refusal is reported without stopping the rest.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select, text

from . import archives, registry, retention, service, store
from .db_models import AttestRecord, AttestTombstone

log = logging.getLogger(__name__)

__all__ = [
    "BUDGET_ENV",
    "DRIVER_KIND",
    "log_store_bytes",
    "pressure",
    "relieve_pressure",
    "run_all",
    "run_due",
    "sealed_logs",
    "tombstone_unarchived",
]

#: The log store's declared budget in bytes. Unset: nothing is ever under pressure, nothing is erased.
BUDGET_ENV = "AXIOM_LOG_BUDGET_BYTES"

#: The scheduler is a machine in ADR-290 D8's sense.
DRIVER_KIND = "machine"


def _due(when: str | None, now: datetime) -> bool:
    return bool(when) and datetime.fromisoformat(when) <= now


def run_due(site: str, logbook: str, *, actor: str, actor_kind: str = DRIVER_KIND,
            now: datetime | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """Archive and tombstone what is due in one log at one site; one entry per act taken or refused."""
    now = now or retention._now()  # noqa: SLF001 - the one clock retention's tests move
    book = registry.get(logbook)
    if book.retention_rule is None:
        return []
    acts: list[dict[str, Any]] = []
    for period in retention.report(site, logbook)["periods"]:
        base = {"site_id": site, "logbook": logbook, "seal_id": period["seal_id"]}
        if period["tombstone_id"]:
            continue  # tombstoned already; erasing is a person's act
        status = period["archive_status"]
        if status not in retention.ARCHIVED and _due(period["archive_due_at"], now):
            try:
                status = _archive(site, logbook, period, actor=actor)
                acts.append({**base, "act": "archived", "archive_id": period["archive_id"]})
            except Exception as exc:  # noqa: BLE001 - one period's refusal never stops the rest
                acts.append({**base, "act": "refused", "step": "archive", "reason": str(exc)})
                continue
        if status in retention.ARCHIVED and _due(period["delete_due_at"], now):
            acts.append(
                {
                    **base,
                    **_tombstone(site, logbook, book, period, actor=actor, actor_kind=actor_kind),
                }
            )
    return acts


def _archive(site: str, logbook: str, period: dict[str, Any], *, actor: str) -> str:
    """Package (when not yet), verify (when not yet) and commit; the archive's status after."""
    archive_id = period["archive_id"]
    if archive_id is None:
        archive_id = archives.build(site, logbook, by=actor, seal_id=period["seal_id"])[
            "archive_id"
        ]
        period["archive_id"] = archive_id
    current = archives.get(archive_id)
    if current["status"] == "packaged":
        current = archives.verify(archive_id, by=actor)
    if current["status"] not in retention.ARCHIVED:
        current = archives.commit(archive_id, owner=actor)
    return current["status"]


def _tombstone(site: str, logbook: str, book: Any, period: dict[str, Any], *, actor: str,
               actor_kind: str) -> dict[str, Any]:  # fmt: skip
    reason = retention.held(site, book)
    if reason:
        return {
            "act": "refused",
            "step": "tombstone",
            "reason": f"{logbook} is under a hold ({reason})",
        }
    try:
        record = service.record_tombstone(site_id=site, logbook=logbook, archive_id=period["archive_id"],
                                          actor=actor, actor_kind=actor_kind)  # fmt: skip
        retention.commit(record["attestation_id"], owner=actor)
    except Exception as exc:  # noqa: BLE001 - reported, never raised past the period
        return {"act": "refused", "step": "tombstone", "reason": str(exc)}
    return {"act": "tombstoned", "record": record}


def sealed_logs() -> list[tuple[str, str]]:
    """Every (site, log) with at least one seal: the only logs the driver can have anything to do in."""
    stmt = (
        select(AttestRecord.site_id, AttestRecord.logbook)
        .where(AttestRecord.kind == "seal")
        .distinct()
    )
    with store.session_scope() as s:
        return [(r.site_id, r.logbook) for r in s.execute(stmt)]


def run_all(*, actor_for: Any = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Run :func:`run_due` for every sealed log the registry still declares. ``actor_for(site)`` names
    the scheduler at a site; by default ``@scheduler:<site>``."""
    acts: list[dict[str, Any]] = []
    for site, logbook in sealed_logs():
        try:
            registry.get(logbook)
        except Exception:  # noqa: BLE001 - a log no longer declared has no rule to act on
            continue
        actor = actor_for(site) if actor_for else f"@scheduler:{site}"
        try:
            acts += run_due(site, logbook, actor=actor, now=now)
        except Exception as exc:  # noqa: BLE001 - one log's failure never stops the others
            log.warning("archival driver: %s at %s: %s", logbook, site, exc)
            acts.append({"site_id": site, "logbook": logbook, "act": "refused", "step": "report",
                         "reason": str(exc)})  # fmt: skip
    return acts


# -- erasing under storage pressure (the owner's rule, 2026-11-25) --------------------------------


def log_store_bytes() -> int:
    """What the logs hold in the database: every ``attest_`` table with its indexes and TOAST. Zero
    where the database cannot say (a test store)."""
    sql = text(
        "SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND c.relname LIKE 'attest\\_%' AND n.nspname = current_schema()"
    )
    try:
        with store.session_scope() as s:
            return int(s.execute(sql).scalar() or 0)
    except Exception:  # noqa: BLE001 - a store that cannot measure itself is never under pressure
        return 0


def pressure() -> str | None:
    """Why the log store is under pressure, or None. Only a declared budget makes pressure."""
    raw = os.environ.get(BUDGET_ENV, "").strip()
    if not raw:
        return None
    budget, used = int(float(raw)), log_store_bytes()
    if used <= budget:
        return None
    return f"the logs hold {used // 2**20} MiB, over their budget of {budget // 2**20} MiB"


def tombstone_unarchived(site: str, logbook: str, seal_id: str, *, actor: str) -> dict[str, Any]:
    """Tombstone and hide a sealed period that was never archived, past its declared rule. The tombstone
    says there was no archive. Only the storage-pressure pass calls this."""
    record = service.record_tombstone(site_id=site, logbook=logbook, archive_id=None, seal_id=seal_id,
                                      actor=actor, actor_kind=DRIVER_KIND)  # fmt: skip
    retention.commit(record["attestation_id"], owner=actor)
    return record


def _erase(tombstone_id: str, *, actor: str, why: str) -> None:
    """Erase now: under pressure the soak is overridden, and the override records the pressure."""
    retention.erase(
        tombstone_id,
        approver=actor,
        approver_kind=DRIVER_KIND,
        override_soak=f"storage pressure: {why}",
    )


def _committed() -> list[AttestTombstone]:
    with store.session_scope() as s:
        rows = s.execute(select(AttestTombstone).where(AttestTombstone.status == "committed")
                         .order_by(AttestTombstone.created_at)).scalars()  # fmt: skip
        return [r for r in rows]


def _due_untombstoned(now: datetime) -> list[tuple[str, str, dict[str, Any]]]:
    """Sealed periods past their deletion date with no tombstone, oldest seal first, across every log."""
    out = []
    for site, logbook in sealed_logs():
        try:
            book = registry.get(logbook)
        except Exception:  # noqa: BLE001 - a log no longer declared has no rule to act on
            continue
        if book.retention_rule is None:
            continue
        for p in retention.report(site, logbook)["periods"]:
            if not p["tombstone_id"] and _due(p["delete_due_at"], now):
                out.append((site, logbook, p))
    return sorted(out, key=lambda item: item[2]["sealed_at"] or "")


def relieve_pressure(*, actor: str | None = None, actor_for: Any = None,
                     now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """Erase, only while the log store is over its declared budget, only what is past its declared rule
    and outside any hold: first the periods already tombstoned, oldest first, then the due periods with
    no tombstone (an archived one by its archive, a never-archived one with a tombstone that says so).
    Stops as soon as the pressure clears; reports when it cannot clear it."""
    reason = pressure()
    if reason is None:
        return {"pressure": None, "acts": [], "still_over": False}
    now = now or retention._now()  # noqa: SLF001
    who = (lambda site: actor) if actor else (actor_for or (lambda site: f"@scheduler:{site}"))
    acts: list[dict[str, Any]] = []
    cleared = False
    for t in _committed():
        base = {"site_id": t.site_id, "logbook": t.logbook, "seal_id": t.seal_id}
        try:
            _erase(t.tombstone_id, actor=who(t.site_id), why=reason)
            acts.append({**base, "act": "erased", "tombstone_id": t.tombstone_id})
        except Exception as exc:  # noqa: BLE001 - a held or no-longer-due period is skipped, said so
            acts.append({**base, "act": "refused", "step": "erase", "reason": str(exc)})
            continue
        if pressure() is None:
            cleared = True
            break
    if not cleared:
        for site, logbook, p in _due_untombstoned(now):
            base = {"site_id": site, "logbook": logbook, "seal_id": p["seal_id"]}
            actor_here = who(site)
            try:
                book = registry.get(logbook)
                held = retention.held(site, book)
                if held:
                    raise service.AttestRefused(f"{logbook} is under a hold ({held})")
                if p["archive_status"] in retention.ARCHIVED:
                    record = service.record_tombstone(site_id=site, logbook=logbook, archive_id=p["archive_id"],
                                                      actor=actor_here, actor_kind=DRIVER_KIND)  # fmt: skip
                    retention.commit(record["attestation_id"], owner=actor_here)
                else:
                    record = tombstone_unarchived(site, logbook, p["seal_id"], actor=actor_here)
                _erase(record["attestation_id"], actor=actor_here, why=reason)
                acts.append({**base, "act": "erased", "tombstone_id": record["attestation_id"]})
            except Exception as exc:  # noqa: BLE001 - reported, never raised past the period
                acts.append({**base, "act": "refused", "step": "erase", "reason": str(exc)})
                continue
            if pressure() is None:
                cleared = True
                break
    still = None if cleared else pressure()
    if still:
        log.warning("archival: %s, and nothing more is past its declared rule", still)
    return {"pressure": reason, "acts": acts, "still_over": bool(still)}
