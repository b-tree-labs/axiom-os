# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``kit-check``: the gate a contribution passes before it can be promoted.

Three kinds of finding, each naming its file:

1. **Declarations** — a gold query that writes, reads outside silver, gold or
   the tenant's schema, a verb over an object that does not exist, a value
   without a unit.
2. **Normalizers** — every sample record run through the tenant's normalizer
   and inspected with the same checks ``conform-try`` applies: a duplicated
   channel that silver would drop silently, a naive timestamp, a missing unit.
3. **Isolation, proven rather than reviewed** — every gold object and every
   verb is run as the tenant role, then another tenant's copy of the same data
   is written beside it, and they are run again. The answers must not move.
   A query that forgot to filter by site would still pass here, because the
   database refuses it the other tenant's rows; that is the point. What this
   catches is an object that reaches past the policy, which is the thing a
   reviewer cannot see by reading SQL.

Static checks run anywhere. The isolation proof needs the local medallion; on
a machine without one it is reported as **not run**, never as passed.
"""

from __future__ import annotations

import json
import tomllib
from typing import Any

from ..skills.conform_try import _inspect, _uncertainty_notes
from . import declarations as decl
from .medallion import FOREIGN_SITE, connect, dsn, require_stamp
from .project import KitError, KitProject
from .runner import load_normalizers, query_object, run_verb, try_kit


def _deposit_findings(
    project, deposits, bronze, undeclared: dict[str, int], unitless: dict[str, int]
) -> list[str]:
    """Every sample deposit through its deposit normalizer, lint on what comes out.

    One deposit can be millions of readings, so the lint inspects the first
    :data:`_DEPOSIT_LINT_ROWS` rows per deposit — enough to catch the shape
    mistakes (duplicate channel at one instant, naive timestamp, missing
    unit), which do not wait for row one million to appear.
    """
    from ..conformance.deposits import load_deposits

    findings: list[str] = []
    for connector, ref in sorted(project.deposit_sources.items()):
        fn = deposits.get(ref)
        loaded, missing, _withheld = load_deposits(bronze, connector)
        findings.extend(f"{m}: its bytes are not in samples/bronze/_content" for m in missing)
        for deposit in loaded:
            name = f"{connector}/{deposit.name or deposit.record.get('item_id')}"
            if fn is None:  # load_normalizers already refuses this; belt for direct calls
                findings.append(f"{name}: no deposit normalizer registered for {ref!r}")
                continue
            rows: list[dict] = []
            try:
                for row in fn(deposit):
                    rows.append(row)
                    if len(rows) >= _DEPOSIT_LINT_ROWS:
                        break
            except Exception as exc:  # noqa: BLE001 — reported, one deposit at a time
                findings.append(
                    f"{name}: the deposit normalizer raised {type(exc).__name__}: {exc}"
                )
                continue
            advisory = {note for i, row in enumerate(rows) for note in _uncertainty_notes(i, row)}
            # The duplicate-channel lint judges ONE INSTANT (one row_hash in
            # silver). A deposit's rows span many instants, so they are judged
            # frame by frame; a channel repeated across timestamps is a series,
            # not a collision.
            frames: dict[Any, list[dict]] = {}
            for row in rows:
                frames.setdefault(row.get("ts"), []).append(row)
            seen_findings: set[str] = set()
            for frame in frames.values():
                for w in _inspect(frame):
                    if _PER_ROW_UNIT in w:
                        continue  # judged once per channel by unit_findings
                    if w not in advisory and w not in seen_findings:
                        seen_findings.add(w)
                        findings.append(f"{name}: {w}")
            for row in rows:
                if not row.get("uncertainty_terms") and row.get("uncertainty") is None:
                    ch = str(row.get("channel"))
                    undeclared[ch] = undeclared.get(ch, 0) + 1
            _count_unitless(rows, unitless)
    return findings


#: How many rows of each deposit the static lint inspects.
_DEPOSIT_LINT_ROWS = 500


#: conform-try's per-row unit warning. The kit judges units per CHANNEL
#: against kit.toml instead, so this one is not repeated row by row.
_PER_ROW_UNIT = "no unit — a bare number is not a measurement"


def _count_unitless(rows, unitless: dict[str, int]) -> None:
    """Tally rows that carry no unit (a missing key, None or blank), by channel."""
    from ...scidisplay.units import declared

    for row in rows:
        if not declared(row.get("unit")):
            ch = str(row.get("channel"))
            unitless[ch] = unitless.get(ch, 0) + 1


def unit_findings(project: KitProject, unitless: dict[str, int]) -> tuple[list[str], list[str]]:
    """``(blocking, notes)`` for values published without a unit.

    A value without a unit is not a fact, so a unitless channel FAILS the check
    — unless kit.toml declares it knowingly unknown under ``[units.undeclared]``
    with a reason. Absence is then declared once, where a reviewer reads it,
    instead of being tolerated row by row where nobody does. One finding per
    channel, never per row.
    """
    blocking: list[str] = []
    notes: list[str] = []
    covered = sorted(ch for ch in unitless if project.unit_knowingly_undeclared(ch))
    for ch in sorted(set(unitless) - set(covered)):
        blocking.append(
            f"{ch}: {unitless[ch]} row(s) carry no unit. Emit the unit from your normalizer, "
            f"or, if it is genuinely unknown, list the channel under [units.undeclared] in "
            f"kit.toml with the reason and what would settle it"
        )
    if covered:
        notes.append(
            f"units: {len(covered)} channel(s) knowingly undeclared "
            f"({project.undeclared_unit_reason})"
        )
    from fnmatch import fnmatchcase

    idle = [
        pattern
        for pattern in project.undeclared_unit_channels
        if not any(fnmatchcase(ch, pattern) for ch in unitless)
    ]
    if idle:
        notes.append(
            f"units: [units.undeclared] pattern(s) {', '.join(idle)} matched no unitless channel "
            f"in the samples; remove them once the units are declared"
        )
    return blocking, notes


def normalizer_findings(project: KitProject) -> tuple[list[str], list[str]]:
    """``(blocking, notes)`` from running every sample record through ``conform/``.

    Uncertainty findings are notes, not failures: declaring nothing is a
    legitimate state, and not knowing you declared nothing is the problem. They
    are collapsed to one line per channel, and a kit that states its posture in
    ``kit.toml`` gets one acknowledged line instead.
    """
    registry, deposits, _ = load_normalizers(project)
    blocking: list[str] = []
    undeclared: dict[str, int] = {}
    bronze = project.folder("samples") / "bronze"
    unitless: dict[str, int] = {}
    blocking.extend(_deposit_findings(project, deposits, bronze, undeclared, unitless))
    for path in sorted(bronze.rglob("*.jsonl")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            rec = json.loads(line)
            fn = registry.get(str(rec.get("schema_ref", "")))
            where = f"{path.relative_to(project.root.parent)}:{n}"
            if fn is None:
                blocking.append(
                    f"{where}: no normalizer in conform/ claims schema_ref {rec.get('schema_ref')!r}"
                )
                continue
            try:
                rows = list(fn(rec))
            except Exception as exc:  # noqa: BLE001 — reported, one record at a time
                blocking.append(f"{where}: the normalizer raised {type(exc).__name__}: {exc}")
                continue
            advisory = {note for i, row in enumerate(rows) for note in _uncertainty_notes(i, row)}
            blocking.extend(
                f"{where}: {w}"
                for w in _inspect(rows)
                if w not in advisory and _PER_ROW_UNIT not in w
            )
            for row in rows:
                if not row.get("uncertainty_terms") and row.get("uncertainty") is None:
                    ch = str(row.get("channel"))
                    undeclared[ch] = undeclared.get(ch, 0) + 1
            _count_unitless(rows, unitless)
            if len(blocking) > 40:
                return blocking + ["… stopped after 40 findings"], []
    unit_blocking, notes = unit_findings(project, unitless)
    blocking.extend(unit_blocking)
    if undeclared:
        posture = project.uncertainty_posture
        if posture:
            notes.append(
                f"uncertainty: posture {posture!r} declared in kit.toml for "
                f"{', '.join(sorted(undeclared))}"
            )
        else:
            notes.extend(
                f"{ch}: {count} rows declare no uncertainty, so every aggregate over them will "
                f"report none. Add uncertainty_terms in your normalizer, or state the honest "
                f'posture in kit.toml: [uncertainty] posture = "uncharacterised"'
                for ch, count in sorted(undeclared.items())
            )
    return blocking, notes


def _answers(project: KitProject, d: str, ds: decl.Declarations) -> dict[str, Any]:
    """Each object's and verb's answer as a sorted list of rows.

    Sorted because a query with no ORDER BY may return the same rows in a
    different order after an insert, and an order change is not a leak.
    """

    def canon(rows: list[dict]) -> list[str]:
        return sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)

    return {
        **{
            f"gold {g.name}": canon(query_object(project, d, g.name, limit=100_000))
            for g in ds.gold
        },
        **{f"verb {v.name}": canon(run_verb(project, d, v)) for v in ds.verbs},
    }


def isolation_findings(project: KitProject, d: str, ds: decl.Declarations) -> list[str]:
    require_stamp(d, "kit-check's isolation proof")
    # ADR-128 E4: the proof writes another tenant's copy of the data into the
    # LOCAL medallion's silver and removes it again. It maintains the tier for
    # the duration of a test; nothing it writes is ever served.
    before = _answers(project, d, ds)
    with connect(d) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM silver.signals WHERE site = %s", (FOREIGN_SITE,))
        cur.execute(
            """INSERT INTO silver.signals
                 (site, feed, channel, ts, value, unit, quality, source_class, schema_ref,
                  row_hash, role, derivation)
               SELECT %s, feed, channel, ts, value * 1000 + 12345, unit, quality, source_class,
                      schema_ref, 'foreign:' || row_hash, role, derivation
               FROM silver.signals WHERE site = %s
               ON CONFLICT DO NOTHING""",
            (FOREIGN_SITE, project.tenant),
        )
    try:
        after = _answers(project, d, ds)
    finally:
        # ADR-128 E4: removes the probe rows written above.
        with connect(d) as conn:
            conn.execute("DELETE FROM silver.signals WHERE site = %s", (FOREIGN_SITE,))
    return [
        f"{name}: its answer changed when another tenant's rows were added; it reaches past "
        f"the tenant boundary"
        for name in before
        if before[name] != after[name]
    ]


def reference_findings(project: KitProject) -> list[str]:
    """A number in reference data has a unit. The facts themselves are the tenant's.

    Reference data is not a transform (ADR-195). The platform does not learn
    what the names mean. It refuses a number that does not say its unit.
    """
    folder = project.folder("reference")
    if not folder.is_dir():
        return []
    findings: list[str] = []
    for path in sorted(folder.glob("*.toml")):
        rel = f"reference/{path.name}"
        try:
            doc = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            findings.append(f"{rel}: could not be read ({exc})")
            continue
        findings.extend(_unitless_quantities(rel, doc))
    return findings


def _unitless_quantities(where: str, node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        value = node.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            unit = node.get("unit")
            if not isinstance(unit, str) or not unit.strip():
                label = node.get("name") or where
                found.append(f"{where}: {label} is a number with no unit")
        for child in node.values():
            found.extend(_unitless_quantities(where, child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_unitless_quantities(where, child))
    return found


def check_kit(project: KitProject, *, isolation: bool = True) -> dict[str, Any]:
    ds = decl.load(project)
    report: dict[str, Any] = {"declarations": ds.problems, "reference": reference_findings(project)}
    try:
        report["normalizers"], report["notes"] = normalizer_findings(project)
    except KitError as exc:
        report["normalizers"], report["notes"] = [str(exc)], []
    report["isolation"] = "not run"
    if isolation and not ds.problems:
        try:
            d = dsn(project)
        except KitError:
            d = None
        if d is not None:
            try_kit(project)
            report["isolation"] = isolation_findings(project, d, ds)
    blocking = list(report["declarations"]) + list(report["reference"]) + list(report["normalizers"])
    if isinstance(report["isolation"], list):
        blocking += report["isolation"]
    report["ok"] = not blocking
    return report


__all__ = ["check_kit", "isolation_findings", "normalizer_findings", "reference_findings"]
