# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Read-only introspection of a bronze tree.

Bronze is not tables. It is a filesystem tree of append-only JSONL under
``<root>/<connector>/<subdir>/<day>/``, so its verbs are inventory-shaped —
what landed, how much, when, and what the provenance gate refused — not
``describe``-shaped. The tabular executor in :mod:`..gold_query` answers the
silver and gold tiers; this answers the one below them.

**Counts are not content.** ``_quarantine_rows`` and ``_excluded`` hold what
the provenance gate refused. Knowing *that* 456 items were refused, and under
which rule, is exactly what an operator needs. Reading the refused items back
out through a generic verb would undo the refusal, so :func:`inventory`
reports those directories by count and :func:`peek` will not open them.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The three dispositions the gate writes, and whether their contents may be
#: read back through :func:`peek`. Allowed rows passed; the other two did not.
SUBDIRS: dict[str, bool] = {
    "_rows": True,
    "_quarantine_rows": False,
    "_excluded": False,
}

#: Records returned by one peek. A peek is for seeing the shape of what
#: landed, not for exporting a deposit.
DEFAULT_PEEK_LIMIT = 20
MAX_PEEK_LIMIT = 500

#: Directories walked in one inventory before it stops and says so. A tree
#: whose row files were written one per sample reaches millions of entries,
#: and an introspection verb must not walk all of them to answer "how much".
MAX_DAYS_PER_SUBDIR = 5_000


class BronzeIntrospectError(ValueError):
    """Base for every caller-facing error from this module."""


class UnknownConnector(BronzeIntrospectError):
    """No such connector directory under the bronze root."""


class RefusedContent(BronzeIntrospectError):
    """The caller asked to read back what the provenance gate refused."""


@dataclass
class SubdirStat:
    """What one disposition holds for one connector."""

    subdir: str
    readable: bool
    files: int = 0
    bytes: int = 0
    days: int = 0
    first_day: str | None = None
    last_day: str | None = None
    truncated: bool = False


@dataclass
class ConnectorInventory:
    connector: str
    root: str
    dispositions: list[SubdirStat] = field(default_factory=list)
    content_blobs: int = 0

    @property
    def files(self) -> int:
        return sum(d.files for d in self.dispositions)

    @property
    def bytes(self) -> int:
        return sum(d.bytes for d in self.dispositions)

    def summary(self) -> str:
        parts = [
            f"{d.subdir}={d.files:,} file(s)" for d in self.dispositions if d.files or d.days
        ]
        return f"{self.connector}: " + ("; ".join(parts) if parts else "nothing landed")


def _day_dirs(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def _stat_subdir(connector_dir: Path, subdir: str, readable: bool) -> SubdirStat:
    stat = SubdirStat(subdir=subdir, readable=readable)
    base = connector_dir / subdir
    days = _day_dirs(base)
    if not days:
        return stat
    stat.truncated = len(days) > MAX_DAYS_PER_SUBDIR
    days = days[:MAX_DAYS_PER_SUBDIR]
    stat.days = len(days)
    stat.first_day = days[0].name
    stat.last_day = days[-1].name
    for day in days:
        try:
            with os.scandir(day) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    stat.files += 1
                    try:
                        stat.bytes += entry.stat().st_size
                    except OSError:  # pragma: no cover - races
                        pass
        except OSError:  # pragma: no cover - permissions
            continue
    return stat


def _connector_dirs(root: Path, connector: str | None) -> list[Path]:
    if not root.is_dir():
        return []
    if connector:
        found = root / connector
        if not found.is_dir():
            raise UnknownConnector(f"no connector {connector!r} under {root}")
        return [found]
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_"))


def inventory(root: Path | str, *, connector: str | None = None) -> dict[str, Any]:
    """What a bronze tree holds, per connector and per disposition.

    Reports refused dispositions by count. A caller learns that 456 items were
    quarantined without any of them being handed back.
    """
    root = Path(root).expanduser()
    out: list[ConnectorInventory] = []
    for cdir in _connector_dirs(root, connector):
        inv = ConnectorInventory(connector=cdir.name, root=str(root))
        for subdir, readable in SUBDIRS.items():
            inv.dispositions.append(_stat_subdir(cdir, subdir, readable))
        content = cdir / "_content"
        if content.is_dir():
            inv.content_blobs = sum(1 for _ in content.rglob("*") if _.is_file())
        out.append(inv)

    return {
        "data": {
            "root": str(root),
            "connectors": [asdict(i) | {"summary": i.summary()} for i in out],
            "files": sum(i.files for i in out),
            "bytes": sum(i.bytes for i in out),
        },
        "provenance": {
            "source": str(root),
            "method": "filesystem walk of <connector>/<disposition>/<day>",
            "rows": len(out),
            "note": (
                "refused dispositions are reported by count only; their contents "
                "are not readable through these verbs"
            ),
        },
    }


def peek(
    root: Path | str,
    connector: str,
    *,
    day: str | None = None,
    limit: int | None = None,
    subdir: str = "_rows",
) -> dict[str, Any]:
    """The first records of one connector's landed rows.

    For seeing the shape of what a producer is sending — the schema_ref it
    claims, the keys in ``row``, whether ``fetched_at`` looks sane. Not an
    export: the limit is capped, and the refused dispositions are closed.
    """
    root = Path(root).expanduser()
    if subdir not in SUBDIRS:
        raise BronzeIntrospectError(f"{subdir!r} is not a bronze disposition")
    if not SUBDIRS[subdir]:
        raise RefusedContent(
            f"{subdir} holds items the provenance gate refused; reading them back "
            "through a generic verb would undo the refusal. `bronze_inventory` "
            "reports how many were refused."
        )
    cdir = _connector_dirs(root, connector)[0]
    base = cdir / subdir
    days = _day_dirs(base)
    if day:
        days = [d for d in days if d.name == day]
        if not days:
            raise UnknownConnector(f"no day {day!r} under {base}")

    want = min(MAX_PEEK_LIMIT, max(1, int(limit or DEFAULT_PEEK_LIMIT)))
    records: list[dict[str, Any]] = []
    unparseable = 0
    for d in reversed(days):  # newest day first — what is arriving now
        for jsonl in sorted(d.glob("*.jsonl")):
            try:
                lines = jsonl.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                unparseable += 1
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    unparseable += 1
                if len(records) >= want:
                    break
            if len(records) >= want:
                break
        if len(records) >= want:
            break

    note = f"newest {len(records)} record(s) of {connector}/{subdir}"
    if unparseable:
        note += f"; {unparseable} line(s) or file(s) could not be parsed"
    return {
        "data": {
            "connector": connector,
            "subdir": subdir,
            "day": day,
            "records": records,
        },
        "provenance": {
            "source": f"{root}/{connector}/{subdir}",
            "method": f"first {want} record(s), newest day first",
            "rows": len(records),
            "note": note,
        },
    }


def freshness(root: Path | str, *, connector: str | None = None) -> dict[str, Any]:
    """The newest day directory per connector, and how old it is.

    Bronze freshness is what says whether a producer is still pushing. It is a
    different fact from silver freshness: a live producer whose conform pass
    has stalled looks fresh here and stale there, and telling those apart is
    the difference between chasing a DAQ and chasing a pipeline.
    """
    root = Path(root).expanduser()
    today = datetime.now(UTC).date()
    rows: list[dict[str, Any]] = []
    for cdir in _connector_dirs(root, connector):
        days = _day_dirs(cdir / "_rows")
        last = days[-1].name if days else None
        age_days: int | None = None
        if last:
            try:
                age_days = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
            except ValueError:
                age_days = None
        rows.append({"connector": cdir.name, "last_day": last, "age_days": age_days})
    rows.sort(key=lambda r: (r["age_days"] is None, r["age_days"] or 0))
    return {
        "data": {"root": str(root), "connectors": rows},
        "provenance": {
            "source": str(root),
            "method": "newest day directory per connector",
            "rows": len(rows),
            "note": "bronze freshness; a stalled conform pass is not visible here",
        },
    }


__all__ = [
    "DEFAULT_PEEK_LIMIT",
    "MAX_DAYS_PER_SUBDIR",
    "MAX_PEEK_LIMIT",
    "SUBDIRS",
    "BronzeIntrospectError",
    "ConnectorInventory",
    "RefusedContent",
    "SubdirStat",
    "UnknownConnector",
    "freshness",
    "inventory",
    "peek",
]
