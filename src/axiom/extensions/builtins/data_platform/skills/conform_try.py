# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.conform_try`` — dry-run a normalizer over one record and inspect it.

The loop a normalizer author actually wants: paste a bronze row, see the
canonical rows it becomes, and be told about the mistakes that silver would
otherwise absorb without complaint.

It writes nothing. No database, no bronze root, no upsert — the registered
normalizer is called on the record you supply and the output is returned.

The checks matter more than the echo. ``silver.signals`` is keyed
``(row_hash, channel)`` and the upsert is ``ON CONFLICT DO NOTHING``, so two
rows yielded from one record with the same channel name collide and the second
is **discarded with no error and no warning** — while a funnel still counts both
in ``rows_out``. That is invisible in production and obvious here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..conformance import NormalizerRegistry
from ..conformance.discovery import register_discovered

#: Canonical fields a normalizer must supply for a row to be usable.
REQUIRED = ("stream", "channel", "ts", "value")

#: Fields ``conform_rows`` fills in from the connector mapping and the record.
#: A normalizer setting them is either duplicating or, worse, disagreeing.
PLATFORM_OWNED = ("site", "schema_ref", "row_hash")


def _load_registry() -> tuple[NormalizerRegistry, list[str]]:
    registry = NormalizerRegistry()
    loaded = register_discovered(registry)
    return registry, loaded


def _inspect(rows: list[dict[str, Any]]) -> list[str]:
    """Everything wrong with these rows that silver would not tell you."""
    warnings: list[str] = []

    seen: dict[str, int] = {}
    for row in rows:
        channel = str(row.get("channel", ""))
        seen[channel] = seen.get(channel, 0) + 1
    for channel, count in sorted(seen.items()):
        if count > 1:
            warnings.append(
                f"channel {channel!r} emitted {count} times from one record — "
                "silver is keyed (row_hash, channel) and upserts ON CONFLICT DO "
                "NOTHING, so only the first survives and the rest vanish silently"
            )

    for index, row in enumerate(rows):
        missing = [f for f in REQUIRED if f not in row or row[f] is None]
        if missing:
            warnings.append(f"row {index}: missing required field(s) {', '.join(missing)}")

        owned = [f for f in PLATFORM_OWNED if f in row]
        if owned:
            warnings.append(
                f"row {index}: sets {', '.join(owned)}, which conform_rows fills in "
                "from the connector mapping — remove it rather than risk disagreeing"
            )

        ts = row.get("ts")
        if isinstance(ts, str):
            try:
                parsed = datetime.fromisoformat(ts)
            except ValueError:
                warnings.append(f"row {index}: ts {ts!r} is not ISO-8601")
            else:
                if parsed.tzinfo is None:
                    warnings.append(
                        f"row {index}: ts {ts!r} has no timezone; silver.signals is "
                        "timestamptz and a naive stamp is read as server-local"
                    )

        if "unit" not in row:
            warnings.append(f"row {index}: no unit — a bare number is not a measurement")

    if not rows:
        warnings.append("the normalizer yielded nothing; this record would be silently dropped")
    return warnings


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Dry-run a registered normalizer over one bronze record.

    Params:
        schema_ref: Which normalizer to exercise. Omit to list what is
            registered, which is also the fastest way to find out that your
            entry point did not load.
        record: The bronze record, as a dict or a JSON string. Its payload
            belongs under ``row``, matching what the edge writes.
    """
    registry, loaded = _load_registry()
    schema_ref = params.get("schema_ref")

    if not schema_ref:
        return SkillResult(
            ok=True,
            value={
                "registered": registry.refs(),
                "distributions": loaded,
                "hint": "pass schema_ref and record to dry-run one",
            },
        )

    fn = registry.get(str(schema_ref))
    if fn is None:
        return SkillResult(
            ok=False,
            errors=[
                f"no normalizer registered for {schema_ref!r}. Registered: "
                f"{', '.join(registry.refs()) or '(none)'}. If yours is missing, the "
                "entry point did not load — check that your distribution declares "
                "axiom.portfolio_member as well as axiom.data_platform.normalizers."
            ],
            value={"registered": registry.refs(), "distributions": loaded},
        )

    record = params.get("record")
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except json.JSONDecodeError as exc:
            return SkillResult(ok=False, errors=[f"record is not valid JSON: {exc}"])
    if not isinstance(record, dict):
        return SkillResult(ok=False, errors=["record must be a JSON object"])
    record.setdefault("schema_ref", schema_ref)
    record.setdefault("row_hash", "dry-run")

    try:
        rows = [dict(r) for r in fn(record)]
    except Exception as exc:  # noqa: BLE001 — the point is to show the author their error
        return SkillResult(
            ok=False,
            errors=[f"{type(exc).__name__}: {exc}"],
            value={
                "schema_ref": schema_ref,
                "note": "in a real run this record would be counted in the funnel's "
                "'errored' and the walk would continue",
            },
        )

    warnings = _inspect(rows)
    return SkillResult(
        ok=not warnings,
        errors=warnings,
        value={
            "schema_ref": schema_ref,
            "rows_out": len(rows),
            "channels": sorted({str(r.get("channel", "")) for r in rows}),
            "rows": rows,
        },
    )
