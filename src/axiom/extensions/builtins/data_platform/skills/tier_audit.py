# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.tier_audit`` — what shape the medallion is actually in.

ADR-128 says bronze lands, silver conforms, gold serves, and names the
allowances. A static test can hold the code to that. It cannot see the
database, and the database is where the drift showed up: eleven base tables in
gold that no axiom code creates, authored reference records split across two
tiers, and nothing anywhere that made either visible.

This reads ``information_schema`` and reports. It does not fix anything and it
does not fail a deploy — a table that appeared in gold by hand is a decision
somebody made, and the useful thing is that it stops being invisible.

This is itself an E5 read: a diagnostic, naming every tier it looked at.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..tiers import (
    DECLARATION_ENV,
    TIERS,
    classify,
    declared_gold_tables,
    reads_a_working_tier,
    transforms_in_views,
    unserved_working_tables,
)

_OBJECTS = (
    "SELECT table_schema, table_name, table_type FROM information_schema.tables "
    "WHERE table_schema = ANY(%s)"
)

#: ``view_table_usage`` only lists sources the current user owns, so this can
#: come back short. It never comes back WRONG, which is the property that
#: matters: every row it does return is a real dependency. Checked against the
#: ``pg_depend``/``pg_rewrite`` join on the node — the two agreed row for row,
#: so the cheaper and portable one is enough.
_VIEW_SOURCES = (
    "SELECT view_schema, view_name, table_schema, table_name "
    "FROM information_schema.view_table_usage WHERE view_schema = ANY(%s)"
)


#: ``view_definition`` comes back NULL when the caller cannot see the view's
#: body, so an empty definition means "could not read", never "does nothing".
#: Both land the same way here: no signature matches, no finding is invented.
_VIEW_DEFS = (
    "SELECT table_schema, table_name, view_definition "
    "FROM information_schema.views WHERE table_schema = ANY(%s)"
)


def _resolve_dsn(params: dict[str, Any]) -> str | None:
    from .._dsn import resolve_dsn

    return resolve_dsn(params)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Report the medallion's shape and anything ADR-128 did not expect.

    Params: ``dsn`` (else the usual env names), ``schemas`` (default the three
    medallion tiers).
    """
    raw = params.get("schemas") or list(TIERS)
    # The CLI passes one comma-separated string; a caller in-process passes
    # a list. Both mean the same thing, so accept both rather than make the
    # surface decide the shape.
    schemas = [s.strip() for s in raw.split(",")] if isinstance(raw, str) else list(raw)
    schemas = [s for s in schemas if s]
    if not schemas:
        return SkillResult(ok=False, errors=["schemas is empty"])

    dsn = _resolve_dsn(params)
    if not dsn:
        return SkillResult(
            ok=False,
            errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL / AXIOM_DB_URL"],
        )

    try:
        import psycopg2
    except ImportError:  # pragma: no cover - deployment always has it
        return SkillResult(ok=False, errors=["psycopg2 is not installed"])

    try:
        conn = psycopg2.connect(dsn, connect_timeout=15)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"could not connect: {exc}"])

    try:
        with conn.cursor() as cur:
            cur.execute(_OBJECTS, (schemas,))
            objects = [(r[0], r[1], r[2]) for r in cur.fetchall()]
            try:
                cur.execute(_VIEW_SOURCES, (schemas,))
                sources = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001 — a permission shortfall, not a failure
                conn.rollback()
                sources = []
            try:
                cur.execute(_VIEW_DEFS, (schemas,))
                definitions = [(r[0], r[1], r[2] or "") for r in cur.fetchall()]
            except Exception:  # noqa: BLE001 — same
                conn.rollback()
                definitions = []
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"could not read information_schema: {exc}"])
    finally:
        conn.close()

    declared = declared_gold_tables()
    findings = (
        classify(objects, declared)
        + reads_a_working_tier(sources)
        + transforms_in_views(definitions)
    )

    shape: dict[str, dict[str, list[str]]] = {}
    for schema, name, object_type in sorted(objects):
        bucket = "views" if object_type == "VIEW" else "tables"
        shape.setdefault(schema, {"tables": [], "views": []})[bucket].append(name)

    missing = [s for s in schemas if s not in shape]
    unserved = unserved_working_tables(objects, sources)

    actions = [
        f"{schema}: {len(shape[schema]['tables'])} table(s), "
        f"{len(shape[schema]['views'])} view(s)"
        for schema in schemas
        if schema in shape
    ]
    if missing:
        actions.append(f"not present in this database: {', '.join(missing)}")
    if declared:
        actions.append(f"{len(declared)} gold table(s) declared via {DECLARATION_ENV}")
    if unserved:
        actions.append(
            f"{len(unserved)} working-tier table(s) no gold object reads: "
            + ", ".join(f"{schema}.{name}" for schema, name in unserved)
            + " — not a violation, but this is exactly the set a serving path "
            "would have to reach into the working tier to read"
        )
    actions.append(
        f"{len(findings)} finding(s)"
        if findings
        else "no findings — the tiers hold the shape ADR-128 describes"
    )

    return SkillResult(
        ok=True,
        value={
            "schemas": schemas,
            "shape": shape,
            "missing_schemas": missing,
            "declared_gold_tables": sorted(declared),
            "unserved_working_tables": [f"{s}.{n}" for s, n in unserved],
            "findings": [
                {
                    "schema": f.schema,
                    "name": f.name,
                    "kind": f.kind,
                    "detail": f.detail,
                }
                for f in findings
            ],
        },
        actions_taken=actions,
    )


__all__ = ["run"]
