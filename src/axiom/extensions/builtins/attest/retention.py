# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A log's declared retention, enforced with signed tombstones (unit LD-6, ADR-251 D7).

The record is never compacted and never edited. A sealed period is archived (unit LD-5); where,
and only where, the log's declaration allows it, the period's content is later removed, and the
removal is itself a record: a **tombstone**, signed by a person in the role the declaration names
for it, that names the sealed period, the archive, the rule and every record, digest and blob
that goes. Then the chain still verifies: a covered record keeps its digest, link, signatures and
labels, and the verifier accepts it without its content because a later tombstone names it.

- **The rule** (:func:`effective`): ``retention = { live_days, archive, class, delete_after_days,
  tombstone_by }``. ``class`` is a platform retention class whose floor nothing shortens
  (``axiom.governance.retention``); a site may lengthen ``delete_after_days``
  (``retention.<log>.delete_after_days``), and a value at or below the floor is reported as the
  floor and never applied. A log under a hold (class ``legal_hold``, or the site setting
  ``retention.<log>.hold``) deletes nothing. A settings source that cannot answer is never read
  as "not held": nothing is deleted.
- **The report** (:func:`report`): declared, effective and next due, per log: what the CLI's
  ``log <log> retention`` and the Log page read, through a door.
- **The dry run** (:func:`preview`): everything a tombstone of one sealed, archived period would
  remove, named, with nothing removed.
- **The tombstone** (``service.create_tombstone_draft`` for a person at the prompt;
  ``service.record_tombstone`` as an act, ADR-290 D8): a person acting for themselves needs a
  declared ``tombstone_by`` role (administration roles hold no authority in a log, ADR-142 rule 8);
  an agent or machine acts by the declared rule alone and is signed for by the accountable human
  where one is traced. The scheduled driver (``archival``) records and commits the tombstones that
  fall due; erasing stays a named approver's act.
- **The erasure** is a switch on the one runner (ADR-197, :func:`plan`): ``commit`` hides the
  period's content from every read (reversible: ``rollback`` shows it again); ``retire`` (a named
  approver who holds the tombstone role, a change record, the rule checked again) writes the
  deletion to the action chain first, then removes the content from the chain rows, their drafts,
  presentations and answers (the database allows exactly that change and nothing else), their
  index rows, field values and attachment index rows, every blob no live record still names, the
  archived package, the memory fragments that point at them and the journal's words for them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import registry, store
from .db_models import AttestArchive, AttestRecord, AttestTombstone
from .logbooks import PLATFORM_ADMIN_ROLES, LogbookError
from .service import AttestRefused

log = logging.getLogger("axiom.attest.retention")

KIND = "attest.tombstone"
#: Archive statuses whose period counts as sealed and archived (LD-6-A7).
ARCHIVED = ("archived", "retired")
#: Tombstone statuses whose period is hidden from reads or gone.
HIDING = ("committed", "erased")


class RetentionRefused(AttestRefused):
    """A retention step that is not allowed; the message says why. Nothing was deleted."""


def _now() -> datetime:
    """The clock the rule is judged by (a test moves it instead of waiting a year)."""
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _when(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# -- the rule ------------------------------------------------------------------------------------


def _setting(site: str, key: str) -> Any:
    """A site setting, or a refusal when the source cannot answer: a policy error never reads as
    "not held" or "no lengthening"."""
    from . import site_settings

    try:
        return site_settings.value(site, key, None)
    except Exception as exc:  # noqa: BLE001 - fail closed, say why
        raise RetentionRefused(f"cannot tell whether the site lengthens or holds this log's retention "
                               f"({type(exc).__name__}); nothing is deleted") from None  # fmt: skip


def held(site: str, book: Any) -> str | None:
    """Why ``book`` is held at ``site`` (nothing is deleted while it is), or None."""
    rule = book.retention_rule
    if rule is not None and rule.retention_class == "legal_hold":
        return "the log's retention class is legal_hold"
    hold = _setting(site, f"retention.{book.id}.hold")
    if hold:
        return f"a hold is set for this log at {site}: {hold}"
    return None


def effective(site: str, book: Any) -> dict[str, Any]:
    """The rule in force at ``site``: the declaration, its class's floor, and the site's
    lengthening when it lengthens (a value below the floor is reported and never applied)."""
    from axiom.governance import retention as gov

    rule = book.retention_rule
    if rule is None:
        return {"class": None, "floor_days": None, "live_days": None, "archive": None, "deletes": False,
                "delete_after_days": None, "declared_delete_after_days": None, "site_setting_days": None,
                "site_setting_applied": False, "tombstone_by": [], "held": None}  # fmt: skip
    cls = gov.RetentionClass.from_str(rule.retention_class)
    floor = gov.policy_for(site).floor(cls)
    floor_days = floor.days if floor is not None else None
    asked = _setting(site, f"retention.{book.id}.delete_after_days")
    try:
        site_days = int(asked) if asked is not None and not isinstance(asked, bool) else None
    except (TypeError, ValueError):
        site_days = None
    days = None
    applied = False
    if rule.deletes and floor_days is not None:
        days = max(int(rule.delete_after_days), floor_days)
        if site_days is not None and site_days > days:
            days, applied = site_days, True
    return {
        "class": cls.value, "floor_days": floor_days, "live_days": rule.live_days, "archive": rule.archive,
        "deletes": days is not None, "delete_after_days": days,
        "declared_delete_after_days": rule.delete_after_days, "site_setting_days": site_days,
        "site_setting_applied": applied, "tombstone_by": list(rule.tombstone_by), "held": held(site, book),
    }  # fmt: skip


# -- the report (LD-6-A6) ------------------------------------------------------------------------


def _seals(site: str, logbook: str) -> list[dict[str, Any]]:
    from . import service

    with store.session_scope() as s:
        return service._seal_records(s, site, logbook)  # noqa: SLF001 - the log reading its own chain


def _archives_by_seal(site: str, logbook: str) -> dict[str, AttestArchive]:
    from . import archives

    with store.session_scope() as s:
        if not archives._table_exists(s):  # noqa: SLF001
            return {}
        rows = list(s.execute(select(AttestArchive).where(
            AttestArchive.site_id == site, AttestArchive.logbook == logbook)).scalars())  # fmt: skip
        for r in rows:
            s.expunge(r)
    return {r.seal_id: r for r in rows}


def _tombstones_by_seal(site: str, logbook: str) -> dict[str, AttestTombstone]:
    with store.session_scope() as s:
        if not _table_exists(s):
            return {}
        rows = list(s.execute(select(AttestTombstone).where(
            AttestTombstone.site_id == site, AttestTombstone.logbook == logbook)).scalars())  # fmt: skip
        for r in rows:
            s.expunge(r)
    return {r.seal_id: r for r in rows}


def _table_exists(s: Any) -> bool:
    from sqlalchemy import text

    try:
        return bool(s.execute(text("SELECT to_regclass('attest_tombstones') IS NOT NULL")).scalar())
    except Exception:  # noqa: BLE001 - before migration 0019: no tombstones
        return False


def report(site: str, logbook: str) -> dict[str, Any]:
    """Declared, effective and next due for one log at one site; each sealed period with its
    archive and tombstone state and the dates its archive and its deletion fall due."""
    book = registry.get(logbook)
    try:
        eff = effective(site, book)
        policy_error = None
    except RetentionRefused as exc:
        eff, policy_error = effective_unknown(book), str(exc)
    archived = _archives_by_seal(site, logbook)
    stones = _tombstones_by_seal(site, logbook)
    periods = []
    for rec in _seals(site, logbook):
        covers = ((rec.get("content") or {}).get("seal") or {}).get("covers") or {}
        sealed_at = _when(rec["recorded_at"])
        a, t = archived.get(rec["attestation_id"]), stones.get(rec["attestation_id"])
        periods.append({
            "seal_id": rec["attestation_id"], "from_seq": covers.get("from_seq"), "to_seq": covers.get("to_seq"),
            "sealed_at": _iso(sealed_at), "archive_id": a.archive_id if a else None,
            "archive_status": a.status if a else None,
            "archive_due_at": _iso(sealed_at + timedelta(days=eff["live_days"])) if eff["live_days"] else None,
            "delete_due_at": _iso(sealed_at + timedelta(days=eff["delete_after_days"]))
            if eff["delete_after_days"] else None,
            "tombstone_id": t.tombstone_id if t and t.status != "rolled_back" else None,
            "tombstone_status": t.status if t else None,
        })  # fmt: skip
    next_archive = next((p for p in periods if p["archive_due_at"] and p["archive_status"] not in
                         (*ARCHIVED, "tombstoned")), None)  # fmt: skip
    next_delete = next((p for p in periods if p["delete_due_at"] and p["tombstone_status"] != "erased"), None)
    return {
        "site_id": site, "logbook": logbook, "declared": book.retention, "effective": eff,
        "policy_error": policy_error, "periods": periods,
        "next_due": {
            "archive": None if next_archive is None else {
                "seal_id": next_archive["seal_id"], "due_at": next_archive["archive_due_at"]},
            "delete": None if next_delete is None else {
                "seal_id": next_delete["seal_id"], "archive_id": next_delete["archive_id"],
                "due_at": next_delete["delete_due_at"]},
        },
    }  # fmt: skip


def effective_unknown(book: Any) -> dict[str, Any]:
    rule = book.retention_rule
    return {"class": rule.retention_class if rule else None, "floor_days": None,
            "live_days": rule.live_days if rule else None, "archive": rule.archive if rule else None,
            "deletes": False, "delete_after_days": None,
            "declared_delete_after_days": rule.delete_after_days if rule else None,
            "site_setting_days": None, "site_setting_applied": False,
            "tombstone_by": list(rule.tombstone_by) if rule else [], "held": "unknown"}  # fmt: skip


# -- the dry run ---------------------------------------------------------------------------------


def _covered(site: str, logbook: str, lo: int, hi: int) -> list[dict[str, Any]]:
    """The chain's own rows of the period, read by the log that owns them."""
    with store.session_scope() as s:
        return [dict(r.record) for r in s.execute(
            select(AttestRecord).where(AttestRecord.site_id == site, AttestRecord.logbook == logbook,
                                       AttestRecord.seq >= lo, AttestRecord.seq <= hi)
            .order_by(AttestRecord.seq)).scalars()]  # fmt: skip


def _named_elsewhere(site: str, sha: str, outside: set[str]) -> bool:
    """Whether a live record at ``site`` (any log, outside ``outside``) still names the blob."""
    probe = {"evidence": {"attachments": [{"sha256": sha}]}}
    with store.session_scope() as s:
        ids = s.execute(select(AttestRecord.attestation_id).where(
            AttestRecord.site_id == site, AttestRecord.record.contains(probe))).scalars()  # fmt: skip
        return any(aid not in outside for aid in ids)


def _counts(ids: list[str]) -> dict[str, int]:
    from sqlalchemy import func, or_

    from . import index
    from .db_models import AttestAttachmentIndex, AttestFieldValue

    with store.session_scope() as s:
        index.ensure(s)
        fields = s.execute(select(func.count()).select_from(AttestFieldValue).where(or_(
            AttestFieldValue.attestation_id.in_(ids), AttestFieldValue.source_attestation_id.in_(ids)))).scalar()  # fmt: skip
        extracted = s.execute(select(func.count()).select_from(AttestAttachmentIndex).where(
            AttestAttachmentIndex.attestation_id.in_(ids))).scalar()  # fmt: skip
        return {"index_rows": index.count_records(s, ids), "field_values": int(fields or 0),
                "attachment_index_rows": int(extracted or 0)}  # fmt: skip


#: What a tombstone with no archive says in place of one (erased under storage pressure, ADR-290 D8).
UNARCHIVED = "no archive: erased under storage pressure past the declared rule"


def preview(site: str, logbook: str, *, archive_id: str | None = None,
            seal_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Everything a tombstone of one sealed, archived period would remove, named; nothing is
    removed. Refused (nothing deleted, ever) without a declared rule that allows deletion, under a
    hold, before the rule is due, for a period that is not sealed and archived, or twice."""
    from . import archives

    try:
        book = registry.get(logbook)
    except LogbookError as exc:
        raise RetentionRefused(str(exc)) from None
    if book.retention_rule is None:
        raise RetentionRefused(f"{logbook} declares no retention rule; nothing is ever deleted from it")
    eff = effective(site, book)
    if not eff["deletes"]:
        raise RetentionRefused(f"{logbook}'s retention rule does not allow deletion (no delete_after_days "
                               "beyond a time floor); nothing is deleted")  # fmt: skip
    if eff["held"]:
        raise RetentionRefused(f"{logbook} is under a hold ({eff['held']}); nothing is deleted")
    if seal_id is not None:
        return _preview_period(site, logbook, book, eff, seal_id, None, {"archive_id": None, "reason": UNARCHIVED},
                               None)  # fmt: skip
    if archive_id is None:
        due = report(site, logbook)["next_due"]["delete"]
        if not due or not due.get("archive_id"):
            raise RetentionRefused(f"no sealed and archived period of {logbook} is due for deletion")
        archive_id = due["archive_id"]
    try:
        a = archives.get(archive_id)
    except AttestRefused:
        raise RetentionRefused(f"no archive {archive_id}; only a sealed and archived period is named") from None
    if (a["site_id"], a["logbook"]) != (site, logbook):
        raise RetentionRefused(f"archive {archive_id} is not of {logbook} at {site}")
    if a["status"] not in ARCHIVED:
        raise RetentionRefused(f"a tombstone names only a sealed and archived period; archive {archive_id} is "
                               f"{a['status']} (commit the archive first)")  # fmt: skip
    if not archives.verified(archives._get(archive_id)):  # noqa: SLF001
        raise RetentionRefused(f"archive {archive_id} has no stored verify report for its package")
    archive = {"archive_id": archive_id, "package_digest": a["package_digest"]}
    package = {"archive_id": archive_id, "package_digest": a["package_digest"], "storage_class": a["storage_class"]}
    return _preview_period(site, logbook, book, eff, a["seal_id"], archive_id, archive, package)


def _preview_period(site: str, logbook: str, book: Any, eff: dict[str, Any], seal_id: str,
                    archive_id: str | None, archive: dict[str, Any], package: dict[str, Any] | None
                    ) -> dict[str, Any]:  # fmt: skip
    """The dry run of one sealed period, archived (``archive_id``) or not (erased under pressure)."""
    from axiom.memory import references as mem_refs

    from . import attachments

    t = _tombstones_by_seal(site, logbook).get(seal_id)
    if t is not None and t.status != "rolled_back":
        raise RetentionRefused(f"the period sealed by {seal_id} already has tombstone {t.tombstone_id}")
    seal = next((r for r in _seals(site, logbook) if r["attestation_id"] == seal_id), None)
    if seal is None:
        raise RetentionRefused(f"no seal {seal_id} of {logbook}" if archive_id is None
                               else f"archive {archive_id} names no seal of {logbook}")  # fmt: skip
    covers = ((seal.get("content") or {}).get("seal") or {}).get("covers") or {}
    lo, hi = int(covers["from_seq"]), int(covers["to_seq"])
    sealed_at = _when(seal["recorded_at"])
    due_at = sealed_at + timedelta(days=int(eff["delete_after_days"]))
    if _now() < due_at:
        raise RetentionRefused(f"#{lo} to #{hi} of {logbook} is not due for deletion until {_iso(due_at)} "
                               f"({eff['delete_after_days']} days after it was sealed); nothing is deleted")  # fmt: skip
    recs = _covered(site, logbook, lo, hi)
    if len(recs) != hi - lo + 1:
        raise RetentionRefused(f"#{lo} to #{hi} of {logbook} is not whole in the chain")
    if any("content" not in r for r in recs):
        raise RetentionRefused(f"#{lo} to #{hi} of {logbook} already lost content; nothing more is removed")
    ids = [r["attestation_id"] for r in recs]
    shas = sorted({str(ref.get("sha256")) for r in recs for ref in attachments.refs_of(r)})
    kept = [h for h in shas if _named_elsewhere(site, h, set(ids))]
    return {
        "site_id": site, "logbook": logbook,
        "seal": {"attestation_id": seal["attestation_id"], "seq": int(seal["seq"]), "digest": seal["digest"]},
        "archive": archive,
        "package": package,
        "covers": {"from_seq": lo, "to_seq": hi},
        "rule": {"retention": book.retention, "class": eff["class"], "delete_after_days": eff["delete_after_days"],
                 "due_at": _iso(due_at)},
        "erased": [{"seq": int(r["seq"]), "attestation_id": r["attestation_id"], "digest": r["digest"]} for r in recs],
        "blobs": {"removed": [h for h in shas if h not in kept], "kept": kept},
        "fragments": [mem_refs.pointer("attest", r["digest"]) for r in recs],
        "labels": dict(book.labels),
        "counts": _counts(ids),
        "evaluated_at": _iso(_now()),
    }  # fmt: skip


#: The keys of a dry run that go into the signed tombstone: what is removed and why, never a count
#: that moves or the time it was looked at.
SIGNED_KEYS = ("seal", "archive", "covers", "rule", "erased", "blobs", "labels")


def tombstone_content(site: str, logbook: str, archive_id: str | None,
                      seal_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The content a tombstone of the archive's period signs (or of a sealed period with no archive,
    named by ``seal_id``): its title and the dry run's names."""
    dry = preview(site, logbook, archive_id=archive_id) if archive_id else preview(site, logbook, seal_id=seal_id)
    lo, hi = dry["covers"]["from_seq"], dry["covers"]["to_seq"]
    return {"title": f"Removed under the retention rule: #{lo} to #{hi}", "fields": {},
            "tombstone": {k: dry[k] for k in SIGNED_KEYS}}  # fmt: skip


def check_unchanged(site: str, logbook: str, body: dict[str, Any]) -> None:
    """Refuse a tombstone whose period, rule or blobs changed since it was drafted."""
    now = tombstone_content(site, logbook, body["archive"]["archive_id"], body["seal"]["attestation_id"])["tombstone"]
    if now != body:
        raise RetentionRefused("what the tombstone would remove changed since the draft (its records, blobs or "
                               "rule); create the draft again")  # fmt: skip


def may_sign(handle: str, site: str, book: Any, *, state_dir: Path | None = None) -> str | None:
    """None when ``handle`` holds a role the log declares for tombstones; else why not."""
    from axiom.infra.paths import get_user_state_dir

    from .roles import roles_for

    rule = book.retention_rule
    if rule is None or not rule.tombstone_by:
        return f"{book.id} names no role that removes content"
    held_roles = set(roles_for(handle, site, state_dir=Path(state_dir or get_user_state_dir())))
    if not (held_roles - PLATFORM_ADMIN_ROLES) & set(rule.tombstone_by):
        return (f"{handle} holds no role that may remove content from {book.id} (needs one of "
                f"{', '.join(rule.tombstone_by)}; administration roles never do, ADR-142 rule 8)")
    return None


# -- reads hide a committed tombstone's period ---------------------------------------------------


def hidden_ranges(site: str, logbook: str) -> list[tuple[int, int, str]]:
    """``(from_seq, to_seq, tombstone_id)`` of each period whose content a tombstone hides."""
    with store.session_scope() as s:
        if not _table_exists(s):
            return []
        return [(int(t.from_seq), int(t.to_seq), t.tombstone_id) for t in s.execute(
            select(AttestTombstone).where(AttestTombstone.site_id == site, AttestTombstone.logbook == logbook,
                                          AttestTombstone.status.in_(HIDING))).scalars()]  # fmt: skip


def without_content(rec: dict[str, Any], tombstone_id: str) -> dict[str, Any]:
    """A record as a tombstone leaves it: no content, no evidence, and the tombstone named."""
    out = {k: v for k, v in rec.items() if k not in ("content", "evidence")}
    out["erased"] = {"tombstone": tombstone_id}
    return out


def tombstone_of(site: str, logbook: str, seq: int) -> dict[str, Any] | None:
    """``{attestation_id, seq}`` of the tombstone that hides or removed ``#seq``, or None."""
    for lo, hi, tid in hidden_ranges(site, logbook):
        if lo <= seq <= hi:
            with store.session_scope() as s:
                row = s.get(AttestRecord, tid)
                return {"attestation_id": tid, "seq": int(row.seq) if row else None}
    return None


# -- the erasure: a switch on the one runner (ADR-197) -------------------------------------------


def _row(t: AttestTombstone) -> dict[str, Any]:
    return {"tombstone_id": t.tombstone_id, "site_id": t.site_id, "logbook": t.logbook, "seal_id": t.seal_id,
            "archive_id": t.archive_id, "from_seq": int(t.from_seq), "to_seq": int(t.to_seq), "status": t.status,
            "switch_id": t.switch_id, "erased_at": _iso(t.erased_at), "erased_by": t.erased_by,
            "deletion": t.deletion, "created_by": t.created_by, "created_at": _iso(t.created_at)}  # fmt: skip


def _get(tombstone_id: str) -> AttestTombstone:
    with store.session_scope() as s:
        t = s.get(AttestTombstone, tombstone_id)
        if t is None:
            raise RetentionRefused(f"no tombstone {tombstone_id}")
        s.expunge(t)
        return t


def get(tombstone_id: str) -> dict[str, Any]:
    return _row(_get(tombstone_id))


def _signed(tombstone_id: str) -> dict[str, Any]:
    with store.session_scope() as s:
        row = s.get(AttestRecord, tombstone_id)
        if row is None or row.kind != "tombstone":
            raise RetentionRefused(f"{tombstone_id} is not a signed tombstone")
        return dict(row.record)


def _set(tombstone_id: str, **values: Any) -> None:
    with store.session_scope() as s:
        t = s.get(AttestTombstone, tombstone_id)
        for k, v in values.items():
            setattr(t, k, v)
        s.commit()


def _still_allowed(t: AttestTombstone) -> tuple[bool, str]:
    """The rule still allows this tombstone now: declared, deleting, not held, due."""
    try:
        book = registry.get(t.logbook)
        if book.retention_rule is None:
            return False, f"{t.logbook} no longer declares a retention rule"
        eff = effective(t.site_id, book)
    except (LogbookError, RetentionRefused) as exc:
        return False, str(exc)
    if not eff["deletes"]:
        return False, f"{t.logbook}'s rule no longer allows deletion"
    if eff["held"]:
        return False, f"{t.logbook} is under a hold ({eff['held']})"
    rec = _signed(t.tombstone_id)
    due = _when(rec["content"]["tombstone"]["rule"]["due_at"])
    seal = next((r for r in _seals(t.site_id, t.logbook) if r["attestation_id"] == t.seal_id), None)
    if seal is None:
        return False, "the seal the tombstone names is gone"
    later = _when(seal["recorded_at"]) + timedelta(days=int(eff["delete_after_days"]))
    if _now() < max(due, later):
        return False, f"not due until {_iso(max(due, later))}"
    return True, "the rule allows it now"


def _chain_holds(t: AttestTombstone) -> tuple[bool, str]:
    """The chain verifies from the period's first record through the tombstone."""
    from axiom.attest import verify as attest_verify

    rec = _signed(t.tombstone_id)
    report = attest_verify.chain(t.site_id, t.logbook, int(t.from_seq), int(rec["seq"]))
    return (True, "the chain verifies through the tombstone") if report.ok else (
        False, f"the chain fails at #{report.first_bad_seq}: {report.reason}")


class TombstoneHooks:
    """The four hooks and ``retire`` for one tombstone's erasure."""

    def __init__(self, tombstone_id: str) -> None:
        self.tombstone_id = tombstone_id

    def prepare(self, ctx: Any) -> None:
        t = _get(self.tombstone_id)
        body = _signed(self.tombstone_id)["content"]["tombstone"]
        recs = _covered(t.site_id, t.logbook, int(t.from_seq), int(t.to_seq))
        now = [{"seq": int(r["seq"]), "attestation_id": r["attestation_id"], "digest": r["digest"]} for r in recs]
        if now != body["erased"]:
            raise RetentionRefused("the chain no longer holds what the tombstone names; nothing is removed")
        _set(self.tombstone_id, plan={k: body[k] for k in SIGNED_KEYS})
        ctx.state["erased"] = len(now)

    def _allowed(self, ctx: Any) -> tuple[bool, str]:
        return _still_allowed(_get(self.tombstone_id))

    def _holds(self, ctx: Any) -> tuple[bool, str]:
        return _chain_holds(_get(self.tombstone_id))

    def probe(self, ctx: Any) -> list[Any]:
        from axiom.infra.switchplan import Probe

        return [Probe(name="rule-allows", kind="callable", when="pre", fn=self._allowed),
                Probe(name="chain-verifies", kind="callable", when="pre", fn=self._holds),
                Probe(name="rule-still-allows", kind="callable", when="post", fn=self._allowed),
                Probe(name="chain-still-verifies", kind="callable", when="post", fn=self._holds)]  # fmt: skip

    def commit(self, ctx: Any, scope: Any) -> str:
        with scope.holding():
            _set(self.tombstone_id, status="committed")
        return "reads of the period show no content; nothing is removed until a named approver retires it"

    def rollback(self, ctx: Any) -> str:
        t = _get(self.tombstone_id)
        if t.status == "committed":
            _set(self.tombstone_id, status="signed")
        return "reads of the period show its content again"

    def retire(self, ctx: Any) -> str:
        return _erase(_get(self.tombstone_id), approver=str(ctx.params.get("approver") or ""))


def _switch_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir()) / "attest" / "tombstone-switches"


def plan(tombstone_id: str, *, owner: str, change_path: Path | None = None, soak: timedelta = timedelta(0),
         approver: str | None = None) -> Any:  # fmt: skip
    """The tombstone's erasure on the one runner: ``reclaim`` retire, owned by ``owner``."""
    from axiom.infra.switchplan import FileLedger, SwitchPlan

    t = _get(tombstone_id)
    p = SwitchPlan(kind=KIND, subject=f"{t.site_id}/{t.logbook}#{t.from_seq}-{t.to_seq}",
                   hooks=TombstoneHooks(tombstone_id), ledger=FileLedger(_switch_dir()),
                   change_kind="migration", retire="reclaim", owner=owner, soak=soak,
                   change_path=change_path, record_id=t.switch_id, params={"approver": approver},
                   rollback_command=f"log retention rollback {tombstone_id}")  # fmt: skip
    if t.switch_id != p.record.id:
        _set(tombstone_id, switch_id=p.record.id)
    return p


def commit(tombstone_id: str, *, owner: str, change_path: Path | None = None) -> dict[str, Any]:
    """Prepare (the chain still holds what the tombstone names), prove (the rule allows it and the
    chain verifies through the tombstone) and commit: reads hide the period's content."""
    t = _get(tombstone_id)
    if t.status not in ("signed", "committed"):
        raise RetentionRefused(f"tombstone {tombstone_id} is {t.status}")
    plan(tombstone_id, owner=owner, change_path=change_path).run()
    return get(tombstone_id)


def rollback(tombstone_id: str, *, owner: str, reason: str = "operator request",
             change_path: Path | None = None) -> dict[str, Any]:  # fmt: skip
    plan(tombstone_id, owner=owner, change_path=change_path).rollback(reason)
    return get(tombstone_id)


def erase(tombstone_id: str, *, approver: str, change_path: Path | None = None,
          override_soak: str | None = None, approver_kind: str = "human") -> dict[str, Any]:  # fmt: skip
    """Remove the period's content: a named approver's retire through the runner (ADR-197 D6).
    Refused before the switch committed, by an approver who holds no tombstone role at the site,
    under a hold, or when the rule no longer allows it; the deletion is recorded before anything
    is destroyed. An agent or machine approver (``approver_kind``) is held to the declared rule, the
    hold and the platform floor, not to a role-table entry (ADR-290 D8): the tombstone it carries out
    was already signed by the accountable actor."""
    from axiom.infra import change_intent
    from axiom.infra.switchplan.plan import PRINCIPAL

    t = _get(tombstone_id)
    if t.status != "committed":
        raise RetentionRefused(f"tombstone {tombstone_id} is {t.status}; commit it first (reads hide the content "
                               "before anything is removed)")  # fmt: skip
    book = registry.get(t.logbook)
    if approver_kind == "human" and approver and PRINCIPAL.match(approver) and approver != "@system":
        why = may_sign(approver, t.site_id, book)
        if why:
            raise RetentionRefused(f"the approver must hold a tombstone role: {why}")
    reason = held(t.site_id, book)
    if reason:
        raise RetentionRefused(f"{t.logbook} is under a hold ({reason}); nothing is removed")
    p = plan(tombstone_id, owner=t.created_by, change_path=change_path, approver=approver)
    if not change_intent.has_intent(p.record.change_id, path=change_path):
        raise RetentionRefused("the erasure's change record was not written (ADR-182 D5a); commit it first")
    p.retire(approver=approver, override_soak=override_soak)
    return get(tombstone_id)


def _erase(t: AttestTombstone, *, approver: str) -> str:
    """Carry out a committed tombstone. The deletion goes on the action chain first; a step that
    cannot be taken after that is reported in the result, never skipped silently."""
    from axiom.governance import retention as gov
    from axiom.infra.paths import get_user_state_dir

    rec = _signed(t.tombstone_id)
    body = rec["content"]["tombstone"]
    ids = [e["attestation_id"] for e in body["erased"]]
    deletion = gov.record_deletion(
        kind="attest.tombstone", subject=f"{t.site_id}/{t.logbook}#{t.from_seq}-{t.to_seq}", principal=approver,
        site=t.site_id, retention_class=gov.RetentionClass.from_str(body["rule"]["class"]), forever=True,
        detail={"tombstone": t.tombstone_id, "tombstone_seq": int(rec["seq"]), "erased": body["erased"],
                "blobs_removed": body["blobs"]["removed"], "archive": body["archive"]},
        state_dir=Path(get_user_state_dir()),
    )  # fmt: skip
    _erase_rows(t, ids, approver=approver, deletion=deletion)
    notes = [f"{len(ids)} record(s) lost their content"]
    notes += _erase_blobs(t, body, ids)
    notes += _erase_package(t)
    notes += _retire_fragments(body, rec)
    notes += _erase_journal(t, ids, rec)
    return "; ".join(notes)


def _erase_rows(t: AttestTombstone, ids: list[str], *, approver: str, deletion: dict[str, Any]) -> None:
    """In one transaction: the chain rows, drafts, presentations and answers lose their content
    (the database guard allows exactly this change), and the derived rows go."""
    from sqlalchemy import delete, null, or_

    from . import index
    from .db_models import (
        AttestAttachmentIndex,
        AttestDraft,
        AttestFieldValue,
        AttestPresentation,
        AttestResponse,
    )

    tid = t.tombstone_id
    with store.session_scope() as s:
        index.ensure(s)
        for row in s.execute(select(AttestRecord).where(AttestRecord.attestation_id.in_(ids))).scalars():
            if "content" in (row.record or {}):
                row.record = without_content(dict(row.record), tid)
        drafts = list(s.execute(select(AttestDraft).where(AttestDraft.attestation_id.in_(ids))).scalars())
        for d in drafts:
            d.content, d.provenance, d.attachments = {"erased": tid}, {}, null()
        draft_ids = [d.draft_id for d in drafts]
        pres = list(s.execute(select(AttestPresentation).where(AttestPresentation.draft_id.in_(draft_ids))).scalars())
        for p in pres:
            if "erased" not in (p.statement or {}):
                p.statement, p.rendered, p.forms, p.speakable = {"erased": tid}, "", null(), None
        for r in s.execute(select(AttestResponse).where(
                AttestResponse.presentation_id.in_([p.presentation_id for p in pres]),
                AttestResponse.transcript.isnot(None))).scalars():  # fmt: skip
            r.transcript = None
        s.execute(delete(AttestFieldValue).where(or_(AttestFieldValue.attestation_id.in_(ids),
                                                     AttestFieldValue.source_attestation_id.in_(ids))))  # fmt: skip
        s.execute(delete(AttestAttachmentIndex).where(AttestAttachmentIndex.attestation_id.in_(ids)))
        index.remove_records(s, ids)
        row = s.get(AttestTombstone, tid)
        row.status, row.erased_at, row.erased_by, row.deletion = "erased", datetime.now(UTC), approver, deletion
        a = s.get(AttestArchive, t.archive_id) if t.archive_id else None
        if a is not None:
            a.status = "tombstoned"
        s.commit()


def _erase_blobs(t: AttestTombstone, body: dict[str, Any], ids: list[str]) -> list[str]:
    from . import attachments

    gone, kept = [], []
    for sha in body["blobs"]["removed"]:
        if _named_elsewhere(t.site_id, sha, set(ids)):
            kept.append(sha)  # named again since the tombstone was signed: it stays
            continue
        attachments.remove(t.site_id, sha)
        gone.append(sha)
    out = [f"{len(gone)} blob(s) removed"]
    if kept:
        out.append(f"{len(kept)} blob(s) kept: a live record names them again ({', '.join(kept)})")
    return out


def _erase_package(t: AttestTombstone) -> list[str]:
    """The archived package held the period's content: it goes too (its digest is in the
    tombstone and the deletion record)."""
    from . import archives

    if not t.archive_id:
        return []  # erased under pressure with no archive: there is no package
    try:
        a = archives._get(t.archive_id)  # noqa: SLF001
        archives._provider_for(a).delete(a.storage_id)  # noqa: SLF001
        archives._packages.pop(a.package_sha256, None)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - reported, never silent
        log.warning("the package of archive %s could not be removed: %s", t.archive_id, exc)
        return [f"the package of archive {t.archive_id} could NOT be removed ({type(exc).__name__}); "
                "remove it from the archive target by hand"]  # fmt: skip
    return [f"the package of archive {t.archive_id} removed"]


def _retire_fragments(body: dict[str, Any], rec: dict[str, Any]) -> list[str]:
    from axiom.memory import references as mem_refs

    pointers = [mem_refs.pointer("attest", e["digest"]) for e in body["erased"]]
    done = mem_refs.retire(pointers, tombstone={"attestation_id": rec["attestation_id"], "seq": int(rec["seq"])})
    return [f"memory fragments pointing at them retired by {done} store(s)"]


def _erase_journal(t: AttestTombstone, ids: list[str], rec: dict[str, Any]) -> list[str]:
    try:
        from axiom.extensions.builtins.journal import erasures
    except ImportError:
        return []
    try:
        n = erasures.erase("attest_records", ids, reason={"tombstone": rec["attestation_id"],
                                                         "seq": int(rec["seq"]), "logbook": t.logbook})  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - reported, never silent
        log.warning("the journal's words for tombstone %s were not removed: %s", t.tombstone_id, exc)
        return [f"the journal's words could NOT be removed ({type(exc).__name__}); run it again"]
    return [f"{n} journal reference(s) lost their words"]


__all__ = [
    "ARCHIVED", "HIDING", "KIND", "SIGNED_KEYS", "RetentionRefused", "TombstoneHooks", "check_unchanged",
    "commit", "effective", "erase", "get", "held", "hidden_ranges", "may_sign", "plan", "preview", "report",
    "rollback", "tombstone_content", "tombstone_of", "without_content",
]
