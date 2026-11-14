# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rolling backup history of the program ``data.json`` — retention + restore.

Defense-in-depth on top of the atomic write in :func:`..model.save_program`.
The atomic write prevents a *torn* file at write time; this module keeps a
rolling history of known-good prior states so the program can be rolled back
after any corruption, bad write, or unwanted autonomous change.

This history is **separate** from the change-detection ``snapshot.json`` that
:mod:`.skills._changelog` owns (the per-item diff memory ``sync`` advances).
The two never touch: this module only ever writes under ``<state>/program/
snapshots/`` and reads nothing else.

Layout under the program state dir::

    <state>/program/
      data.json            # the authoritative current state (model.py)
      snapshot.json         # the change-detection diff memory (_changelog.py)
      snapshots/            # THIS module's rolling backup history
        data-20261008T120000.000000Z.json
        data-20261008T131500.000000Z.json
        ...

Each backup is a byte-for-byte copy of the ``data.json`` payload at write
time, named by the UTC instant it was taken (ISO-8601 *basic* form, no colons,
so the name is filesystem-safe everywhere and sorts chronologically).

The module is domain-agnostic: it copies and prunes an opaque JSON payload and
counts the schema's own top-level lists for a human-readable summary; it never
interprets a value.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: A backup file is ``data-<stamp>.json``; a rare same-microsecond collision
#: appends ``-<n>`` before the suffix (``data-<stamp>-1.json``).
SNAPSHOT_PREFIX = "data-"
SNAPSHOT_SUFFIX = ".json"

#: The stamp format: ISO-8601 *basic* UTC with microseconds, no colons. Fixed
#: width, so the filename sorts chronologically as plain text.
_STAMP_FMT = "%Y%m%dT%H%M%S.%fZ"
_STAMP_RE = re.compile(r"^\d{8}T\d{6}\.\d{6}Z$")

# ---- configuration --------------------------------------------------------

#: ``off`` / ``0`` / ``false`` / ``no`` disables the rolling backup entirely
#: (the authoritative write is unaffected). Default: on.
_ENABLE_ENV = "AXIOM_PROGRAM_SNAPSHOTS"
_RECENT_ENV = "AXIOM_PROGRAM_SNAPSHOT_RECENT_HOURS"
_DAILY_ENV = "AXIOM_PROGRAM_SNAPSHOT_DAILY_DAYS"
_WEEKLY_ENV = "AXIOM_PROGRAM_SNAPSHOT_WEEKLY_WEEKS"


@dataclass(frozen=True)
class RetentionPolicy:
    """A bounded, tiered retention policy (grandfather-father-son).

    The prudent default keeps:

    - **every** backup from the last ``recent_hours`` (24h) — full recent
      granularity, so a corruption spotted today rolls back to minutes ago;
    - **one per day** for the last ``daily_days`` (14d) — the newest of each
      UTC calendar day;
    - **one per week** for the last ``weekly_weeks`` (8w) — the newest of each
      ISO week.

    Anything older than the weekly window is pruned. The kept set is therefore
    bounded by ``(backups in the recent window) + daily_days + weekly_weeks``,
    and backups are KB-sized, so the directory stays small.

    Configure per-field via the environment (:meth:`from_env`) or construct
    one directly (tests do).
    """

    recent_hours: int = 24
    daily_days: int = 14
    weekly_weeks: int = 8

    @classmethod
    def from_env(cls) -> RetentionPolicy:
        """Read the policy from ``AXIOM_PROGRAM_SNAPSHOT_*``; a missing or
        malformed value falls back to the field default (a bad env var must
        never break the authoritative write this rides)."""
        return cls(
            recent_hours=_env_int(_RECENT_ENV, cls.recent_hours),
            daily_days=_env_int(_DAILY_ENV, cls.daily_days),
            weekly_weeks=_env_int(_WEEKLY_ENV, cls.weekly_weeks),
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return value if value >= 0 else default


def snapshots_enabled() -> bool:
    """Whether the rolling backup is on (default: yes)."""
    raw = os.environ.get(_ENABLE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "off", "false", "no", "")


# ---- paths / stamps -------------------------------------------------------


def snapshots_dir_for(data_path: str | Path) -> Path:
    """The backup directory that sits beside a ``data.json`` file."""
    return Path(data_path).parent / "snapshots"


def snapshot_stamp(now: datetime) -> str:
    """The stamp string for an instant (UTC, basic ISO-8601, microseconds)."""
    return now.astimezone(UTC).strftime(_STAMP_FMT)


def parse_stamp(name: str) -> datetime | None:
    """The UTC instant a backup filename encodes, or ``None`` if it is not one
    of ours (so an unrecognized file is never selected for pruning)."""
    if not name.startswith(SNAPSHOT_PREFIX) or not name.endswith(SNAPSHOT_SUFFIX):
        return None
    core = name[len(SNAPSHOT_PREFIX) : -len(SNAPSHOT_SUFFIX)]
    stamp = core.split("-", 1)[0]  # strip a collision suffix, if any
    if not _STAMP_RE.match(stamp):
        return None
    try:
        return datetime.strptime(stamp, _STAMP_FMT).replace(tzinfo=UTC)
    except ValueError:
        return None


# ---- listing --------------------------------------------------------------


def list_stamped(snapshots_dir: str | Path) -> list[tuple[Path, datetime]]:
    """Every recognized backup in the directory, newest first.

    Unrecognized files (and a missing directory) are skipped, never raised —
    this read must work when the live ``data.json`` is unreadable, so it
    touches only the backups.
    """
    directory = Path(snapshots_dir)
    out: list[tuple[Path, datetime]] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        if not path.is_file():
            continue
        stamp = parse_stamp(path.name)
        if stamp is not None:
            out.append((path, stamp))
    out.sort(key=lambda pair: pair[1], reverse=True)
    return out


def summarize(path: str | Path) -> dict[str, Any]:
    """A short, human-readable summary of one backup: people / items / lane
    counts and the program id/name, or ``readable=False`` when the file does
    not parse. Never raises."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"readable": False}
    if not isinstance(raw, dict):
        return {"readable": False}
    program = raw.get("program") if isinstance(raw.get("program"), dict) else {}
    return {
        "readable": True,
        "program_id": program.get("id"),
        "program_name": program.get("name"),
        "people": _count(raw.get("people")),
        "items": _count(raw.get("schedule")),
        "lanes": _count(raw.get("lanes")),
    }


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


# ---- retention selection (pure) -------------------------------------------


def select_kept(
    stamps: list[datetime], now: datetime, policy: RetentionPolicy
) -> set[datetime]:
    """The subset of ``stamps`` the policy keeps — a pure function over the
    instants, so the retention rule is testable without any filesystem.

    Union of three tiers (see :class:`RetentionPolicy`). As a safety floor the
    single newest backup is always kept, so retention never leaves zero
    backups even under an aggressive (e.g. all-zero) policy.
    """
    kept: set[datetime] = set()
    if not stamps:
        return kept

    recent_cutoff = now - timedelta(hours=policy.recent_hours)
    daily_cutoff = now - timedelta(days=policy.daily_days)
    weekly_cutoff = now - timedelta(weeks=policy.weekly_weeks)

    # Tier 1 — keep everything in the recent window.
    for ts in stamps:
        if ts >= recent_cutoff:
            kept.add(ts)

    # Tier 2 — the newest backup of each UTC calendar day in the daily window.
    daily: dict[Any, datetime] = {}
    for ts in stamps:
        if ts >= daily_cutoff:
            key = ts.astimezone(UTC).date()
            if key not in daily or ts > daily[key]:
                daily[key] = ts
    kept.update(daily.values())

    # Tier 3 — the newest backup of each ISO week in the weekly window.
    weekly: dict[Any, datetime] = {}
    for ts in stamps:
        if ts >= weekly_cutoff:
            iso = ts.astimezone(UTC).isocalendar()
            key = (iso.year, iso.week)
            if key not in weekly or ts > weekly[key]:
                weekly[key] = ts
    kept.update(weekly.values())

    # Safety floor: never prune the single most-recent known-good state.
    kept.add(max(stamps))
    return kept


# ---- atomic write + prune -------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: a temp file in the same
    directory, flushed and fsynced, then ``os.replace``-d into place — a
    backup is only ever complete-old or complete-new, never torn.

    Mirrors the atomic replace in :func:`..model.save_program` (the same
    discipline that module documents as mirroring ``axiom.infra.state``'s
    ``LockedJsonFile.write``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unique_path(snapshots_dir: Path, stamp: str) -> Path:
    base = snapshots_dir / f"{SNAPSHOT_PREFIX}{stamp}{SNAPSHOT_SUFFIX}"
    if not base.exists():
        return base
    i = 1
    while True:
        candidate = snapshots_dir / f"{SNAPSHOT_PREFIX}{stamp}-{i}{SNAPSHOT_SUFFIX}"
        if not candidate.exists():
            return candidate
        i += 1


def prune(
    snapshots_dir: str | Path, now: datetime, policy: RetentionPolicy
) -> list[Path]:
    """Delete recognized backups the policy does not keep. Returns the paths
    removed. Best-effort and concurrency-tolerant: a file already gone (a
    racing prune) is not an error, and an unrecognized file is left alone."""
    stamped = list_stamped(snapshots_dir)
    if not stamped:
        return []
    kept = select_kept([ts for _, ts in stamped], now, policy)
    removed: list[Path] = []
    for path, ts in stamped:
        if ts not in kept:
            try:
                path.unlink()
                removed.append(path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return removed


def write_snapshot(
    snapshots_dir: str | Path,
    payload: str,
    *,
    now: datetime | None = None,
    policy: RetentionPolicy | None = None,
) -> Path:
    """Write one backup of ``payload`` (the exact ``data.json`` bytes) and
    prune the directory to the retention policy. Returns the backup's path."""
    directory = Path(snapshots_dir)
    now = now or datetime.now(UTC)
    policy = policy or RetentionPolicy.from_env()
    target = _unique_path(directory, snapshot_stamp(now))
    _atomic_write_text(target, payload)
    prune(directory, now, policy)
    return target


__all__ = [
    "RetentionPolicy",
    "SNAPSHOT_PREFIX",
    "SNAPSHOT_SUFFIX",
    "list_stamped",
    "parse_stamp",
    "prune",
    "select_kept",
    "snapshot_stamp",
    "snapshots_dir_for",
    "snapshots_enabled",
    "summarize",
    "write_snapshot",
]
