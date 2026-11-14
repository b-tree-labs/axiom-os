# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""History from another system placed on a log's chain as ``import`` records (#1804, ADR-262).

Design: ``docs/working/design-note-import-records.md``, approved by the owner 2026-11-13.

The Axiom half of an import reads **mapped history**: JSON Lines, one object per entry, which a
consumer's half produces from its own source format. Each object carries::

    occurred_at      required, ISO 8601: when the entry happened
    title            required: the entry's title
    body, fields     optional: the entry's text and its fields as the log's type names them
    entry_type       optional: the log's type the entry maps to
    by               optional: who wrote it, as the source names them (text, never resolved)
    original_meaning optional: the meaning the source stated, kept as text
    source_ref       optional: the source's own id; else the SHA-256 of the canonical line
    interval         optional: {kind, number} when the source names one
    source_hash, source_prev   optional: the source's own chain, checked once per batch

Three steps, each its own function:

- :func:`dry_run` reads the file and the chain and writes nothing. It returns the counts, every
  mapping decision, the warnings (an entry dated after the log's first live entry) and the
  input's digest. Anyone who may read the log may ask for one, software and agents included.
- :func:`commit` writes, only for a person holding the ``importer`` role for the log
  (:func:`roles.holds_log_role`), only if the input still has the dry run's digest and the dry
  run found no errors. It runs under the chain head's lock, appends one record per new entry
  (sealed by the node, no person as signer), writes each entry's reference to
  ``attest_import_refs`` in the same transaction, and records one entry on the action audit
  chain. An entry already on the chain is skipped and counted, never written twice.
- :func:`withdraw` appends one ``import_withdrawal`` record for a whole batch, with a reason,
  and marks its references withdrawn so the history can be imported again. Nothing is deleted.

An import never opens, closes or renumbers an interval, never meets an obligation and changes
nothing in verification. The web commits through the node's gate (``/gate/import/commit``), never
through a raw HTTP route; :func:`prepare` keeps the dry run the gate's page shows and commits.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from axiom.attest.canonical import CanonicalError, normalise
from axiom.attest.chain import Signer, seal

from . import registry, store
from .db_models import AttestImportRef, AttestRecord
from .logbooks import LogbookError
from .service import AttestRefused

#: The role that may commit and withdraw imports: held for one log, with an expiry (ADR-262).
IMPORTER_ROLE = "importer"
#: The meaning every import record carries; the source's own goes in content.original_meaning.
IMPORTED = "imported"
WITHDRAWAL_KIND = "import_withdrawal"
#: The most a single import may read.
MAX_BYTES = 5_000_000
#: How long a dry run prepared for the web waits for its commit at the gate.
PREPARED_TTL = timedelta(minutes=30)

_KEYS = frozenset({"occurred_at", "title", "body", "fields", "entry_type", "by",
                   "original_meaning", "source_ref", "interval", "source_hash", "source_prev"})  # fmt: skip


class ImportRefused(AttestRefused):
    """Nothing was written: the reason says why."""


def _now() -> datetime:
    """The clock a role's expiry is read against (a seam for tests)."""
    return datetime.now(UTC)


def _recorded_now() -> datetime:
    from . import service

    return service._now()


# -- the source's own chain ----------------------------------------------------------------------

Verifier = Callable[[list[dict[str, Any]]], str]
_verifiers: dict[str, Verifier] = {}


def register_source_verifier(source: str, fn: Verifier) -> None:
    """A consumer's check of its source's own chain or checksums, for imports from ``source``:
    ``fn(entries)`` answers ``"ok"``, ``"broken at <source_ref>"`` or ``"none"``."""
    _verifiers[source] = fn


def unregister_source_verifier(source: str) -> None:
    _verifiers.pop(source, None)


def _linked(entries: list[dict[str, Any]]) -> str:
    """The default check: each entry's ``source_prev`` names the previous entry's
    ``source_hash``. ``none`` when the source carries no chain."""
    if not any(e.get("source_hash") for e in entries):
        return "none"
    prev = None
    for i, e in enumerate(entries):
        if i and e.get("source_prev") != prev:
            return f"broken at {e['source_ref']}"
        prev = e.get("source_hash")
    return "ok"


# -- reading the input ---------------------------------------------------------------------------


def digest_of(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("occurred_at is required, as an ISO 8601 time")
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"occurred_at {value!r} is not an ISO 8601 time") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


@dataclass
class _Entry:
    line: int
    raw: dict[str, Any] | None
    source_ref: str | None
    error: str | None = None
    occurred_at: datetime | None = None
    entry_type: str | None = None
    interval: dict[str, Any] | None = None


def _read(content: bytes) -> list[_Entry]:
    if len(content) > MAX_BYTES:
        raise ImportRefused(f"the input is {len(content)} bytes; an import reads at most {MAX_BYTES}")
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ImportRefused("the input is not UTF-8 text") from None
    out: list[_Entry] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            # Decimals stay text: no float ever reaches signed content.
            raw = json.loads(line, parse_float=str)
        except ValueError as exc:
            out.append(_Entry(n, None, None, error=f"not JSON: {exc.msg}"))
            continue
        if not isinstance(raw, dict):
            out.append(_Entry(n, None, None, error="each line is one JSON object"))
            continue
        ref = raw.get("source_ref")
        if ref is None or ref == "":
            canon = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            ref = "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
        out.append(_Entry(n, raw, str(ref)))
    return out


def _check(e: _Entry, book: Any, now: datetime) -> None:
    raw = e.raw or {}
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ValueError(f"unknown key {unknown}; a line carries {sorted(_KEYS)}")
    e.occurred_at = _parse_time(raw.get("occurred_at"))
    if e.occurred_at > now:
        raise ValueError(f"occurred_at {raw['occurred_at']} is in the future")
    if not isinstance(raw.get("title"), str) or not raw["title"].strip():
        raise ValueError("title is required")
    for key in ("body", "by", "original_meaning"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise ValueError(f"{key} is text")
    fields = raw.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("fields is an object")
    if raw.get("entry_type"):
        try:
            et = book.type(str(raw["entry_type"]))
        except LogbookError as exc:
            raise ValueError(str(exc)) from None
        unknown_fields = sorted(set(fields) - {f.id for f in et.fields})
        if unknown_fields:
            raise ValueError(f"{book.id}.{et.id}: unknown field {unknown_fields}")
        e.entry_type = et.id
    elif fields:
        raise ValueError("fields need an entry_type that declares them")
    iv = raw.get("interval")
    if iv is not None:
        if not isinstance(iv, dict) or set(iv) - {"kind", "number"} or "kind" not in iv:
            raise ValueError("interval is {kind, number}")
        if iv["kind"] not in book.intervals:
            raise ValueError(f"{book.id} declares no interval {iv['kind']!r}")
        try:
            number = int(str(iv.get("number")))
        except ValueError:
            raise ValueError("an interval's number is a whole number") from None
        e.interval = {"kind": str(iv["kind"]), "number": number}
    try:
        normalise({"fields": fields})
    except CanonicalError as exc:
        raise ValueError(f"fields cannot be signed content: {exc}") from None


def _first_live(s: Any, site: str, logbook: str) -> tuple[int, datetime] | None:
    row = s.execute(
        text(
            "SELECT seq, occurred_at FROM attest_records WHERE site_id = :site AND "
            "logbook = :logbook AND kind NOT IN ('import', 'import_withdrawal') ORDER BY seq LIMIT 1"
        ),
        {"site": site, "logbook": logbook},
    ).one_or_none()
    return None if row is None else (int(row.seq), row.occurred_at)


def _live_refs(s: Any, site: str, logbook: str, refs: list[str]) -> set[str]:
    if not refs:
        return set()
    rows = s.execute(
        text(
            "SELECT source_ref FROM attest_import_refs WHERE site_id = :site AND logbook = :logbook "
            "AND withdrawn_at IS NULL AND source_ref = ANY(:refs)"
        ),
        {"site": site, "logbook": logbook, "refs": refs},
    ).scalars()
    return set(rows)


def _plan(s: Any, site: str, logbook: str, content: bytes, source: str) -> dict[str, Any]:
    try:
        book = registry.get(logbook)
    except LogbookError as exc:
        raise ImportRefused(str(exc)) from None
    entries = _read(content)
    now = _recorded_now()
    seen: set[str] = set()
    for e in entries:
        if e.error:
            continue
        try:
            _check(e, book, now)
        except ValueError as exc:
            e.error = str(exc)
            continue
        if e.source_ref in seen:
            e.error = f"source_ref {e.source_ref!r} appears twice in this input"
        seen.add(e.source_ref)
    on_chain = _live_refs(s, site, logbook, [e.source_ref for e in entries if not e.error])
    first = _first_live(s, site, logbook)
    decisions: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for e in entries:
        d: dict[str, Any] = {"line": e.line, "source_ref": e.source_ref, "entry_type": e.entry_type,
                             "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None}  # fmt: skip
        if e.error:
            d.update(action="error", reason=e.error)
        elif e.source_ref in on_chain:
            d.update(action="skip", reason="already on the chain, from an earlier import")
        else:
            d.update(action="new", reason=f"type {e.entry_type}" if e.entry_type else "no entry type")
            if first is not None and e.occurred_at > first[1]:
                warnings.append({"line": e.line, "source_ref": e.source_ref,
                                 "warning": f"dated after the log's first live entry (#{first[0]} "
                                            f"at {first[1].isoformat()}); it is placed by its own "
                                            "time in every view"})  # fmt: skip
        decisions.append(d)
    good = [e.raw | {"source_ref": e.source_ref} for e in entries if not e.error]
    verifier = _verifiers.get(source, _linked)
    try:
        verified = verifier(good)
    except Exception as exc:  # noqa: BLE001 - a failing check is reported, never trusted
        verified = f"not checked: {exc}"
    counts = {
        "lines": len(entries),
        "new": sum(1 for d in decisions if d["action"] == "new"),
        "skipped": sum(1 for d in decisions if d["action"] == "skip"),
        "errors": sum(1 for d in decisions if d["action"] == "error"),
        "warnings": len(warnings),
    }
    return {"site_id": site, "logbook": logbook, "source": source, "digest": digest_of(content),
            "counts": counts, "decisions": decisions, "warnings": warnings,
            "source_verified": verified, "written": 0, "_entries": entries,
            "_book": book}  # fmt: skip


def _public(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def _source_name(source: str | None) -> str:
    name = (source or "file").strip()
    if not name or not all(c.isalnum() or c in "-_." for c in name):
        raise ImportRefused(f"source {source!r} must be a short name: letters, digits, - _ .")
    return name


def dry_run(site: str, logbook: str, content: bytes, *, source: str | None = None) -> dict[str, Any]:
    """What an import of ``content`` would write, and why; writes nothing."""
    name = _source_name(source)
    with store.session_scope() as s:
        return _public(_plan(s, site, logbook, content, name))


# -- committing ----------------------------------------------------------------------------------


def _check_importer(by: str, site: str, logbook: str, state_dir: Path) -> None:
    from . import roles

    held, why = roles.holds_log_role(by, site, logbook, IMPORTER_ROLE, state_dir=state_dir, now=_now())
    if not held:
        raise ImportRefused(
            f"{why}. Only a person holding the importer role for this log may import or withdraw "
            f"(`log role grant {by} importer --log {logbook} --until DATE`); no administration "
            "or signing role implies it"
        )


def _state(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir())


def _signer(signer: Signer | None) -> Signer:
    if signer is not None:
        return signer
    from . import signing

    return signing.signer()


def _append(s: Any, site: str, logbook: str, body: dict[str, Any], *, seq: int, prev: str,
            signer: Signer, signer_col: str = "") -> dict[str, Any]:  # fmt: skip
    record = seal(normalise(body), seq=seq, prev_digest=prev, signer=signer)
    s.add(
        AttestRecord(
            attestation_id=record["attestation_id"], site_id=site, logbook=logbook,
            seq=record["seq"], digest=record["digest"], prev_digest=prev, kind=record["kind"],
            meaning=record["meaning"], entry_type=record.get("entry_type"), target=None,
            signer=signer_col, occurred_at=datetime.fromisoformat(record["occurred_at"].replace("Z", "+00:00")),
            recorded_at=datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00")),
            record=record, response_id=None,
        )
    )  # fmt: skip
    return record


def _move_head(s: Any, site: str, logbook: str, record: dict[str, Any]) -> None:
    s.execute(
        text(
            "UPDATE attest_chain_heads SET seq = :seq, digest = :digest "
            "WHERE site_id = :site AND logbook = :logbook"
        ),
        {"seq": record["seq"], "digest": record["digest"], "site": site, "logbook": logbook},
    )


def _audit(state_dir: Path, skill: str, params: dict[str, Any], by: str, site: str) -> None:
    from axiom.infra import audit_trail

    # What was written goes in the record's ``change`` (the params are kept only as a digest).
    audit_trail.record_invocation(state_dir=state_dir, skill=skill, params=params, principal=by,
                                  site=site, outcome="ok", errors_count=0,
                                  change={"kind": skill.removeprefix("attest."), **params})  # fmt: skip


def _publish(subject: str, payload: dict[str, Any]) -> None:
    try:
        from axiom.infra.bus import get_default_eventbus

        get_default_eventbus().publish(subject, {"schema_version": 1, **payload}, source="attest")
    except Exception:  # noqa: BLE001 - the records are committed; a lost event costs nothing
        pass


def commit(site: str, logbook: str, content: bytes, *, digest: str, by: str,
           source: str | None = None, via: str = "cli", state_dir: Path | None = None,
           signer: Signer | None = None) -> dict[str, Any]:  # fmt: skip
    """Write the import the dry run of ``content`` described, as ``by``. Refused, with nothing
    written, unless ``by`` holds the importer role for the log now, ``content`` still has
    ``digest``, and the dry run finds no errors. Returns the counts, the batch and its records;
    ``written`` is 0 and ``batch`` None when every entry was already on the chain."""
    from sqlalchemy.exc import IntegrityError

    from . import service

    name = _source_name(source)
    where = _state(state_dir)
    _check_importer(by, site, logbook, where)
    if digest_of(content) != digest:
        raise ImportRefused(
            f"the input's digest is {digest_of(content)}, not {digest}: it changed since its dry "
            "run. Run the dry run again and commit its digest"
        )
    sealer = _signer(signer)
    batch = str(uuid.uuid4())
    written: list[dict[str, Any]] = []
    with store.session_scope() as s:
        seq, prev = service._lock_head(s, site, logbook)
        # Planned again under the lock, so a concurrent import's entries are skipped here.
        plan = _plan(s, site, logbook, content, name)
        if plan["counts"]["errors"]:
            raise ImportRefused(
                f"the dry run found {plan['counts']['errors']} error(s); nothing was written. "
                "Fix the input and run the dry run again"
            )
        new = {d["line"] for d in plan["decisions"] if d["action"] == "new"}
        now = _recorded_now()
        book = plan["_book"]
        for e in plan["_entries"]:
            if e.line not in new:
                continue
            raw = e.raw or {}
            content_ = {"title": raw["title"].strip(), "fields": dict(raw.get("fields") or {})}
            if raw.get("body"):
                content_["body"] = raw["body"]
            if raw.get("original_meaning"):
                content_["original_meaning"] = raw["original_meaning"]
            body: dict[str, Any] = {
                "attestation_id": str(uuid.uuid4()), "site_id": site, "logbook": logbook,
                "logbook_version": book.version, "kind": "import", "meaning": IMPORTED,
                "entry_type": e.entry_type, "subjects": [], "content": content_,
                "origin": f"integration:{name}", "source": "import",
                "signer": {"kind": "unverified", "display": raw.get("by") or None, "principal": None},
                "imported_by": by, "import_batch": batch, "source_ref": e.source_ref,
                "source_digest": plan["digest"], "source_verified": plan["source_verified"],
                "occurred_at": e.occurred_at, "recorded_at": now,
            }  # fmt: skip
            if e.interval is not None:
                body["interval"] = e.interval  # never an event: nothing is opened or closed
            record = _append(s, site, logbook, body, seq=seq + 1, prev=prev, signer=sealer)
            s.add(AttestImportRef(attestation_id=record["attestation_id"], site_id=site,
                                  logbook=logbook, source_ref=e.source_ref, batch_id=batch))  # fmt: skip
            seq, prev = record["seq"], record["digest"]
            written.append(record)
        if written:
            _move_head(s, site, logbook, written[-1])
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                raise ImportRefused(
                    "another import wrote one of these entries meanwhile; nothing was written. "
                    "Run the dry run again"
                ) from None
    out = {"site_id": site, "logbook": logbook, "digest": plan["digest"],
           "batch": batch if written else None, "written": len(written),
           "skipped": plan["counts"]["skipped"], "warnings": plan["warnings"],
           "source_verified": plan["source_verified"], "records": written}  # fmt: skip
    if written:
        _audit(where, "attest.import_commit",
               {"logbook": logbook, "site": site, "batch": batch, "written": len(written),
                "skipped": plan["counts"]["skipped"], "digest": plan["digest"], "via": via,
                "source": name}, by, site)  # fmt: skip
        _publish(f"attest.{logbook}.imported", {"site_id": site, "logbook": logbook, "batch": batch,
                                                "written": len(written), "imported_by": by})  # fmt: skip
    return out


# -- withdrawing a batch -------------------------------------------------------------------------


def withdraw(site: str, logbook: str, batch: str, *, reason: str, by: str, via: str = "cli",
             state_dir: Path | None = None, signer: Signer | None = None) -> dict[str, Any]:  # fmt: skip
    """Withdraw a whole batch, with a reason, by appending one ``import_withdrawal`` record.
    Nothing is changed or deleted; the batch's references stop counting, so it can be imported
    again. Refused for a batch with a correction or supplement on any of its entries."""
    from . import service

    if not isinstance(reason, str) or not reason.strip():
        raise ImportRefused("a withdrawal needs a reason: say why the batch is not to be relied on")
    where = _state(state_dir)
    _check_importer(by, site, logbook, where)
    sealer = _signer(signer)
    with store.session_scope() as s:
        seq, prev = service._lock_head(s, site, logbook)
        refs = list(
            s.execute(
                text("SELECT attestation_id, withdrawn_at FROM attest_import_refs WHERE "
                     "batch_id = :b AND site_id = :site AND logbook = :logbook"),
                {"b": batch, "site": site, "logbook": logbook},
            ).all()
        )  # fmt: skip
        if not refs:
            raise ImportRefused(f"no import batch {batch} in {logbook} at {site}")
        if any(r.withdrawn_at is not None for r in refs):
            raise ImportRefused(f"import batch {batch} is already withdrawn")
        ids = sorted(r.attestation_id for r in refs)
        acted = s.execute(
            text("SELECT target, kind FROM attest_records WHERE target = ANY(:ids) LIMIT 1"),
            {"ids": ids},
        ).one_or_none()
        if acted is not None:
            raise ImportRefused(
                f"import batch {batch} has a correction or supplement on entry {acted.target}; "
                "a batch whose entries were corrected is kept. Correct its other entries instead"
            )
        now = _recorded_now()
        book = registry.get(logbook)
        body = {
            "attestation_id": str(uuid.uuid4()), "site_id": site, "logbook": logbook,
            "logbook_version": book.version, "kind": WITHDRAWAL_KIND, "meaning": "retracted",
            "entry_type": None, "subjects": [],
            "content": {"title": "Import batch withdrawn", "body": reason.strip(), "fields": {}},
            "origin": "human", "source": via,
            "signer": {"kind": "none", "display": None, "principal": None},
            "withdrawn_by": by, "import_batch": batch, "targets": ids, "reason": reason.strip(),
            "occurred_at": now, "recorded_at": now,
        }  # fmt: skip
        record = _append(s, site, logbook, body, seq=seq + 1, prev=prev, signer=sealer)
        _move_head(s, site, logbook, record)
        s.execute(text("UPDATE attest_import_refs SET withdrawn_at = :at WHERE batch_id = :b"),
                  {"at": now, "b": batch})  # fmt: skip
        s.commit()
    _audit(where, "attest.import_withdraw",
           {"logbook": logbook, "site": site, "batch": batch, "entries": len(ids), "via": via},
           by, site)  # fmt: skip
    _publish(f"attest.{logbook}.import_withdrawn", {"site_id": site, "logbook": logbook,
                                                    "batch": batch, "withdrawn_by": by})  # fmt: skip
    return {"site_id": site, "logbook": logbook, "batch": batch, "withdrawn": len(ids),
            "record": record}  # fmt: skip


def withdrawn_ids(attestation_ids: list[str]) -> set[str]:
    """Which of these import records belong to a withdrawn batch, in one query."""
    if not attestation_ids:
        return set()
    with store.session_scope() as s:
        return set(
            s.execute(
                text("SELECT attestation_id FROM attest_import_refs WHERE withdrawn_at IS NOT NULL "
                     "AND attestation_id = ANY(:ids)"),
                {"ids": list(attestation_ids)},
            ).scalars()
        )  # fmt: skip


def batches(site: str, logbook: str) -> list[dict[str, Any]]:
    """Every import batch of a log, oldest first: when, by whom, how many, and whether withdrawn."""
    with store.session_scope() as s:
        rows = s.execute(
            text(
                "SELECT record->>'import_batch' AS batch, min(seq) AS first_seq, max(seq) AS last_seq, "
                "count(*) AS n, min(recorded_at) AS committed_at, min(occurred_at) AS earliest, "
                "max(occurred_at) AS latest, min(record->>'imported_by') AS imported_by, "
                "min(record->>'source_verified') AS source_verified, "
                "min(record->>'origin') AS origin FROM attest_records WHERE site_id = :site AND "
                "logbook = :logbook AND kind = 'import' GROUP BY 1 ORDER BY 2"
            ),
            {"site": site, "logbook": logbook},
        ).all()
        gone = {
            r.batch: r for r in s.execute(
                text("SELECT record->>'import_batch' AS batch, record->>'reason' AS reason, "
                     "record->>'withdrawn_by' AS by, recorded_at FROM attest_records WHERE "
                     "site_id = :site AND logbook = :logbook AND kind = 'import_withdrawal'"),
                {"site": site, "logbook": logbook},
            ).all()
        }  # fmt: skip
    out = []
    for r in rows:
        w = gone.get(r.batch)
        out.append({
            "batch": r.batch, "entries": int(r.n), "first_seq": int(r.first_seq),
            "last_seq": int(r.last_seq), "committed_at": r.committed_at.isoformat(),
            "earliest": r.earliest.isoformat(), "latest": r.latest.isoformat(),
            "imported_by": r.imported_by, "source_verified": r.source_verified,
            "source": (r.origin or "").removeprefix("integration:"),
            "withdrawn": None if w is None else {"reason": w.reason, "by": w.by,
                                                 "at": w.recorded_at.isoformat()},
        })  # fmt: skip
    return out


# -- a dry run prepared for the web, committed at the gate -----------------------------------


def _prepared_dir(state_dir: Path) -> Path:
    return state_dir / "attest" / "import_prepared"


def _safe_return(return_to: str | None) -> str | None:
    """Only a path on this node: never another origin, never a scheme."""
    if not return_to or not isinstance(return_to, str):
        return None
    if not return_to.startswith("/") or return_to.startswith("//") or "\\" in return_to:
        return None
    return return_to


def prepare(site: str, logbook: str, content: bytes, *, by: str, source: str | None = None,
            return_to: str | None = None, state_dir: Path | None = None) -> dict[str, Any]:  # fmt: skip
    """A dry run kept for its commit at the node's gate. Writes nothing to the chain; the kept
    input is working state and lives :data:`PREPARED_TTL`."""
    result = dry_run(site, logbook, content, source=source)
    prepared_id = str(uuid.uuid4())
    where = _prepared_dir(_state(state_dir))
    where.mkdir(parents=True, exist_ok=True)
    (where / f"{prepared_id}.json").write_text(json.dumps({
        "prepared_id": prepared_id, "site_id": site, "logbook": logbook, "by": by,
        "source": _source_name(source), "digest": result["digest"],
        "content": content.decode("utf-8"), "return_to": _safe_return(return_to),
        "created_at": _now().isoformat(),
    }), encoding="utf-8")  # fmt: skip
    return {**result, "prepared_id": prepared_id}


def prepared(prepared_id: str, *, state_dir: Path | None = None) -> dict[str, Any] | None:
    """The kept dry run, or None when unknown, used or expired."""
    try:
        uuid.UUID(str(prepared_id))
    except ValueError:
        return None
    path = _prepared_dir(_state(state_dir)) / f"{prepared_id}.json"
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if _now() - datetime.fromisoformat(kept["created_at"]) > PREPARED_TTL:
        path.unlink(missing_ok=True)
        return None
    return kept


def spend(prepared_id: str, *, state_dir: Path | None = None) -> None:
    """A kept dry run commits once."""
    (_prepared_dir(_state(state_dir)) / f"{prepared_id}.json").unlink(missing_ok=True)


__all__ = [
    "IMPORTED",
    "IMPORTER_ROLE",
    "ImportRefused",
    "WITHDRAWAL_KIND",
    "batches",
    "commit",
    "digest_of",
    "dry_run",
    "prepare",
    "prepared",
    "register_source_verifier",
    "spend",
    "unregister_source_verifier",
    "withdraw",
    "withdrawn_ids",
]
