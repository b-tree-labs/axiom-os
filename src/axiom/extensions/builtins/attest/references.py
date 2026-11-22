# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The log's side of memory reference fragments (unit LD-3, ADR-251 D2).

A memory fragment about a signed record points at it (``axiom://attest/sha256:<digest>``, with
its id, log, sequence and site) and holds none of its content (:mod:`axiom.memory.references`).
This module is the log's :class:`~axiom.memory.references.Resolver` for that pointer:

- **facts** for writing one: the record as the gold view ``gold.log_records`` has it. A draft or
  a tray item is not in gold, so it cannot be referenced; a record of a log with no declaration
  has no labels, so it cannot either.
- **admit** and **resolve**: the door ``attest.reference`` in the one registry, asked with the
  caller's rights at that moment. An allow returns the record as of the gold watermark with a
  deep link; every allow and every deny writes one read record. A record whose content gold no
  longer holds (removed under its retention rule, LD-6) resolves to its tombstone: digest and
  labels, nothing else.

Serving reads gold, never the chain's own table (ADR-128).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import text

from axiom.governance import doors as g
from axiom.memory import references as refs

from . import doors as log_doors
from . import gold, store

SCHEME = "attest"
DOOR = "attest.reference"

_POINTER = re.compile(r"^axiom://attest/sha256:([0-9a-f]{64})$")
_COLUMNS = ("attestation_id, site_id, logbook, seq, kind, meaning, entry_type, signer, occurred_at, "
            "recorded_at, target, content, digest")  # fmt: skip


def _gold_row(*, attestation_id: str | None = None, digest: str | None = None) -> dict[str, Any] | None:
    """One signed record from the gold view, by id or by digest."""
    where, value = ("attestation_id = :v", attestation_id) if attestation_id else ("digest = :v", digest)
    if not value:
        return None
    with store.session_scope() as s:
        gold.ensure(s)
        # The view name passed gold.ident() via qualified(); columns and the WHERE column are constants.
        # nosemgrep: avoid-sqlalchemy-text -- validated names only; the one value is bound
        row =s.execute(text(f"SELECT {_COLUMNS} FROM {gold.qualified()} WHERE {where} LIMIT 1"),
                        {"v": value}).mappings().first()  # fmt: skip
    if row is None:
        return None
    from . import archives

    return archives.overlay_gold(dict(row))  # an archived period's content comes from its package


def _row_for(record: str) -> dict[str, Any] | None:
    m = _POINTER.match(record)
    return _gold_row(digest=m.group(1)) if m else _gold_row(attestation_id=record)


def _target(row: dict[str, Any]) -> g.Target:
    return log_doors.target(row["site_id"], row["logbook"], (str(row["attestation_id"]),))


def _state_dir(caller: Any) -> Path | None:
    return getattr(caller, "state_dir", None)


def _record(caller: Any, the_target: g.Target, decision: g.Decision, transport: str,
            ids: tuple[str, ...] = ()) -> None:  # fmt: skip
    log_doors.record(DOOR, caller, the_target, decision, transport=transport, ids=ids,
                     count=len(ids), params={"logbook": the_target.scope, "site": the_target.site},
                     state_dir=_state_dir(caller), standalone=True)  # fmt: skip


def _check(caller: Any, the_target: g.Target, transport: str) -> g.Decision:
    decision = g.check(g.get(DOOR), caller, the_target)
    log_doors.clear()  # nothing is carried: this read is recorded here, not by a skill
    return decision


def served(row: dict[str, Any]) -> dict[str, Any]:
    """The record as the door serves it (from gold), credentials redacted, or its tombstone."""
    from axiom.infra.secret_scan import redact

    aid, st, b = row["attestation_id"], row["site_id"], row["logbook"]
    out: dict[str, Any] = {
        "as_of": gold.watermark(st, b),
        "link": f"/api/v1/attest/{b}/records/{aid}?site={st}",
        "url": f"#/log/{b}/{aid}",
    }
    if row.get("content") is None:
        tomb = {"attestation_id": aid, "digest": row["digest"], "labels": log_doors.labels_for(b)}
        from . import retention

        by = retention.tombstone_of(st, b, int(row["seq"])) if row.get("seq") is not None else None
        if by is not None:
            tomb["removed_by"] = by  # the signed tombstone that removed it (unit LD-6)
        out.update(record=None, tombstone=tomb)
        return out
    words, _ = redact(json.dumps(row["content"], default=str))
    out["record"] = {
        "attestation_id": aid, "site": st, "logbook": b, "seq": row["seq"],
        "kind": row.get("kind") or "statement", "meaning": row.get("meaning"),
        "entry_type": row.get("entry_type"), "signer": row.get("signer"),
        "occurred_at": gold.iso(row.get("occurred_at")), "target": row.get("target"),
        "digest": row["digest"], "content": json.loads(words),
    }  # fmt: skip
    out["tombstone"] = None
    return out


class LogResolver:
    """The log's resolver for ``axiom://attest/...`` pointers."""

    def facts(self, record: str) -> refs.RecordFacts:
        row = _row_for(str(record or ""))
        if row is None:
            raise refs.ReferenceRefused("only a signed record can be referenced; this is not one "
                                        "(a draft or a tray item is not a record)")  # fmt: skip
        return refs.RecordFacts(scheme=SCHEME, id=str(row["attestation_id"]), scope=row["logbook"],
                                seq=int(row["seq"]), site=row["site_id"], digest=row["digest"],
                                labels=log_doors.labels_for(row["logbook"]),
                                content=row.get("content"))  # fmt: skip

    def admit(self, caller: Any, facts: refs.RecordFacts) -> str | None:
        row = {"attestation_id": facts.id, "site_id": facts.site, "logbook": facts.scope}
        the_target = _target(row)
        decision = _check(caller, the_target, "agent_tool")
        _record(caller, the_target, decision, "agent_tool", (facts.id,) if decision.allowed else ())
        return None if decision.allowed else decision.code

    def caller_for(self, principal: str) -> Any:
        return caller_for(principal)

    def resolve(self, reference: Mapping[str, Any], *, caller: Any, transport: str) -> refs.Resolution:
        from . import archives

        archives.forget_source()  # a new read: no package has been read for this pointer yet
        row = _gold_row(attestation_id=str(reference.get("id") or ""))
        if row is None or row["digest"] != reference.get("digest"):
            # A pointer gold cannot match is answered like a record the caller may not read.
            asked = g.Target(site=reference.get("site"), scope=reference.get("scope"),
                             labels=log_doors.labels_for(reference.get("scope")))  # fmt: skip
            reason = g.Reason.NOT_PERMITTED if getattr(caller, "resolved", False) else g.Reason.UNRESOLVED_CALLER
            decision = log_doors.refused(DOOR, reason, caller, asked)
            _record(caller, asked, decision, transport)
            return refs.Resolution(False, decision.code)
        the_target = _target(row)
        decision = _check(caller, the_target, transport)
        if not decision.allowed:
            _record(caller, the_target, decision, transport)
            return refs.Resolution(False, decision.code)
        out = served(row)
        _record(caller, the_target, decision, transport, (str(row["attestation_id"]),))
        return refs.Resolution(True, None, record=out["record"], as_of=out["as_of"], link=out["link"],
                               url=out["url"], labels=decision.labels, tombstone=out["tombstone"])  # fmt: skip

    def current_labels(self, reference: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return log_doors.labels_for(reference.get("scope"))


def caller_for(principal: str) -> Any:
    """A memory consumer's principal as the log's caller: the skill callers' rule (the
    principal, and this process's site scope)."""
    from axiom.infra.site_authority import SiteNotAuthorized
    from axiom.infra.site_scope import resolve

    try:
        scope = resolve()
        sites = None if scope.unbounded else frozenset(scope.sites)
    except SiteNotAuthorized:
        sites = frozenset()
    return log_doors.LogCaller(handle=principal or None, sites=sites)


refs.register(SCHEME, LogResolver())

__all__ = ["DOOR", "SCHEME", "LogResolver", "caller_for", "served"]
