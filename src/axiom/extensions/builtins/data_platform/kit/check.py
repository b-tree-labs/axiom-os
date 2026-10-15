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
from typing import Any

from ..skills.conform_try import _inspect, _uncertainty_notes
from . import declarations as decl
from .medallion import FOREIGN_SITE, connect, dsn
from .project import KitError, KitProject
from .runner import load_normalizers, query_object, run_verb, try_kit


def normalizer_findings(project: KitProject) -> tuple[list[str], list[str]]:
    """``(blocking, notes)`` from running every sample record through ``conform/``.

    Uncertainty findings are notes, not failures: declaring nothing is a
    legitimate state, and not knowing you declared nothing is the problem. They
    are collapsed to one line per channel, and a kit that states its posture in
    ``kit.toml`` gets one acknowledged line instead.
    """
    registry, _ = load_normalizers(project)
    blocking: list[str] = []
    undeclared: dict[str, int] = {}
    bronze = project.folder("samples") / "bronze"
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
            blocking.extend(f"{where}: {w}" for w in _inspect(rows) if w not in advisory)
            for row in rows:
                if not row.get("uncertainty_terms") and row.get("uncertainty") is None:
                    ch = str(row.get("channel"))
                    undeclared[ch] = undeclared.get(ch, 0) + 1
            if len(blocking) > 40:
                return blocking + ["… stopped after 40 findings"], []
    notes: list[str] = []
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


def check_kit(project: KitProject, *, isolation: bool = True) -> dict[str, Any]:
    ds = decl.load(project)
    report: dict[str, Any] = {"declarations": ds.problems}
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
    blocking = list(report["declarations"]) + list(report["normalizers"])
    if isinstance(report["isolation"], list):
        blocking += report["isolation"]
    report["ok"] = not blocking
    return report


__all__ = ["check_kit", "isolation_findings", "normalizer_findings"]
