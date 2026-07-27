# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Project chat command/prompt templates into an Open WebUI instance.

This is the canonical, schema-introspecting form of a shell adapter that used to
live in a downstream site repo. Open WebUI serves its "/" command palette from a
``prompt`` table whose rows are validated by a Pydantic model that REQUIRES
``tags`` to be a list and ``data``/``meta`` to be dicts, and only surfaces rows
with a truthy ``is_active``. A prompt written with NULL JSON columns or a falsy
``is_active`` makes the whole ``/api/v1/prompts/`` response fail validation, which
presents to the user as an empty palette.

This projector therefore:

* always writes valid empty JSON (``tags='[]'``, ``data='{}'``, ``meta='{}'``) and
  ``is_active=1`` on insert;
* ``COALESCE``-heals legacy NULL JSON and re-activates rows on re-sync (idempotent);
* grants public read via the ``access_grant`` table so non-owner users see the
  prompts;
* introspects the live ``prompt`` schema (``PRAGMA table_info``) so it survives
  Open WebUI upgrades that add NOT NULL columns.

It is deliberately dependency-free (stdlib ``sqlite3`` only): Open WebUI stores its
state in a SQLite file, and the projector is a thin, well-tested writer against it.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PromptEntry",
    "ProjectionResult",
    "parse_command_catalog",
    "project_prompts",
]

# One `### /command — Title` section per command; body runs until the next
# `### /` heading or EOF. Matches the command-catalog markdown a site authors.
_CATALOG_RE = re.compile(r"^### (/[\w-]+) — (.+?)\n(.*?)(?=^### /|\Z)", re.M | re.S)


def parse_command_catalog(text: str) -> list[PromptEntry]:
    """Parse a command-catalog markdown document into ``PromptEntry`` rows."""
    return [
        PromptEntry(
            command=m.group(1),
            name=m.group(2).strip(),
            content=m.group(3).strip(),
        )
        for m in _CATALOG_RE.finditer(text)
    ]

# Columns the projector manages explicitly; everything else NOT NULL gets a
# type-appropriate default so inserts never violate a constraint.
_MANAGED = {"command", "user_id", "name", "content", "created_at", "updated_at", "id"}


@dataclass(frozen=True)
class PromptEntry:
    """One chat command projected as an Open WebUI prompt.

    ``command`` carries the leading slash (e.g. ``/day``) exactly as Open WebUI
    stores it; ``name`` is the human title; ``content`` is the template body.
    """

    command: str
    name: str
    content: str


@dataclass
class ProjectionResult:
    inserted: int = 0
    updated: int = 0
    total: int = 0


def _columns(db: sqlite3.Connection) -> dict[str, dict]:
    return {
        r[1]: {"type": (r[2] or "").upper(), "notnull": bool(r[3])}
        for r in db.execute("PRAGMA table_info(prompt)")
    }


def _defaults(cols: dict[str, dict]) -> dict[str, object]:
    """Values for every managed-elsewhere column, so a fresh row is palette-valid.

    JSON columns must never be NULL (the Pydantic model rejects it); ``is_active``
    must be truthy; other NOT NULL columns get a type-appropriate zero value.
    """
    d: dict[str, object] = {}
    for name, info in cols.items():
        if name in _MANAGED:
            continue
        typ = info["type"]
        if name == "tags":
            d[name] = "[]"  # the only list-shaped JSON column on `prompt`
        elif name in ("data", "meta") or "JSON" in typ:
            d[name] = "{}"  # dict-shaped JSON column (never NULL)
        elif name == "is_active":
            d[name] = 1  # MUST be truthy or the prompt is hidden from the palette
        elif not info["notnull"]:
            continue  # other nullable columns may stay unset
        elif any(k in typ for k in ("INT", "BOOL", "NUM", "REAL", "FLOAT", "DEC")):
            d[name] = 0  # numeric/boolean NOT NULL -> 0 (not "" which is wrong-type)
        else:
            d[name] = ""
    return d


def _resolve_owner(db: sqlite3.Connection, owner_user_id: str | None) -> str | None:
    if owner_user_id:
        return owner_user_id
    row = db.execute(
        "select id from user where role='admin' order by created_at limit 1"
    ).fetchone()
    if row:
        return row[0]
    row = db.execute("select id from user order by created_at limit 1").fetchone()
    return row[0] if row else None


def _ensure_public_grant(db: sqlite3.Connection, resource_id: str, now: int) -> None:
    exists = db.execute(
        "select 1 from access_grant where resource_type='prompt' and resource_id=? "
        "and principal_type='user' and principal_id='*' and permission='read'",
        (resource_id,),
    ).fetchone()
    if not exists:
        db.execute(
            "insert into access_grant("
            "id, resource_type, resource_id, principal_type, principal_id, permission, created_at"
            ") values (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "prompt", resource_id, "user", "*", "read", now),
        )


def project_prompts(
    db_path: str | Path,
    entries: Iterable[PromptEntry],
    *,
    owner_user_id: str | None = None,
    public: bool = True,
) -> ProjectionResult:
    """Upsert ``entries`` into the Open WebUI ``prompt`` table at ``db_path``.

    Idempotent and schema-introspecting. Returns counts of inserted/updated rows
    and the resulting prompt total. Raises ``ValueError`` if the database has no
    ``prompt`` table or no user to own the prompts.
    """
    entries = list(entries)
    result = ProjectionResult()
    db = sqlite3.connect(str(db_path))
    try:
        cols = _columns(db)
        if not cols:
            raise ValueError("no `prompt` table found in the Open WebUI database")
        owner = _resolve_owner(db, owner_user_id)
        if owner is None:
            raise ValueError("no user found to own projected prompts")
        has_grants = bool(
            db.execute(
                "select name from sqlite_master where type='table' and name='access_grant'"
            ).fetchone()
        )
        now = int(time.time())

        for entry in entries:
            exists = db.execute(
                "select 1 from prompt where command=?", (entry.command,)
            ).fetchone()
            if exists:
                sets = ["name=?", "content=?"]
                vals: list[object] = [entry.name, entry.content]
                if "is_active" in cols:
                    sets.append("is_active=?")
                    vals.append(1)  # self-heal any stale falsy value
                for jcol, jval in (("tags", "[]"), ("data", "{}"), ("meta", "{}")):
                    if jcol in cols:
                        sets.append(f"{jcol}=COALESCE({jcol}, '{jval}')")  # heal legacy NULLs
                if "updated_at" in cols:
                    sets.append("updated_at=?")
                    vals.append(now)
                db.execute(
                    f"update prompt set {', '.join(sets)} where command=?",
                    (*vals, entry.command),
                )
                result.updated += 1
            else:
                row: dict[str, object] = {
                    "command": entry.command,
                    "user_id": owner,
                    "name": entry.name,
                    "content": entry.content,
                }
                if "id" in cols and "INT" not in cols["id"]["type"]:
                    row["id"] = str(uuid.uuid4())
                if "created_at" in cols:
                    row["created_at"] = now
                if "updated_at" in cols:
                    row["updated_at"] = now
                row.update(_defaults(cols))
                db.execute(
                    f"insert into prompt({','.join(row)}) "
                    f"values({','.join('?' * len(row))})",
                    tuple(row.values()),
                )
                result.inserted += 1

        if public and has_grants:
            for (resource_id,) in db.execute("select id from prompt"):
                _ensure_public_grant(db, resource_id, now)

        db.commit()
        result.total = db.execute("select count(*) from prompt").fetchone()[0]
    finally:
        db.close()
    return result
