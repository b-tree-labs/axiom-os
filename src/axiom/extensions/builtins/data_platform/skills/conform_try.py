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
REQUIRED = ("feed", "channel", "ts", "value")

#: Qualities under which a NULL ``value`` is correct rather than missing.
#:
#: A device-asserted fault is withheld rather than served: the fault travels
#: as `quality` and the reading is NULL, so SQL aggregates self-correct
#: (`avg()` ignores it, where a 961 sentinel inside a rod-position average is
#: silently wrong). See ADR-132 and the sensing-fault absence table.
#:
#: Without this, the one loop that tells a normalizer author what their rows
#: will do reported a CORRECT withholding as a missing required field — which
#: would teach them to emit the sentinel instead, producing exactly the defect
#: the withholding exists to prevent.
#:
#: `good` and `ok` are deliberately absent. A NULL value under a quality that
#: claims the reading is fine is a real defect: something produced nothing and
#: said nothing was wrong.
WITHHELD_QUALITIES = frozenset({"bad", "suspect", "saturated", "stale", "uncertain"})

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
        withheld = str(row.get("quality") or "") in WITHHELD_QUALITIES
        missing = [
            f
            for f in REQUIRED
            if (f not in row or row[f] is None)
            # A withheld value is the point, not an omission.
            and not (f == "value" and withheld)
        ]
        if missing:
            warnings.append(f"row {index}: missing required field(s) {', '.join(missing)}")
        if withheld and row.get("value") is not None:
            # The other direction, and the one that actually reaches a served
            # surface: a row that says the reading is bad AND carries the
            # number anyway. Downstream has no way to know the number is the
            # fault code, and `avg()` will happily include it.
            warnings.append(
                f"row {index}: quality={row.get('quality')!r} but a value is still "
                "carried — withhold it (value=None) so aggregates self-correct; a "
                "fault code inside an average is silently wrong"
            )

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

        # A withheld reading has nothing to be uncertain ABOUT, so the
        # uncertainty notes would be noise on exactly the rows that are
        # already saying something.
        if not withheld:
            warnings.extend(_uncertainty_notes(index, row))

    if not rows:
        warnings.append("the normalizer yielded nothing; this record would be silently dropped")
    return warnings


def _uncertainty_notes(index: int, row: dict[str, Any]) -> list[str]:
    """What this row will be able to say about how well it is known.

    Said here because this is the only loop where a normalizer author finds
    out. Downstream, a row that declares nothing produces an aggregate
    reporting ``claimable: false`` and **nothing fails** — the absence is
    invisible precisely because it is an absence, so the author has to be told
    at the moment they can act on it (ADR-136; spec-uncertainty §2).

    Notes rather than errors. Declaring nothing is a legitimate state; not
    knowing that you declared nothing is not.
    """
    notes: list[str] = []
    terms = row.get("uncertainty_terms")
    scalar = row.get("uncertainty")

    if scalar == 0 or scalar == 0.0:
        notes.append(
            f"row {index}: uncertainty is 0 — that is a claim of PERFECT precision, "
            "which no instrument supports. Omit the field for 'not reported'; NULL "
            "and zero are different facts"
        )

    if not terms:
        if scalar is None:
            notes.append(
                f"row {index}: declares no uncertainty, so every served aggregate "
                "over it will report none and no check will fail. If the instrument "
                "is genuinely uncharacterised that is the honest state — say so in "
                "the extension's `[extension.uncertainty] posture`"
            )
        else:
            notes.append(
                f"row {index}: carries a magnitude but not its sources, so aggregates "
                "can only be BOUNDED. Two readings sharing a calibration cannot be "
                "told from two independent ones, and averaging them as independent "
                "overstates the precision. Add `uncertainty_terms` to get an exact "
                "figure — declaring more makes the answer narrower"
            )
        return notes

    from axiom.uncertainty import SymbolError, check_symbol

    shared, per_reading = [], []
    for symbol, spec in terms.items():
        try:
            check_symbol(str(symbol))
        except SymbolError as exc:
            notes.append(f"row {index}: {exc}")
            continue
        if isinstance(spec, (tuple, list)):
            coefficient = spec[0]
            independent = bool(spec[1]) if len(spec) > 1 else False
        elif isinstance(spec, dict):
            coefficient = spec.get("coefficient")
            independent = bool(spec.get("independent", False))
        else:
            coefficient, independent = spec, False
        if coefficient is None:
            notes.append(
                f"row {index}: source {symbol!r} has no coefficient, so it will be "
                "skipped. A declared symbol with no magnitude is not a zero-magnitude "
                "source"
            )
            continue
        (per_reading if independent else shared).append(str(symbol))

    if shared:
        notes.append(
            f"row {index}: {len(shared)} shared source(s) — {', '.join(sorted(shared))} "
            "— will NOT average away across rows. That is usually right for a "
            "calibration; check it is right for each of these"
        )
    if per_reading:
        notes.append(
            f"row {index}: {len(per_reading)} per-reading source(s) — "
            f"{', '.join(sorted(per_reading))} — WILL average down as 1/sqrt(n). "
            "Declaring a shared source this way makes it vanish: at 400 readings the "
            "figure is 20x too confident and looks entirely reasonable"
        )
    if terms and scalar is not None:
        from axiom.uncertainty import Quantity

        flat = {}
        for symbol, spec in terms.items():
            c = (
                spec[0]
                if isinstance(spec, (tuple, list))
                else (spec.get("coefficient") if isinstance(spec, dict) else spec)
            )
            if c is not None:
                flat[str(symbol)] = float(c)
        try:
            implied = Quantity(value=0.0, unit=row.get("unit", ""), terms=flat).u
        except Exception:  # noqa: BLE001 — a bad symbol was already reported
            return notes
        if implied > 0 and abs(float(scalar) - implied) > 1e-9 * max(1.0, implied):
            notes.append(
                f"row {index}: the scalar uncertainty ({scalar:g}) disagrees with the "
                f"declared sources ({implied:g}). The scalar is meant to be a SUMMARY "
                "of the same sources; derive it from them so the two cannot drift"
            )
    return notes


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
