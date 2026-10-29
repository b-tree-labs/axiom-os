# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""How fresh the program tracker is, earned rather than asserted.

The clerk fires on a heartbeat, and "it fired" is not "it is current": a sync
whose feeder was skipped, whose read failed, or whose state did not land fires
just as cheerfully as one that worked. So the tracker's "as of" stamp is not the
clock and not the last attempt. It is ``last_verified_sync_at``, and it moves in
exactly one case: a sync that

1. ran against every live feeder the program declares (none skipped, none
   refused, none that failed mid-read),
2. read each of them to completion (a feeder that cannot tell "the read
   failed" from "nothing changed" does not count; see ``TrackerReadError``),
3. saved the data file and the change-detection snapshot, and
4. read both back and found them intact.

Every other outcome (skipped, refused, raised, partial, self-reconcile only)
is recorded as an attempt with its reason and leaves the stamp where it was.

``assess`` turns the record into the machine-readable block every consumer
reads. A consumer never computes freshness from a timestamp itself; it renders
``state``. The states:

``fresh``  a verified sync completed within ``stale_after_s``.
``stale``  the last verified sync is older than ``stale_after_s``. Carries
           ``stale_since`` (the instant it crossed the line) and ``reason``:
           the last failed attempt's reason if the clerk has been failing, or
           "no sync attempted since ..." if it has gone silent. The silent
           case is the one a clerk cannot report about itself, which is why
           the record lives on disk and the check is a pure function of it,
           runnable by something that is not the clerk.
``never``  no verified sync has ever completed.
``unfed``  syncs have run but only as a self-reconcile, which verifies nothing
           against a tracker; there is no live feeder to be fresh *from*.

The window is two heartbeats: one missed or failed beat is tolerated (a slow
host, one transient GitLab error), two in a row is not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.state import LockedJsonFile

#: The clerk's heartbeat, in seconds. Mirrors ``[agent] heartbeat_interval`` in
#: the manifest; a test holds the two equal so they cannot drift apart.
DEFAULT_INTERVAL_S = 900

#: Stale once this many heartbeats have passed without a verified sync.
STALE_FACTOR = 2

FILE_NAME = "freshness.json"

#: How many characters of a failure reason are kept. The reason is shown to
#: people and to dashboards; it must never be a vehicle for a response body.
_REASON_MAX = 300


def freshness_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "program" / FILE_NAME


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_record(state_dir: Path | str) -> dict[str, Any]:
    """The persisted record, or an empty dict when none exists or it is unreadable."""
    path = freshness_path(state_dir)
    if not path.exists():
        return {}
    try:
        with LockedJsonFile(path) as handle:
            record = handle.read()
    except Exception:  # noqa: BLE001 - an unreadable record is "no evidence", never fresh
        return {}
    return record if isinstance(record, dict) else {}


def record_attempt(
    state_dir: Path | str,
    *,
    verified: bool,
    live: bool,
    feeders: list[str],
    reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one sync attempt. ``verified`` advances the stamp; nothing else does.

    ``verified`` is the caller's claim that all four conditions in the module
    docstring held. ``live`` says whether any live feeder was involved: a
    self-reconcile can be perfectly healthy and still earns no stamp.
    """
    moment = now or datetime.now(UTC)
    stamp = _iso(moment)
    path = freshness_path(state_dir)
    with LockedJsonFile(path, exclusive=True) as handle:
        record = handle.read()
        if not isinstance(record, dict):
            record = {}
        record["last_attempt_at"] = stamp
        record["feeders"] = list(feeders)
        record["live"] = bool(live)
        if verified and live:
            record["last_verified_sync_at"] = stamp
            record["last_attempt_ok"] = True
            record["consecutive_failures"] = 0
        elif live:
            record["last_attempt_ok"] = False
            record["consecutive_failures"] = int(record.get("consecutive_failures") or 0) + 1
            record["last_failure"] = {
                "at": stamp,
                "reason": (reason or "sync did not verify")[:_REASON_MAX],
            }
        else:
            # A self-reconcile: neither a success nor a failure of the feed.
            record["last_attempt_ok"] = None
            record["self_only_at"] = stamp
        handle.write(record)
    return record


def thresholds(program: dict[str, Any] | None) -> tuple[int, int]:
    """``(interval_s, stale_after_s)``: defaults, overridable in ``program.freshness``."""
    declared = (program or {}).get("freshness")
    interval = DEFAULT_INTERVAL_S
    stale_after: int | None = None
    if isinstance(declared, dict):
        raw_interval = declared.get("interval_s")
        if (
            isinstance(raw_interval, int)
            and not isinstance(raw_interval, bool)
            and raw_interval > 0
        ):
            interval = raw_interval
        raw_stale = declared.get("stale_after_s")
        if isinstance(raw_stale, int) and not isinstance(raw_stale, bool) and raw_stale > 0:
            stale_after = raw_stale
    return interval, stale_after if stale_after is not None else interval * STALE_FACTOR


def assess(
    record: dict[str, Any] | None,
    *,
    program: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The freshness block. A pure function of ``record`` and ``now``."""
    moment = now or datetime.now(UTC)
    record = record or {}
    interval, stale_after = thresholds(program)
    verified_at = _parse(record.get("last_verified_sync_at"))
    attempt_at = _parse(record.get("last_attempt_at"))
    failure = record.get("last_failure") if isinstance(record.get("last_failure"), dict) else None

    block: dict[str, Any] = {
        "state": "never",
        "fresh": False,
        "as_of": _iso(verified_at) if verified_at else None,
        "age_s": None,
        "interval_s": interval,
        "stale_after_s": stale_after,
        "stale_since": None,
        "reason": None,
        "last_attempt_at": _iso(attempt_at) if attempt_at else None,
        "last_attempt_ok": record.get("last_attempt_ok"),
        "consecutive_failures": int(record.get("consecutive_failures") or 0),
        "last_failure": failure,
        "feeders": list(record.get("feeders") or []),
        "checked_at": _iso(moment),
    }

    if verified_at is None:
        if record and record.get("live") is False:
            block["state"] = "unfed"
            block["reason"] = (
                "syncs ran as a self-reconcile only; no live feeder is declared, so nothing was verified against a tracker"
            )
        elif failure:
            block["reason"] = (
                f"no verified sync has ever completed; last attempt failed: {failure.get('reason')}"
            )
        elif attempt_at:
            block["reason"] = "no verified sync has ever completed"
        else:
            block["reason"] = "the clerk has never run a sync"
        return block

    age = (moment - verified_at).total_seconds()
    block["age_s"] = max(0, int(age))
    if age <= stale_after:
        block["state"] = "fresh"
        block["fresh"] = True
        return block

    block["state"] = "stale"
    block["stale_since"] = _iso(
        datetime.fromtimestamp(verified_at.timestamp() + stale_after, tz=UTC)
    )
    failed_since_verified = (
        failure is not None
        and (_parse(failure.get("at")) or verified_at) >= verified_at
        and record.get("last_attempt_ok") is False
    )
    if failed_since_verified and failure is not None:
        block["reason"] = (
            f"{block['consecutive_failures']} consecutive sync(s) failed to verify; "
            f"last: {failure.get('reason')}"
        )
    elif attempt_at is not None and attempt_at <= verified_at:
        block["reason"] = (
            f"no sync has been attempted since {_iso(attempt_at)}; the clerk is not running"
        )
    elif attempt_at is not None:
        block["reason"] = (
            f"no verified sync since {_iso(verified_at)}; last attempt {_iso(attempt_at)}"
        )
    else:
        block["reason"] = f"no verified sync since {_iso(verified_at)}"
    return block


def current(state_dir: Path | str, program: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the record and assess it now."""
    return assess(read_record(state_dir), program=program)


__all__ = [
    "DEFAULT_INTERVAL_S",
    "FILE_NAME",
    "STALE_FACTOR",
    "assess",
    "current",
    "freshness_path",
    "read_record",
    "record_attempt",
    "thresholds",
]
