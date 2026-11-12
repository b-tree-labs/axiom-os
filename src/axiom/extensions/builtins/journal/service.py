# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Query, lanes and verify: the read side every surface calls, with the caller's rights applied.

Paging is newest first by ``seq`` with an opaque cursor (the last sequence examined), so rows
written during paging, which have higher sequence numbers, never reorder or repeat what was
returned. ``verify`` compares the journal's references with the records they point at and
reports; it writes to neither.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text

from . import lanes as lane_vocab
from . import rights, store, writer
from .rights import Caller

MAX_LIMIT = 500

# Every statement is a literal: filters are bound parameters, each optional predicate switched by
# its own parameter, so no identifier or value is ever interpolated (the Semgrep rule
# avoid-sqlalchemy-text). ``lanes`` and ``sites`` bind as arrays; a caller with no site bound
# passes ``unbounded``.
_ROWS = """
SELECT * FROM journal_events
WHERE lane = ANY(CAST(:lanes AS text[]))
  AND (CAST(:unbounded AS boolean) OR site IS NULL OR site = ANY(CAST(:sites AS text[])))
  AND (CAST(:subject AS text) IS NULL OR subject = :subject OR subject LIKE :subject_prefix)
  AND (CAST(:n_subjects AS integer) = 0 OR subjects @> CAST(:subjects AS jsonb))
  AND (CAST(:since AS timestamptz) IS NULL OR at >= :since)
  AND (CAST(:until AS timestamptz) IS NULL OR at <= :until)
  AND (CAST(:cursor AS bigint) IS NULL OR seq < :cursor)
ORDER BY seq DESC
LIMIT :limit
"""
_COUNTS = """
SELECT lane, count(*) FROM journal_events
WHERE lane = ANY(CAST(:lanes AS text[]))
  AND (CAST(:unbounded AS boolean) OR site IS NULL OR site = ANY(CAST(:sites AS text[])))
  AND (CAST(:above AS boolean) = false OR classification NOT IN ('public', 'internal'))
GROUP BY lane
"""
_LANE_ROWS = """
SELECT * FROM journal_events
WHERE lane = :lane
  AND (CAST(:unbounded AS boolean) OR site IS NULL OR site = ANY(CAST(:sites AS text[])))
ORDER BY seq DESC
LIMIT 5000
"""
_PLATFORM_SUBJECTS = """
SELECT subject, count(*) FROM journal_events
WHERE lane = 'platform'
  AND (CAST(:unbounded AS boolean) OR site IS NULL OR site = ANY(CAST(:sites AS text[])))
GROUP BY subject
"""
_BY_SOURCE = "SELECT * FROM journal_events WHERE source = :source ORDER BY seq DESC LIMIT :limit"


def _when(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    from axiom.policy.action_ledger import parse_when

    return parse_when(value)


def _lanes_param(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = [v for v in value.split(",") if v.strip()]
    return [lane_vocab.check_lane(str(v).strip()) for v in value]


def _subjects_param(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def _scope(caller: Caller) -> dict[str, Any]:
    """The site predicate a caller cannot remove: node-wide rows, plus their own sites."""
    return {"unbounded": caller.sites is None, "sites": sorted(caller.sites or ())}


def query(caller: Caller, *, lanes: Any = None, subjects: Any = None, subject: str | None = None,
          since: Any = None, until: Any = None, limit: int = 50, cursor: Any = None) -> dict[str, Any]:  # fmt: skip
    """Rows the caller may see, newest first, with hidden counts per lane and the next cursor."""
    limit = max(1, min(int(limit or 50), MAX_LIMIT))
    lane_list = rights.allowed_lanes(caller, _lanes_param(lanes))
    out: dict[str, Any] = {"rows": [], "hidden": {}, "lanes": lane_list, "next_cursor": None}
    if not lane_list:
        return out
    position = int(cursor) if cursor not in (None, "") else None
    since_at, until_at = _when(since), _when(until)
    subject_ids = _subjects_param(subjects)
    hidden: dict[str, int] = {}
    visible: list[dict[str, Any]] = []
    last_seen: int | None = None
    exhausted = False
    params: dict[str, Any] = {
        **_scope(caller),
        "lanes": lane_list,
        "subject": subject or None,
        "subject_prefix": (subject + ".%") if subject else None,
        "n_subjects": len(subject_ids),
        "subjects": json.dumps(subject_ids),
        "since": since_at,
        "until": until_at,
        "limit": limit + 1,
    }
    with store.session_scope() as s:
        for _ in range(20):  # a bounded number of pages of hidden rows
            params["cursor"] = position
            fetched = s.execute(text(_ROWS), params).mappings().all()
            more = len(fetched) > limit
            for m in fetched[:limit]:
                row = writer.row_json(m)
                last_seen = row["seq"]
                ok, reason = rights.visible(row, caller)
                if ok:
                    visible.append(row)
                    if len(visible) >= limit:
                        break
                elif reason in ("classification", "resolver"):
                    hidden[row["lane"]] = hidden.get(row["lane"], 0) + 1
            if len(visible) >= limit:
                exhausted = not (more or len(fetched) > len(visible))
                break
            if not more:
                exhausted = True
                break
            position = last_seen
    out["rows"] = visible
    out["hidden"] = hidden
    if visible and not exhausted:
        out["next_cursor"] = str(last_seen)
    elif visible and len(visible) >= limit:
        out["next_cursor"] = str(last_seen) if not exhausted else None
    return out


def lanes_report(caller: Caller) -> dict[str, Any]:
    """Lanes the caller may see, with retention, durability, counts, hidden counts and the
    subjects no declared pattern matched."""
    allowed = rights.allowed_lanes(caller)
    out: list[dict[str, Any]] = []
    unmapped: dict[str, int] = {}
    if not allowed:
        return {"lanes": [], "unmapped": {}}
    with store.session_scope() as s:
        scope = _scope(caller)
        counts = {
            lane: int(n)
            for lane, n in s.execute(
                text(_COUNTS), {**scope, "lanes": allowed, "above": False}
            ).all()
        }
        above = (
            {
                lane: int(n)
                for lane, n in s.execute(
                    text(_COUNTS), {**scope, "lanes": allowed, "above": True}
                ).all()
            }
            if caller.ceiling == "internal"
            else {}
        )
        resolved_hidden: dict[str, int] = {}
        for lane in allowed:
            if rights.has_row_resolver(lane):
                fetched = s.execute(text(_LANE_ROWS), {**scope, "lane": lane}).mappings().all()
                resolved_hidden[lane] = sum(
                    1
                    for m in fetched
                    if rights.visible(writer.row_json(m), caller)[1] == "resolver"
                )
        if "platform" in allowed:
            for subject, n in s.execute(text(_PLATFORM_SUBJECTS), scope).all():
                if lane_vocab.match(subject)[1] is None:
                    unmapped[subject] = int(n)
    for lane in allowed:
        out.append(
            {
                "lane": lane,
                "retention": lane_vocab.retention(lane),
                "durable": lane_vocab.durable(lane),
                "patterns": lane_vocab.patterns_for(lane),
                "count": counts.get(lane, 0),
                "hidden_count": above.get(lane, 0) + resolved_hidden.get(lane, 0),
            }
        )
    return {"lanes": out, "unmapped": unmapped}


def lanes(caller: Caller) -> dict[str, Any]:
    """The ticket's name for :func:`lanes_report`."""
    return lanes_report(caller)


def verify(*, limit: int = 5000, state_dir: Any = None) -> dict[str, Any]:
    """Journal references against the records they point at. Reports; changes nothing."""
    from . import ingest

    mismatches: list[dict[str, Any]] = []
    checked = 0
    with store.session_scope() as s:
        attest_rows = (
            s.execute(text(_BY_SOURCE), {"source": "attest_records", "limit": limit})
            .mappings()
            .all()
        )
        audit_rows = (
            s.execute(text(_BY_SOURCE), {"source": "skill_audit", "limit": limit}).mappings().all()
        )
    if attest_rows:
        from axiom.extensions.builtins.attest import service as attest

        for m in attest_rows:
            ref = m["ref"] or {}
            checked += 1
            rec = attest.record(str(ref.get("attestation_id")))
            reason = None
            if rec is None:
                reason = "missing"
            elif rec.get("digest") != ref.get("digest"):
                reason = "digest"
            elif int(rec.get("seq") or 0) != int(ref.get("seq") or -1) or rec.get(
                "logbook"
            ) != ref.get("logbook"):
                reason = "seq"
            if reason:
                mismatches.append({"seq": m["seq"], "lane": "attestations", "attestation_id": ref.get("attestation_id"),
                                   "logbook": ref.get("logbook"), "reason": reason})  # fmt: skip
    if audit_rows:
        from axiom.infra import audit_trail

        where = state_dir or ingest.current().state_dir
        if where is None:
            from axiom.infra.paths import get_user_state_dir

            where = get_user_state_dir()
        chain = {
            int(r.get("seq") or 0): r for r in audit_trail.read_chain(audit_trail.audit_path(where))
        }
        for m in audit_rows:
            ref = m["ref"] or {}
            checked += 1
            rec = chain.get(int(ref.get("seq") or 0))
            if rec is None:
                mismatches.append(
                    {
                        "seq": m["seq"],
                        "lane": "tool_audit",
                        "chain_seq": ref.get("seq"),
                        "reason": "missing",
                    }
                )
            elif rec.get("hash") != ref.get("hash"):
                mismatches.append(
                    {
                        "seq": m["seq"],
                        "lane": "tool_audit",
                        "chain_seq": ref.get("seq"),
                        "reason": "hash",
                    }
                )
    return {
        "ok": not mismatches,
        "checked": checked,
        "mismatches": mismatches,
        "lag": ingest.current().lag(),
        "live_failures": ingest.current().failures(),
        "last_error": ingest.current().last_error,
    }


__all__ = ["MAX_LIMIT", "lanes", "lanes_report", "query", "verify"]
