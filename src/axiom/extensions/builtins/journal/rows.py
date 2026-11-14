# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A journal row, and the builders that make one from a record's summary form.

Every builder reads keys, never content: a signed entry's row comes from ``attest.service.brief``
(entry type, title, signer, digest), a skill call's from the audit record's skill and outcome, a
command's from its path and outcome with its arguments dropped. ADR-163's amendment and ADR-241
D6 are enforced here, and LB-J1-A1 checks the stored result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import lanes

#: Payload keys whose string values name the objects a generic event concerns.
_ID_KEYS = ("id", "attestation_id", "draft_id", "schedule_id", "action_id", "receipt_id",
            "candidate", "connector", "name", "branch", "tag", "pr", "item")  # fmt: skip
#: Payload keys that are access labels, not objects the event concerns.
_LABEL_KEYS = ("site_id", "site", "logbook", "tenant_id", "account_id")
#: Payload keys a generic event's display line may quote, first one found.
_LINE_KEYS = ("summary", "message", "reason", "title", "level", "state", "outcome", "status")
#: Keys naming the person a record was asked for or done for: the Timeline's "Mine" reads them
#: from ``ref.for``. Principals only; a row with none of them keeps its shape.
FOR_KEYS = ("requester", "requested_by", "for_principal", "on_behalf_of", "for")


@dataclass(frozen=True)
class Row:
    lane: str
    subject: str
    source: str
    source_id: str
    at: datetime
    status: str
    summary: str
    actor: str | None = None
    subjects: tuple[str, ...] = ()
    ref: dict[str, Any] = field(default_factory=dict)
    link: str | None = None
    site: str | None = None
    logbook: str | None = None
    classification: str = "internal"

    @property
    def retention_class(self) -> str:
        return lanes.retention_class(self.lane)


def when(value: Any, default: datetime | None = None) -> datetime:
    """An aware datetime from a datetime or an ISO string; ``default`` (or now) otherwise."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return default or datetime.now(UTC)


def digest_of(*parts: Any) -> str:
    """A stable key over JSON-able parts, for a source with no key of its own."""
    canonical = json.dumps(list(parts), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:40]


def _principal(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return value if value.startswith("@") else f"@{value}"


def done_for(source: Any) -> list[str]:
    """The principals a record names as asked for or done for, in key order, once each."""
    if not isinstance(source, dict):
        return []
    out: list[str] = []
    for key in FOR_KEYS:
        values = source.get(key)
        for value in values if isinstance(values, (list, tuple)) else (values,):
            handle = _principal(value)
            if handle and handle not in out:
                out.append(handle)
    return out


def _with_for(ref: dict[str, Any], *sources: Any) -> dict[str, Any]:
    found: list[str] = []
    for source in sources:
        found.extend(h for h in done_for(source) if h not in found)
    return {**ref, "for": found} if found else ref


def _text(value: Any, limit: int = 160) -> str:
    s = str(value if value is not None else "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


# -- attestations ------------------------------------------------------------------------


def from_brief(brief: dict[str, Any], *, source: str = "attest_records") -> Row:
    """The reference row for a signed entry, from its brief and nothing else."""
    logbook = str(brief["logbook"])
    attestation_id = str(brief["attestation_id"])
    kind = brief.get("entry_type") or brief.get("meaning") or "entry"
    title = brief.get("title")
    return Row(
        lane="attestations",
        subject=f"attest.{logbook}.signed",
        source=source,
        source_id=attestation_id,
        at=when(brief.get("occurred_at")),
        status="ok",
        summary=f"{kind}: {_text(title)}" if title else str(kind),
        actor=_principal(brief.get("signer")),
        subjects=(attestation_id,),
        ref={
            "logbook": logbook,
            "seq": int(brief["seq"]),
            "attestation_id": attestation_id,
            "digest": str(brief["digest"]),
        },
        link=f"/api/v1/attest/{logbook}/records/{attestation_id}",
        site=brief.get("site_id"),
        logbook=logbook,
    )


def obligation_key(logbook: str, obligation: str, number: Any, due_at: Any, state: str) -> str:
    return f"{logbook}:{obligation}:{number}:{when(due_at).isoformat()}:{state}"


def from_obligation(payload: dict[str, Any], *, logbook: str, state: str, site: str | None,
                    at: Any = None) -> Row:  # fmt: skip
    """An obligation change, from the fact the tick recorded: ids, interval, due time, state."""
    interval = payload.get("interval") or {}
    number = payload.get("interval_number", interval.get("number"))
    kind = interval.get("kind") or "interval"
    obligation = str(payload.get("obligation") or "")
    due_at = payload.get("due_at")
    status = {"warn": "warn", "missed": "bad", "met": "ok"}.get(state, "info")
    word = {"warn": "due soon", "missed": "missed", "met": "now signed"}.get(state, state)
    return Row(
        lane="attestations",
        subject=f"attest.{logbook}.obligation_{state}",
        source="attest_obligations",
        source_id=obligation_key(logbook, obligation, number, due_at, state),
        at=when(at or due_at),
        status=status,
        summary=f"{obligation} {word} in {kind} {number}",
        subjects=(),
        ref={
            "logbook": logbook,
            "obligation": obligation,
            "interval": {"kind": kind, "number": number},
            "due_at": when(due_at).isoformat(),
            "state": state,
        },  # fmt: skip
        site=site,
        logbook=logbook,
    )


def from_draft_created(payload: dict[str, Any]) -> Row:
    logbook = str(payload.get("logbook") or "")
    draft_id = str(payload.get("draft_id") or "")
    return Row(
        lane="attestations",
        subject=f"attest.{logbook}.draft_created",
        source="attest_drafts",
        source_id=draft_id or digest_of(payload),
        at=datetime.now(UTC),
        status="info",
        summary=f"{payload.get('entry_type') or 'entry'} proposed by {payload.get('origin')} "
        f"for {payload.get('for_principal')}",  # fmt: skip
        actor=_principal(payload.get("for_principal")),
        subjects=(draft_id,) if draft_id else (),
        ref=_with_for({"logbook": logbook, "draft_id": draft_id}, payload),
        site=payload.get("site_id"),
        logbook=logbook or None,
    )


# -- the durable readers' rows ------------------------------------------------------------


def from_audit_record(rec: dict[str, Any]) -> Row:
    """A skill call from the action audit chain: who ran what, with what outcome; no params."""
    skill = str(rec.get("skill") or "skill")
    outcome = str(rec.get("outcome") or "")
    seq = int(rec.get("seq") or 0)
    return Row(
        lane="tool_audit",
        subject=lanes.safe_subject("audit", skill),
        source="skill_audit",
        source_id=str(seq),
        at=when(rec.get("ts")),
        status="ok" if outcome == "ok" else ("bad" if outcome else "info"),
        summary=f"{skill} ({outcome or 'ran'})",
        actor=_principal(rec.get("principal")),
        subjects=(),
        ref={"chain": "actions", "seq": seq, "hash": rec.get("hash")},
        link=f"audit://actions/{seq}",
        site=rec.get("site") or None,
    )


def from_ledger_row(rec: dict[str, Any]) -> Row:
    """An agent action from the provenance ledger: agent, operation, candidate, outcome."""
    outcome = str(rec.get("outcome") or "")
    status = {"proceeded": "ok", "refused": "bad", "failed": "bad"}.get(outcome, "info")
    rid = str(rec.get("id") or digest_of(rec))
    candidate = rec.get("candidate")
    return Row(
        lane="tool_audit",
        subject=lanes.safe_subject("ledger", str(rec.get("op_class") or "action")),
        source="action_ledger",
        source_id=rid,
        at=when(rec.get("ts")),
        status=status,
        summary=f"{rec.get('name') or rec.get('op_class')} {_text(candidate, 80)}: {outcome}"
        + (" (dry run)" if rec.get("dry_run") else ""),  # fmt: skip
        actor=_principal(rec.get("agent")),
        subjects=(str(candidate),) if candidate else (),
        ref=_with_for({"id": rid, "op_class": rec.get("op_class")}, _metadata(rec)),
        link=f"ledger://agent_actions/{rid}",
    )


def _metadata(rec: dict[str, Any]) -> dict[str, Any]:
    meta = rec.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return {}
    return meta if isinstance(meta, dict) else {}


def from_program_change(entry: dict[str, Any], *, source_id: str) -> Row:
    kind = str(entry.get("kind") or "change")
    subject = str(entry.get("subject") or "")
    subject_id = entry.get("id") or entry.get("subject_id") or entry.get("item")
    return Row(
        lane="program",
        subject=lanes.safe_subject("program", kind),
        source="program_changelog",
        source_id=source_id,
        at=when(entry.get("at") or entry.get("ts")),
        status=lanes.status_for(f"program.{kind}"),
        summary=f"{kind}: {subject} {subject_id or ''}".strip(),
        actor=_principal(entry.get("by") or entry.get("actor")),
        subjects=(str(subject_id),) if subject_id else (),
        ref={"kind": kind, "subject": subject, "id": subject_id},
    )


# -- live events -------------------------------------------------------------------------


def from_event(subject: str, payload: dict[str, Any] | None, *, source: str = "bus",
               at: Any = None, source_id: str | None = None) -> Row | None:  # fmt: skip
    """The row for a bus event, or None for an event the journal does not index.

    The tool gateway's live events (``tool.>``, ``capability.>``, ``prompt.>``) are not written:
    the action audit chain is their record and a ``tool.post_invoke`` payload carries a whole
    result, which this index must not hold. Catch-up reads the chain instead.
    """
    payload = dict(payload or {})
    lane, _ = lanes.match(subject)
    tokens = subject.split(".")
    if lane == "attestations" and len(tokens) == 3:
        logbook, kind = tokens[1], tokens[2]
        if kind == "signed" and payload.get("attestation_id"):
            return from_brief(payload)
        if kind == "draft_created":
            return from_draft_created(payload)
        if kind.startswith("obligation_"):
            return from_obligation(payload, logbook=logbook, state=kind[len("obligation_"):],
                                   site=payload.get("site_id"), at=at)  # fmt: skip
    if lane == "tool_audit" and tokens[0] in ("tool", "capability", "prompt"):
        return None
    if lane == "commands" and tokens[0] == "cli":
        return _from_cli_event(subject, payload, source=source, at=at, source_id=source_id)
    return _generic(subject, payload, lane=lane, source=source, at=at, source_id=source_id)


def _from_cli_event(subject: str, payload: dict[str, Any], *, source: str, at: Any,
                    source_id: str | None) -> Row:  # fmt: skip
    """A command a person gave the node: its path and outcome, never its arguments' values."""
    path = str(payload.get("command_path") or payload.get("command") or "")
    outcome = payload.get("exit_code", payload.get("outcome"))
    word = subject.rsplit(".", 1)[-1].replace("_", " ")
    line = f"{path} {word}".strip()
    if outcome is not None:
        line += f" (exit {outcome})" if isinstance(outcome, int) else f" ({outcome})"
    key = source_id or digest_of(subject, path, payload.get("timestamp") or at or "", outcome)
    status = lanes.status_for(subject, payload)
    if isinstance(outcome, int):
        status = "ok" if outcome == 0 else "bad"
    return Row(
        lane="commands",
        subject=subject,
        source=source,
        source_id=key,
        at=when(at or payload.get("timestamp")),
        status=status,
        summary=line or subject,
        actor=_principal(payload.get("principal")),
        subjects=(),
        ref={"command": path} if path else {},
    )


def _generic(subject: str, payload: dict[str, Any], *, lane: str, source: str, at: Any,
             source_id: str | None) -> Row:  # fmt: skip
    """Any other event: the subject, a short line, the ids it names. No payload."""
    ids = tuple(
        str(v)
        for k, v in payload.items()
        if (k == "id" or k.endswith("_id") or k in _ID_KEYS)
        and k not in _LABEL_KEYS
        and isinstance(v, (str, int))
        and str(v)
    )
    line = next((f"{subject}: {_text(payload[k])}" for k in _LINE_KEYS
                 if isinstance(payload.get(k), (str, int))), subject)  # fmt: skip
    own = payload.get("id") or payload.get("event_id")
    stamp = payload.get("ts") or payload.get("timestamp") or payload.get("at")
    key = source_id or (f"{subject}:{own}" if own else digest_of(subject, stamp, payload))
    site = payload.get("site_id") or payload.get("site")
    return Row(
        lane=lane,
        subject=subject,
        source=source,
        source_id=str(key)[:160],
        at=when(at or stamp),
        status=lanes.status_for(subject, payload),
        summary=line,
        actor=_principal(payload.get("principal") or payload.get("actor")),
        subjects=ids[:8],
        ref=_with_for({}, payload),
        site=site if isinstance(site, str) and site else None,
        classification=lanes.classification_of(payload.get("classification")),
    )


__all__ = [
    "Row",
    "FOR_KEYS",
    "digest_of",
    "done_for",
    "from_audit_record",
    "from_brief",
    "from_draft_created",
    "from_event",
    "from_ledger_row",
    "from_obligation",
    "from_program_change",
    "obligation_key",
    "when",
]
