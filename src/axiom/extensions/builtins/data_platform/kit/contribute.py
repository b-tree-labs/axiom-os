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
KINDS = ("conform", "derived", "gold", "verb", "chart")

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
    project: KitProject, kind: str, name: str, *, deposit: bool = False
) -> list[str]:
    """Scaffold one contribution; return the paths written (relative to data/)."""
    if kind not in KINDS:
        raise KitError(f"kit-scaffold: kind {kind!r} is not one of {', '.join(KINDS)}")
    _require_name(kind, name)
    return {
        "conform": lambda: _new_conform(project, name, deposit=deposit),
        "derived": lambda: _new_derived(project, name),
        "gold": lambda: _new_gold(project, name),
        "verb": lambda: _new_verb(project, name),
        "chart": lambda: _new_chart(project, name),
    }[kind]()


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
