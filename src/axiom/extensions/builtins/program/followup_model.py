# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Follow-ups: the pure rules (ADR-196). No I/O, no clock of its own.

Everything here is a function of its arguments so the state machine, the due
parser, the ladder and the secret guard are testable without a database.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .model import _PRINCIPAL_RE

STATES = ("open", "due", "acknowledged", "snoozed", "done", "expired")
#: No further reminders and no scheduler row.
TERMINAL = ("done",)
#: States that still count as owed: seen in "what do I owe".
OWED = ("open", "due", "acknowledged", "snoozed", "expired")
#: States the ladder is running or waiting in.
ACTIVE = ("open", "due", "snoozed")

AUDIENCES = ("owner", "requester", "operator")
KINDS = ("reminder", "flag", "contact_the_owner")

WHAT_MAX = 500
COMMAND_MAX = 500
HORIZON_DAYS = 366
EXPIRY_AFTER_DUE = timedelta(days=7)
#: A follow-up this far past due with no delivery is reported missed.
MISSED_GRACE = timedelta(minutes=3)

#: The default ladder, measured from the due time (ADR-196 D3).
DEFAULT_LADDER: tuple[dict[str, Any], ...] = (
    {"after_s": 0, "audience": "owner", "kind": "reminder"},
    {"after_s": 6 * 3600, "audience": "owner", "kind": "reminder"},
    {"after_s": 24 * 3600, "audience": "requester", "kind": "flag"},
    {"after_s": 48 * 3600, "audience": "operator", "kind": "contact_the_owner"},
)

_DURATION = re.compile(
    r"^(?:in\s+)?(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|wks?|weeks?)$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}

#: Keys that may not appear in a check's arguments, whatever the skill declares.
_SECRETISH_KEY = re.compile(r"(secret|passw|token|credential|api[_-]?key|private[_-]?key)", re.IGNORECASE)


class FollowupError(ValueError):
    """A request the rules refuse; ``fix`` says how to make it acceptable."""

    def __init__(self, message: str, *, fix: str = "", kind: str = "bad_request") -> None:
        super().__init__(message if not fix else f"{message} Fix: {fix}")
        self.kind = kind
        self.fix = fix


def aware(dt: datetime | None) -> datetime | None:
    """Coerce a stored timestamp to UTC-aware (SQLite drops the zone)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def parse_due(text: str, *, now: datetime) -> datetime:
    """A due time from ``30m`` / ``24h`` / ``in 2 days`` or an ISO-8601 timestamp.

    A timestamp with no zone is read as UTC; the caller echoes the resolved
    time back so a wrong guess is visible. A time already past (beyond a short
    grace) and a time beyond the horizon are refused.
    """
    raw = (text or "").strip()
    if not raw:
        raise FollowupError("a due time is required.", fix="pass due='24h' or an ISO-8601 timestamp")
    m = _DURATION.match(raw)
    if m:
        qty = float(m.group(1))
        unit = _UNIT_SECONDS[m.group(2)[0].lower()]
        due = now + timedelta(seconds=qty * unit)
    else:
        try:
            due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise FollowupError(
                f"cannot read a due time from {raw!r}.",
                fix="use a duration such as 30m, 24h, 2d, 1w, or an ISO-8601 timestamp such as 2026-10-21T09:00:00-05:00",
            ) from None
        due = aware(due)  # type: ignore[assignment]
    if due < now - timedelta(minutes=1):
        raise FollowupError(f"due time {due.isoformat()} is in the past.", fix="pass a future time or a duration")
    if due > now + timedelta(days=HORIZON_DAYS):
        raise FollowupError(f"due time {due.isoformat()} is more than {HORIZON_DAYS} days out.", fix="pick a nearer date")
    return due


def valid_principal(handle: str) -> bool:
    return bool(handle) and bool(_PRINCIPAL_RE.match(handle))


def need_principal(handle: str, what: str) -> str:
    handle = (handle or "").strip()
    if not valid_principal(handle):
        raise FollowupError(f"{what} {handle!r} is not a principal.", fix="use the form @name or @name:context")
    return handle


def secret_findings(fields: Mapping[str, Any]) -> list[str]:
    """Names of fields that look like they hold a credential. Never the values."""
    from axiom.infra.secret_scan import scan_text

    bad: list[str] = []
    for name, value in _flatten(fields):
        if value and scan_text(str(value)):
            bad.append(name)
    return bad


def _flatten(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def scrub_text(text: str) -> tuple[str, int]:
    """Replace credential-format matches with a typed marker. Returns (text, count)."""
    from axiom.infra.secret_scan import SECRET_PATTERNS

    n = 0
    for name, pattern in SECRET_PATTERNS:
        text, k = pattern.subn(f"[redacted:{name}]", text)
        n += k
    return text, n


def secretish_key(key: Any) -> bool:
    return bool(_SECRETISH_KEY.search(str(key)))


def refuse_secrets(fields: Mapping[str, Any]) -> None:
    bad = secret_findings(fields)
    if bad:
        raise FollowupError(
            f"{', '.join(sorted(bad))} look like a credential value.",
            fix="a follow-up names a secret, it never holds one; say 'the rotation key' and let the check read it from the vault",
        )


def refuse_secretish_keys(args: Mapping[str, Any] | None) -> None:
    for key in (args or {}):
        if _SECRETISH_KEY.search(str(key)):
            raise FollowupError(
                f"check argument {key!r} looks like it carries a credential.",
                fix="a check takes identifiers, never secret values; have the skill resolve credentials itself",
            )


def clean_text(value: Any, *, name: str, limit: int, required: bool = False) -> str | None:
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        if required:
            raise FollowupError(f"{name} is required.")
        return None
    if len(text) > limit:
        raise FollowupError(f"{name} is {len(text)} characters; the limit is {limit}.")
    return text


def validate_ladder(ladder: Any) -> list[dict[str, Any]]:
    if ladder is None:
        return [dict(r) for r in DEFAULT_LADDER]
    if not isinstance(ladder, list) or not ladder:
        raise FollowupError("ladder must be a non-empty list of rungs.")
    out: list[dict[str, Any]] = []
    last = -1.0
    for r in ladder:
        if not isinstance(r, Mapping):
            raise FollowupError("each rung must be an object with after_s, audience, kind.")
        after = float(r.get("after_s", -1))
        audience, kind = r.get("audience"), r.get("kind", "reminder")
        if after < 0 or after < last:
            raise FollowupError("rungs must have non-decreasing after_s >= 0.")
        if audience not in AUDIENCES:
            raise FollowupError(f"rung audience {audience!r} must be one of {', '.join(AUDIENCES)}.")
        if kind not in KINDS:
            raise FollowupError(f"rung kind {kind!r} must be one of {', '.join(KINDS)}.")
        last = after
        out.append({"after_s": int(after), "audience": audience, "kind": kind})
    return out


def rung_time(due_at: datetime, ladder: list[dict[str, Any]], rung: int) -> datetime | None:
    """When rung ``rung`` is due, or None past the end of the ladder."""
    if rung < 0 or rung >= len(ladder):
        return None
    return aware(due_at) + timedelta(seconds=ladder[rung]["after_s"])  # type: ignore[operator]


def visible_to(row: Mapping[str, Any], principal: str) -> bool:
    return principal in (row.get("owner"), row.get("created_by")) or principal in (row.get("shared_with") or [])


def is_missed(row: Mapping[str, Any], *, now: datetime, delivered_this_cycle: bool) -> bool:
    """Past due, still active or expired unanswered, and nothing was ever delivered."""
    if row["state"] == "done" or delivered_this_cycle:
        return False
    return now - aware(row["due_at"]) > MISSED_GRACE  # type: ignore[operator]


def next_command_hint(row: Mapping[str, Any]) -> str:
    """The exact commands for the person, by follow-up id."""
    fid = row["id"]
    return (
        f"ack: axi program followup ack --id {fid}; "
        f"snooze: axi program followup snooze --id {fid} --by 24h; "
        f"close: axi program followup done --id {fid} --note '<what you found>'"
    )


__all__ = [
    "ACTIVE",
    "AUDIENCES",
    "DEFAULT_LADDER",
    "EXPIRY_AFTER_DUE",
    "KINDS",
    "OWED",
    "STATES",
    "TERMINAL",
    "FollowupError",
    "aware",
    "clean_text",
    "is_missed",
    "need_principal",
    "next_command_hint",
    "parse_due",
    "refuse_secretish_keys",
    "refuse_secrets",
    "scrub_text",
    "secretish_key",
    "rung_time",
    "validate_ladder",
    "valid_principal",
    "visible_to",
]
