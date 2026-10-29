# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``kit-scaffold`` and ``kit-sample``: one more contribution, and the provider's own data.

``kit-init`` writes one example of every kind. After that a provider needs two
things the examples cannot give them: a second contribution of a kind without
copying a file by hand, and their OWN raw data in ``samples/`` without learning
the mirror's on-disk layout.

Both lean on one inventory: the kit's normalizers run over its samples, so a
scaffold names channels, units and a time window that actually exist. A chart
scaffolded from the inventory draws on the next ``kit-try``; one scaffolded
from placeholders would draw nothing and say so only after a round trip.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .project import KitError, KitProject

_SCAFFOLDS = Path(__file__).parent / "scaffolds"

#: Contribution kinds ``kit-scaffold`` scaffolds, and where each lands.
KINDS = ("conform", "derived", "gold", "verb", "chart", "starter", "automation")

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
#: How many rows of each deposit the inventory reads. Enough to learn its
#: channels; a deposit is often millions of readings.
_DEPOSIT_PEEK = 2000


def _require_name(kind: str, name: str) -> str:
    if not _NAME.match(name or ""):
        raise KitError(
            f"kit-scaffold {kind}: name {name!r} must be lowercase letters, digits and _, starting "
            f"with a letter (it becomes a file name, and for gold an object name)"
        )
    return name


def _refuse_existing(path: Path) -> None:
    if path.exists():
        raise KitError(f"{path} already exists; pick another name (kit-scaffold never overwrites)")


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------


def inventory(project: KitProject) -> dict[str, Any]:
    """What the kit's samples hold, as the kit's own normalizers read them.

    ``{"channels": {name: {"unit", "rows", "first", "last"}}, "first", "last"}``.
    Runs on the provider's machine only, like everything that imports
    ``conform/``.
    """
    from ..conformance.deposits import load_deposits
    from .runner import load_normalizers

    registry, deposits, _ = load_normalizers(project)
    bronze = project.folder("samples") / "bronze"
    seen: dict[str, dict[str, Any]] = {}
    #: Samples a normalizer raised on. Reported, not raised: the inventory
    #: describes the samples, and kit-check is the gate that fails on them.
    unreadable: list[str] = []

    def note(row: dict[str, Any]) -> None:
        ch = str(row.get("channel"))
        ts = row.get("ts")
        when = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        entry = seen.setdefault(ch, {"unit": None, "rows": 0, "first": when, "last": when})
        entry["rows"] += 1
        entry["unit"] = entry["unit"] or row.get("unit")
        if when is not None:
            entry["first"] = min(entry["first"] or when, when)
            entry["last"] = max(entry["last"] or when, when)

    for path in sorted(bronze.rglob("_rows/*/*.jsonl")) if bronze.is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            fn = registry.get(str(rec.get("schema_ref", "")))
            if fn is not None:
                try:
                    for row in fn(rec):
                        note(row)
                except Exception as exc:  # noqa: BLE001 — reported; kit-check is where it fails
                    unreadable.append(f"{path.name}: {type(exc).__name__}: {exc}")
    for connector, ref in sorted(project.deposit_sources.items()):
        fn = deposits.get(ref)
        if fn is None:
            continue
        loaded, _missing, _withheld = load_deposits(bronze, connector)
        for deposit in loaded:
            try:
                for i, row in enumerate(fn(deposit)):
                    if i >= _DEPOSIT_PEEK:
                        break
                    note(row)
            except Exception as exc:  # noqa: BLE001 — reported; kit-check is where it fails
                unreadable.append(f"{connector}/{deposit.name}: {type(exc).__name__}: {exc}")
    firsts = [e["first"] for e in seen.values() if e["first"] is not None]
    lasts = [e["last"] for e in seen.values() if e["last"] is not None]
    return {
        "channels": seen,
        "first": min(firsts) if firsts else None,
        "last": max(lasts) if lasts else None,
        "unreadable": unreadable,
    }


def _same_unit_channels(inv: dict[str, Any], limit: int) -> tuple[list[str], str | None]:
    """The most common declared unit, and up to ``limit`` channels carrying it."""
    units = Counter(e["unit"] for e in inv["channels"].values() if e["unit"])
    if not units:
        return sorted(inv["channels"])[:limit], None
    unit = units.most_common(1)[0][0]
    return sorted(ch for ch, e in inv["channels"].items() if e["unit"] == unit)[:limit], unit


# --------------------------------------------------------------------------
# kit-scaffold
# --------------------------------------------------------------------------

_ROW_NORMALIZER = '''"""Bronze to silver: {name}.

One pure function of one bronze record. Register its schema_ref in kit.toml
under [sources.<connector>] so kit-try conforms your samples with it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SCHEMA_REF = "{tenant}/{name}-v1"

#: Raw field -> silver channel name.
CHANNELS: dict[str, str] = {{}}

#: Silver channel -> unit. A value without a unit fails kit-check.
UNITS: dict[str, str] = {{}}


def {name}(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    payload = record["row"]
    ts = payload["ts"]  # a record with no time is broken; let it raise
    for field, channel in CHANNELS.items():
        value = payload.get(field)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue  # an absent optional channel is a skip, not a raise
        yield {{
            "feed": "{tenant}",
            "channel": channel,
            "ts": ts,
            "value": float(value),
            "unit": UNITS[channel],
            "source_class": "measured",
        }}


def register_all(registry) -> None:
    registry.register(SCHEMA_REF, {name})
'''

_DEPOSIT_NORMALIZER = '''"""Bronze deposits to silver: {name}.

One mirrored FILE to canonical rows. Return nothing to decline a file that is
not your format; raise to refuse one you claim and cannot read. Declare the
source in kit.toml with kind = "deposit".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SCHEMA_REF = "{tenant}/{name}-v1"
SUFFIX = ".csv"  # the file type this normalizer claims


def {name}(deposit) -> Iterable[dict[str, Any]]:
    if deposit.suffix != SUFFIX:
        return
    # deposit.path is the file's bytes; deposit.companion(...) reaches the
    # files deposited beside it (an index, a channel catalogue, settings).
    raise NotImplementedError(f"{{deposit.name}}: write the reader for this format")
    yield  # pragma: no cover


def register_deposits(registry) -> None:
    registry.register(SCHEMA_REF, {name})
'''

_CSV_NORMALIZER = '''"""Bronze deposits to silver: {name}, from CSV files shaped like {csv_name}.

Written by `kit-scaffold conform {name} --from-csv {csv_name}` from the file's first
rows. Fill in every UNIT (kit-check fails a channel with no unit unless kit.toml
declares it unknown, with a reason) and confirm TIMEZONE. Rename a channel by
editing its value in CHANNELS; the column name stays the key.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_REF = "{tenant}/{name}-v1"
FEED = "{name}"
SUFFIX = ".csv"
TIME_COLUMN = {time_column!r}
#: The zone the file's times are written in when they carry no offset. CONFIRM IT.
TIMEZONE = "UTC"
#: column -> (channel name in silver, unit). An empty unit is not yet known.
CHANNELS: dict[str, tuple[str, str]] = {{
{channels}}}


def _ts(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(TIMEZONE))


def {name}(deposit) -> Iterable[dict[str, Any]]:
    if deposit.suffix != SUFFIX:
        return
    text = deposit.path.read_bytes().decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(text)):
        stamp = (row.get(TIME_COLUMN) or "").strip()
        if not stamp:
            continue
        ts = _ts(stamp).astimezone(timezone.utc)
        for column, (channel, unit) in CHANNELS.items():
            raw = (row.get(column) or "").strip()
            if raw == "":
                continue
            yield {{
                "feed": FEED,
                "channel": channel,
                "ts": ts,
                "value": float(raw),
                "unit": unit or None,
                "source_class": "measured",
            }}


def register_deposits(registry) -> None:
    registry.register(SCHEMA_REF, {name})
'''

_TIME_NAME = re.compile(r"(^|_|\b)(time|timestamp|ts|date|datetime|stamp|utc)($|_|\b)", re.IGNORECASE)


def csv_shape(path: Path, *, rows: int = 200) -> tuple[str, list[str]]:
    """The time column and the numeric columns of a CSV, from its first rows."""
    import csv as _csv

    text = path.read_bytes()[:2_000_000].decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(text.splitlines())
    sample = [r for _, r in zip(range(rows), reader)]
    if not reader.fieldnames or not sample:
        raise KitError(f"{path}: no header row and data rows to learn from")

    def parses_time(v: str) -> bool:
        try:
            datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return True
        except ValueError:
            return False

    def numeric(v: str) -> bool:
        try:
            float(v)
            return True
        except ValueError:
            return False

    cols = list(reader.fieldnames)
    filled = {c: [str(r.get(c) or "") for r in sample if (r.get(c) or "").strip()] for c in cols}
    times = [c for c in cols if filled[c] and all(parses_time(v) for v in filled[c])]
    named = [c for c in times if _TIME_NAME.search(c)]
    if not times:
        raise KitError(f"{path}: no column holds ISO times (e.g. 2026-09-01T12:00:00); say which is time")
    time_col = (named or times)[0]
    nums = [c for c in cols if c != time_col and filled[c] and all(numeric(v) for v in filled[c])]
    if not nums:
        raise KitError(f"{path}: no numeric columns besides {time_col!r}")
    return time_col, nums


def _channel_name(column: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", column.strip().lower()).strip("_") or "value"


def _new_csv_conform(project: KitProject, name: str, csv_path: Path) -> list[str]:
    time_col, nums = csv_shape(csv_path)
    channels = "".join(f"    {c!r}: ({_channel_name(c)!r}, \"\"),  # unit?\n" for c in nums)
    written = [_write(project, f"conform/{name}.py", _CSV_NORMALIZER.format(
        name=name, tenant=project.tenant, csv_name=csv_path.name, time_column=time_col, channels=channels))]
    written.append(_write(project, f"conform/tests/test_{name}.py", _NORMALIZER_TEST.format(name=name)))
    manifest = project.root / "kit.toml"
    text = manifest.read_text(encoding="utf-8")
    if f"[sources.{name}]" not in text:
        manifest.write_text(
            text.rstrip("\n") + f'\n\n[sources.{name}]\nkind = "deposit"\nschema_ref = "{project.tenant}/{name}-v1"\n',
            encoding="utf-8",
        )
        written.append("kit.toml (added [sources." + name + "])")
    return written


_NORMALIZER_TEST = '''"""Tests for {name}: pure functions, nothing installed."""

import {name} as module


def test_it_registers_its_schema_ref():
    seen = {{}}

    class _Registry:
        def register(self, ref, fn):
            seen[ref] = fn

    getattr(module, "register_all", getattr(module, "register_deposits", None))(_Registry())
    assert module.SCHEMA_REF in seen
'''


def scaffold_contribution(
    project: KitProject, kind: str, name: str, *, deposit: bool = False, from_csv: str | None = None,
    from_snapshot: str | None = None,
    **automation: Any,
) -> list[str]:
    """Scaffold one contribution; return the paths written (relative to data/).

    ``automation`` carries the ``automation`` kind's choices: ``verb``, ``every``
    or ``cron``, ``above``/``below``/``any_rows`` with ``column``, ``notify``,
    ``annotate`` (a label).
    """
    if kind not in KINDS:
        raise KitError(f"kit-scaffold: kind {kind!r} is not one of {', '.join(KINDS)}")
    _require_name(kind, name)
    return {
        "conform": lambda: (
            _new_csv_conform(project, name, Path(from_csv).expanduser())
            if from_csv else _new_conform(project, name, deposit=deposit)
        ),
        "derived": lambda: _new_derived(project, name),
        "gold": lambda: _new_gold(project, name),
        "verb": lambda: _new_verb(project, name),
        "chart": lambda: _new_chart(project, name),
        "starter": lambda: _new_starter(project, name, from_snapshot or ""),
        "automation": lambda: _new_automation(project, name, **automation),
    }[kind]()


def _new_starter(project: KitProject, name: str, snapshot_id: str) -> list[str]:
    """A gold object, a verb and a chart over the channels a snapshot pulled.

    Pulling a frame that already exists leaves the kit's invented example running over
    invented channels; this writes the three pieces over the frame you pulled, filled in
    enough to pass kit-check, so the next kit-try shows your own data.
    """
    from .snapshot import load_snapshot

    if not snapshot_id:
        raise KitError("kit-scaffold starter: name the snapshot it builds on (--from-snapshot <id>)")
    snap = load_snapshot(project, snapshot_id)
    channels = [str(c) for c in (snap.get("channels") or [])]
    if not channels:
        raise KitError(f"snapshot {snapshot_id!r} names no channels")
    site = str(snap.get("site") or project.tenant)
    tenant_slug = project.tenant.replace("-", "_")
    sql = _starter_sql(f"gold_{tenant_slug}.{name}", snapshot_id, channels, snap.get("rows") or [],
                       days=_starter_window_days(snap.get("window") or {}))
    listed = ", ".join(channels)
    meta = (
        f'title = "{name.replace("_", " ").capitalize()}"\n'
        f'description = "Hourly mean and reading count of {listed} at {site}."\n'
        'grain = "one row per channel per hour"\n'
    )
    verb = (
        f'name = "{name}_by_hour"\n'
        f'description = "Hourly mean of {listed} at {site}, newest first, in each channel\'s own unit."\n'
        f'object = "{name}"\n'
        'sql = "SELECT * FROM {object} ORDER BY hour DESC LIMIT %(limit)s"\n\n'
        "[params.limit]\n"
        'type = "int"\n'
        "default = 48\n"
        'description = "How many hourly rows to return."\n'
    )
    window = snap.get("window") or {}
    # One chart per unit: channels in different units on one axis is refused by
    # kit-try, and the snapshot already says which unit each channel is in.
    by_unit: dict[str, list[str]] = {}
    unit_of: dict[str, str] = {}
    for row in snap.get("rows") or []:
        ch, unit = str(row.get("channel")), str(row.get("unit") or "")
        if unit or ch not in unit_of:
            unit_of[ch] = unit
    for ch in channels:  # snapshot order, names exactly as pulled
        by_unit.setdefault(unit_of.get(ch, ""), []).append(ch)
    title = name.replace("_", " ").capitalize()
    charts: list[tuple[str, dict[str, Any]]] = []
    for unit, members in sorted(by_unit.items()):
        suffix = "" if len(by_unit) == 1 else "_" + (re.sub(r"[^a-z0-9]+", "_", unit.lower()).strip("_") or "no_unit")
        charts.append((f"{name}{suffix}", {
            "schema_version": "1.1",
            "kind": "timeseries",
            "site": project.tenant,
            "title": title + (f" ({unit or 'no unit'})" if suffix else ""),
            "channels": members,
            "window": {"from": str(window.get("start") or ""), "to": str(window.get("end") or "")},
            "transform": {"bucket": "1m", "agg": "mean"},
        }))
    return [
        _write(project, f"gold/{name}.sql", sql),
        _write(project, f"gold/{name}.toml", meta),
        _write(project, f"verbs/{name}_by_hour.toml", verb),
        *(_write(project, f"charts/{chart_name}.json", json.dumps(chart, indent=2) + "\n")
          for chart_name, chart in charts),
    ]


#: The window a starter's gold view reads, back from now, in days. A view over a site's
#: whole history runs past the node's statement timeout once the site has any history.
STARTER_WINDOW_DAYS = 7


def _starter_window_days(window: dict[str, Any]) -> int:
    """7, or enough whole days back from now to cover the snapshot's start, so kit-try on
    the pulled frame shows rows. Still a rolling bound: the view never reads all history."""
    start = str((window or {}).get("start") or "")
    try:
        since = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return STARTER_WINDOW_DAYS
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return max(STARTER_WINDOW_DAYS, (datetime.now(UTC) - since).days + 1)


def _lit(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _starter_sql(view: str, snapshot_id: str, channels: list[str], rows: list[dict[str, Any]],
                 days: int = STARTER_WINDOW_DAYS) -> str:
    """The starter's gold view: the pulled channels, each in its own feed, over a bounded window.

    Naming the feed with each channel, and bounding ``ts``, is what lets the node answer
    from the ``(site, feed, channel, ts)`` index instead of reading every row the site holds.
    A channel the snapshot gives no feed for is matched by name alone, and the file says so.
    """
    feed_of: dict[str, str] = {}
    for row in rows:
        ch, feed = str(row.get("channel")), str(row.get("feed") or "")
        if feed and ch not in feed_of:
            feed_of[ch] = feed
    by_feed: dict[str, list[str]] = {}
    for ch in channels:
        by_feed.setdefault(feed_of.get(ch, ""), []).append(ch)
    terms = []
    for feed, members in sorted(by_feed.items()):
        names = ", ".join(_lit(c) for c in members)
        terms.append(f"(feed = {_lit(feed)} AND channel IN ({names}))" if feed else f"channel IN ({names})")
    unfed = by_feed.get("")
    # ADR-128 E4: this is the text of a gold derivation, which the materializer (a gold
    # writer) runs to build gold from silver. Nothing is served from silver here.
    return (
        f"-- {view}: hourly means of the channels pulled into snapshot\n"
        f"-- {snapshot_id}, run as your tenant role, so it only ever sees your own rows.\n"
        "--\n"
        f"-- The window: the last {days} days"
        + ("" if days == STARTER_WINDOW_DAYS else
           " (7 by default, widened so it covers the snapshot)")
        + ". A view over the site's whole\n"
        "-- history runs past the node's statement timeout. To widen it, change the interval\n"
        "-- below (say '30 days'); keep a bound. kit-try shows rows only when your sandbox holds\n"
        "-- readings inside it.\n"
        + (f"-- No feed is known for {', '.join(unfed)}; name its feed here so the node can use\n"
           "-- its signal index.\n" if unfed else "")
        # ADR-128 E4: the gold derivation's own text (the materializer, a gold writer, runs it).
        + "SELECT channel,\n"
        "       unit,\n"
        "       date_trunc('hour', ts) AS hour,\n"
        "       avg(value)             AS mean,\n"
        "       count(value)           AS readings\n"
        "FROM silver.signals\n"
        f"WHERE ts >= now() - interval '{days} days'\n"
        "  AND (" + "\n       OR ".join(terms) + ")\n"
        "GROUP BY channel, unit, date_trunc('hour', ts)\n"
    )


def _write(project: KitProject, rel: str, text: str) -> str:
    path = project.root / rel
    _refuse_existing(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _new_conform(project: KitProject, name: str, *, deposit: bool) -> list[str]:
    template = _DEPOSIT_NORMALIZER if deposit else _ROW_NORMALIZER
    written = [
        _write(project, f"conform/{name}.py", template.format(name=name, tenant=project.tenant))
    ]
    written.append(
        _write(project, f"conform/tests/test_{name}.py", _NORMALIZER_TEST.format(name=name))
    )
    conftest = project.root / "conform" / "tests" / "conftest.py"
    if not conftest.exists():
        written.append(
            _write(
                project,
                "conform/tests/conftest.py",
                '"""Put conform/ on the path: the kit loads normalizers by file, flat."""\n\n'
                "import sys\nfrom pathlib import Path\n\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n",
            )
        )
    return written


def _new_derived(project: KitProject, name: str) -> list[str]:
    inv = inventory(project)
    channels, unit = _same_unit_channels(inv, 2)
    if len(channels) < 2:
        raise KitError(
            "kit-scaffold derived: the samples hold fewer than two channels with one unit, so there "
            "is nothing to derive from yet. Bring data with kit-sample first."
        )
    path = project.folder("silver") / "derived.toml"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if re.search(rf'^\s*name\s*=\s*"{re.escape(name)}"', existing, re.MULTILINE):
        raise KitError(f"{path}: a derived channel named {name!r} already exists")
    block = (
        f'\n[[derived]]\nname = "{name}"\n'
        f'expression = "{channels[1]} - {channels[0]}"\n'
        f'unit = "{unit or "unknown"}"\n'
        f'description = ""  # say what it means; chat reads this\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + block, encoding="utf-8")
    return ["silver/derived.toml"]


def _new_gold(project: KitProject, name: str) -> list[str]:
    # The SQL text lives in scaffolds/gold.sql: it is the tenant's gold object,
    # written into their repository, not a query the platform runs.
    sql = (
        (_SCAFFOLDS / "gold.sql")
        .read_text(encoding="utf-8")
        .format(tenant_slug=project.tenant.replace("-", "_"), name=name)
    )
    meta = (
        '# What chat reads to answer "what data do we have". kit-check refuses an empty\n'
        "# description, so this scaffold fails the check until you write one.\n"
        f'title = "{name.replace("_", " ").capitalize()}"\n'
        'description = ""\n'
        'grain = "one row per channel per hour"\n'
    )
    return [_write(project, f"gold/{name}.sql", sql), _write(project, f"gold/{name}.toml", meta)]


def _new_verb(project: KitProject, name: str) -> list[str]:
    objects = sorted(p.stem for p in project.folder("gold").glob("*.sql"))
    if not objects:
        raise KitError("kit-scaffold verb: a verb runs over a gold object, and there is none yet")
    body = (
        "# A question chat can answer. kit-check refuses an empty description, so this\n"
        "# scaffold fails the check until you say what it answers and in what unit.\n"
        f'name = "{name}"\n'
        'description = ""\n'
        f'object = "{objects[0]}"\n'
        'sql = "SELECT * FROM {object} LIMIT %(limit)s"\n\n'
        "[params.limit]\n"
        'type = "int"\n'
        "default = 100\n"
        'description = "How many rows to return."\n'
    )
    return [_write(project, f"verbs/{name}.toml", body)]


def _toml_value(v: Any) -> str:
    return json.dumps(v) if isinstance(v, str) else repr(float(v)) if isinstance(v, (int, float)) else str(v)


def _new_automation(
    project: KitProject, name: str, *, verb: str | None = None, every: str | None = None,
    cron: str | None = None, above: float | None = None, below: float | None = None,
    any_rows: bool = False, column: str | None = None, notify: bool = False,
    annotate: str | None = None, **_ignored: Any,
) -> list[str]:
    """``automations/<name>.toml``, refused here for anything kit-check would refuse
    except the description, which is the contributor's to write."""
    import tomllib

    from . import automations
    from . import declarations as decl

    if bool(every) == bool(cron):
        raise KitError('kit-scaffold automation: give one of --every or --cron (e.g. --every 1h, or --cron "0 7 * * 1-5")')
    lines = [
        "# Runs one of this kit's verbs on a cadence and acts on the rows it returns.",
        "# kit-try shows what one run would do; on the node it runs as your site's role.",
        "# kit-check refuses an empty description: say what this watches for.",
        'description = ""',
        f"every = {_toml_value(every)}" if every else f"cron = {_toml_value(cron)}",
        f"verb = {_toml_value(verb or '')}",
    ]
    tests = [(k, v) for k, v in (("above", above), ("below", below)) if v is not None]
    if any_rows:
        tests.append(("any_rows", True))
    if tests:
        lines += ["", "[when]"]
        if column:
            lines.append(f"column = {_toml_value(column)}")
        lines += [f"{k} = {'true' if v is True else _toml_value(v)}" for k, v in tests]
    lines += ["", "[then]"]
    if notify:
        lines.append('notify = "submitter"')
    if annotate:
        lines.append(f"annotate = {{ label = {_toml_value(annotate)} }}")
    text = "\n".join(lines) + "\n"
    ds = decl.load(project)
    problems: list[str] = []
    doc = {**tomllib.loads(text), "description": "(written by the contributor)"}
    automations._parse(name, doc, f"automations/{name}.toml", {v.name: v for v in ds.verbs},
                       {g.name: g for g in ds.gold}, problems)
    if problems:
        raise KitError("kit-scaffold automation:\n  " + "\n  ".join(problems))
    return [_write(project, f"automations/{name}.toml", text)]


def _new_chart(project: KitProject, name: str) -> list[str]:
    inv = inventory(project)
    channels, _unit = _same_unit_channels(inv, 4)
    if not channels or inv["first"] is None:
        raise KitError(
            "kit-scaffold chart: the samples conform to no channels yet, so a chart would draw "
            "nothing. Bring data with kit-sample, and register its normalizer, first."
        )
    first = inv["first"].replace(minute=0, second=0, microsecond=0)
    last = inv["last"]
    span = (last - first).total_seconds()
    bucket = "1m" if span <= 6 * 3600 else "1h" if span <= 14 * 86400 else "1d"
    doc = {
        "schema_version": "1.1",
        "kind": "timeseries",
        "site": project.tenant,
        "title": name.replace("_", " ").capitalize(),
        "channels": channels,
        # The whole sample span: from the hour the samples start to just past
        # their last reading (the window's end is exclusive).
        "window": {"from": first.isoformat(), "to": _ceil_second(last).isoformat()},
        "transform": {"bucket": bucket, "agg": "mean"},
    }
    return [_write(project, f"charts/{name}.json", json.dumps(doc, indent=2) + "\n")]


def _ceil_second(when: datetime) -> datetime:
    """The next whole second after ``when``, so the last reading is inside the window."""
    from datetime import timedelta

    return when.replace(microsecond=0) + timedelta(seconds=1)


# --------------------------------------------------------------------------
# kit-sample
# --------------------------------------------------------------------------


def sample(project: KitProject, source: str, origin: Path | str) -> dict[str, Any]:
    """Bring the provider's own raw files into ``samples/`` for one declared source.

    A ``rows`` source takes ``*.jsonl`` bronze records as they are. A
    ``deposit`` source takes any files: each is content-addressed under
    ``_content/`` (gitignored; bytes are the provider's to keep) and gets a
    mirror record naming its hash, with its path relative to ``origin`` kept
    as ``source_path`` so files that arrive together stay companions.
    """
    origin = Path(origin).expanduser().resolve()
    if not origin.exists():
        raise KitError(f"kit-sample: {origin} does not exist")
    files = [origin] if origin.is_file() else sorted(p for p in origin.rglob("*") if p.is_file())
    files = [f for f in files if not f.name.startswith(".")]
    if not files:
        raise KitError(f"kit-sample: {origin} holds no files")
    bronze = project.folder("samples") / "bronze"
    day = datetime.now(UTC).date().isoformat()
    added: list[str] = []
    if source in project.deposit_sources:
        base = origin if origin.is_dir() else origin.parent
        records = bronze / source / "_records" / day
        records.mkdir(parents=True, exist_ok=True)
        for f in files:
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            content = bronze / "_content" / sha[:2] / sha
            content.parent.mkdir(parents=True, exist_ok=True)
            if not content.exists():
                shutil.copyfile(f, content)
            rel = f.relative_to(base).as_posix()
            record = {
                "item_id": sha[:16],
                "display_name": f.name,
                "source_path": f"/{rel}",
                "content_sha256": sha,
                "size": f.stat().st_size,
                "disposition": "allow",
                "fetched_at": datetime.now(UTC).isoformat(),
                "source_name": source,
            }
            (records / f"{sha[:16]}.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
            added.append(rel)
    elif source in project.sources:
        rows = bronze / source / "_rows" / day
        rows.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.suffix != ".jsonl":
                raise KitError(
                    f"kit-sample: {f.name} is not .jsonl; source {source!r} is declared as rows. "
                    f'If these are whole files, declare it kind = "deposit" in kit.toml'
                )
            shutil.copyfile(f, rows / f.name)
            added.append(f.name)
    else:
        declared = sorted({*project.sources, *project.deposit_sources})
        raise KitError(
            f"kit-sample: no source {source!r} in kit.toml (declared: {', '.join(declared) or 'none'})"
        )
    inv = inventory(project)
    return {
        "source": source,
        "added": added,
        "channels": {
            ch: {"unit": e["unit"], "rows": e["rows"]} for ch, e in sorted(inv["channels"].items())
        },
        "first": inv["first"].isoformat() if inv["first"] else None,
        "last": inv["last"].isoformat() if inv["last"] else None,
        "unreadable": inv["unreadable"],
    }


__all__ = ["KINDS", "inventory", "sample", "scaffold_contribution"]
