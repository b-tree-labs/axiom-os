# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.site_rename`` — put one site's data back in one place.

A site id was whatever string a credential or manifest carried, so a
renamed site kept its history under the old id and gathered new readings
under the new one. Two datasets about one rig, and every question answered
from either sees half.

:mod:`axiom.infra.site_identity` stops it happening again — new rows land
under the canonical id whatever the credential says. This is the other
half: the rows already written.

**Dry run by default.** It rewrites the column everything else is keyed
and scoped by, so the default is to say what it would change. Nothing
about a count is destructive and the count is usually the thing somebody
actually wanted.

**Tables are discovered, not listed.** A hardcoded list goes stale the
first time a table is added, and the failure mode of a stale list here is
a site left half-renamed — which is the very state this exists to end.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: Schemas that hold site-keyed rows. Named rather than "every schema"
#: because rewriting a site column in a table nobody thought about is a
#: worse outcome than missing one and being told.
SCHEMAS = (
    "silver", "gold", "public", "data_platform", "webapp", "fleet", "chat",
)


def _site_columns(connection, schemas: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """``(schema, table, column)`` for every site-keyed column there is.

    Base tables only. A view over a renamed table follows it, and an
    UPDATE against one either fails or writes through to the table twice —
    on this node `gold.signals`, `gold.signals_latest` and the two
    `ingest_*` are all views over `silver.signals`.
    """
    from sqlalchemy import text

    rows = connection.execute(
        text(
            "SELECT c.table_schema, c.table_name, c.column_name "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.column_name IN ('site', 'site_id') "
            "AND c.table_schema = ANY(:schemas) "
            "AND t.table_type = 'BASE TABLE' "
            "ORDER BY c.table_schema, c.table_name"
        ),
        {"schemas": list(schemas)},
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _collisions(connection, schema: str, table: str, column: str,
                old: str, new: str) -> int:
    """Rows that would violate a unique key if *old* became *new*.

    A forked site has rows under BOTH ids, so the same key can exist twice
    — on one node three channels existed under both of a site's ids. A bulk
    UPDATE then fails partway and leaves the site half-renamed, which is
    the state this tool exists to end.
    """
    from sqlalchemy import text

    keys = connection.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = :rel ::regclass AND i.indisunique"
        ),
        {"rel": f'"{schema}"."{table}"'},
    ).fetchall()
    others = [k[0] for k in keys if k[0] != column]
    if not others:
        return 0
    joined = " AND ".join(f'a."{c}" = b."{c}"' for c in others)
    qualified = f'"{schema}"."{table}"'
    return int(
        connection.execute(
            text(
                f"SELECT count(*) FROM {qualified} a JOIN {qualified} b ON {joined} "  # noqa: S608
                f'WHERE a."{column}" = :old AND b."{column}" = :new'
            ),
            {"old": old, "new": new},
        ).scalar_one()
    )


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Count or rewrite one site id to another, across every site column."""
    from sqlalchemy import text

    from axiom.infra.db import engine_for

    old = str(params.get("old") or "").strip()
    new = str(params.get("new") or "").strip()
    if not old or not new:
        return SkillResult(
            ok=False,
            value={"error": "both `old` and `new` site ids are required"},
            actions_taken=["pass old=<former id> new=<canonical id>"],
        )
    if old == new:
        return SkillResult(
            ok=False,
            value={"error": f"{old!r} and {new!r} are the same id"},
            actions_taken=["nothing to do"],
        )
    apply = bool(params.get("apply", False))
    schemas = tuple(params.get("schemas") or SCHEMAS)

    found: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    try:
        engine = engine_for("data_platform")
        with engine.begin() as connection:
            for schema, table, column in _site_columns(connection, schemas):
                qualified = f'"{schema}"."{table}"'
                count = connection.execute(
                    text(f"SELECT count(*) FROM {qualified} WHERE {column} = :old"),
                    {"old": old},
                ).scalar_one()
                if not count:
                    continue
                clash = _collisions(connection, schema, table, column, old, new)
                entry = {"schema": schema, "table": table, "column": column,
                         "rows": int(count), "collisions": clash, "updated": 0}
                if clash:
                    # Refused rather than resolved. Which row wins — the one
                    # with the history or the one the new id has been
                    # collecting — is a judgement about the data, and a tool
                    # picking silently is how the wrong half survives.
                    blocked.append(entry)
                    found.append(entry)
                    continue
                if apply:
                    # One transaction for every table: a site half-renamed
                    # is the state this exists to end, so it must not be
                    # reachable by a failure partway through.
                    result = connection.execute(
                        text(f"UPDATE {qualified} SET {column} = :new WHERE {column} = :old"),
                        {"old": old, "new": new},
                    )
                    entry["updated"] = int(result.rowcount or 0)
                found.append(entry)
            if not apply:
                # Nothing was written, but a read transaction left open on
                # a big table is still a lock somebody else waits behind.
                connection.rollback()
    except Exception as exc:  # noqa: BLE001 - the reason is the answer
        return SkillResult(
            ok=False,
            value={"error": str(exc)},
            actions_taken=[f"could not reach the data platform: {exc}"],
        )

    if blocked:
        names = ", ".join(f"{e['schema']}.{e['table']} ({e['collisions']})"
                          for e in blocked)
        return SkillResult(
            ok=False,
            value={"old": old, "new": new, "applied": False,
                   "tables": found, "blocked": blocked},
            actions_taken=[
                f"refused: {len(blocked)} table(s) hold the same key under both "
                f"ids, so renaming would violate a unique key: {names}",
                "a forked site has rows under both names; which row wins is a "
                "judgement about the data, not one this should make",
                "resolve those rows first, then run this again",
            ],
        )

    total = sum(e["rows"] for e in found)
    verb = "renamed" if apply else "would rename"
    lines = [f"{e['rows']} row(s) in {e['schema']}.{e['table']}.{e['column']}"
             for e in found]
    if not found:
        lines = [f"no rows carry site {old!r} — nothing to rename"]
    else:
        lines.append(f"{verb} {total} row(s) from {old!r} to {new!r}")
        if not apply:
            lines.append("nothing was written — pass apply=true to do it")
    return SkillResult(
        ok=True,
        value={"old": old, "new": new, "applied": apply,
               "tables": found, "rows": total},
        actions_taken=lines,
    )


__all__ = ["SCHEMAS", "run"]
