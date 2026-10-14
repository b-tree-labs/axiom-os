# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Read a kit's declarations: silver building blocks, gold objects, verbs, charts.

Everything here is data the platform evaluates, never code it runs on the
tenant's behalf. Each problem is reported with the file it is in and what to
change, because "invalid declaration" with no file is a search, not a message.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .project import KitProject

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
#: A derived channel is one arithmetic step over two operands: a channel name
#: or a number. Anything more is a gold object, not a building block.
_OPERAND = r"(?:[A-Za-z_][A-Za-z0-9_.:-]*|-?\d+(?:\.\d+)?)"
_EXPRESSION = re.compile(rf"^\s*({_OPERAND})\s*([-+*/])\s*({_OPERAND})\s*$")
#: Words that make a statement something other than one read.
_WRITES = re.compile(
    r"\b(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|copy|"
    r"call|do|execute|vacuum|analyze|set|reset|listen|notify|lock|refresh)\b",
    re.IGNORECASE,
)
_RELATION = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\"]*\.[A-Za-z_][A-Za-z0-9_\"]*)", re.IGNORECASE
)


@dataclass(frozen=True)
class Derived:
    name: str
    expression: str
    left: str
    op: str
    right: str
    unit: str | None
    role: str | None
    description: str
    source: Path


@dataclass(frozen=True)
class GoldObject:
    name: str
    sql: str
    title: str
    description: str
    grain: str
    source: Path


@dataclass(frozen=True)
class Verb:
    name: str
    description: str
    obj: str
    sql: str
    params: dict[str, dict[str, Any]]
    source: Path


@dataclass(frozen=True)
class Chart:
    name: str
    spec: dict[str, Any]
    source: Path


@dataclass
class Declarations:
    derived: list[Derived] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)
    gold: list[GoldObject] = field(default_factory=list)
    verbs: list[Verb] = field(default_factory=list)
    charts: list[Chart] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _rel(project: KitProject, path: Path) -> str:
    try:
        return str(path.relative_to(project.root.parent))
    except ValueError:
        return str(path)


def _toml(path: Path, problems: list[str], label: str) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        problems.append(f"{label}: not valid TOML: {exc}")
        return {}


def sql_problems(sql: str, *, allowed_schemas: set[str]) -> list[str]:
    """Why ``sql`` is not one read of permitted relations, or ``[]``."""
    out: list[str] = []
    # Comments explain a query; they are not part of what it does.
    body = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    body = re.sub(r"--[^\n]*", " ", body).strip().rstrip(";").strip()
    if ";" in body:
        out.append("holds more than one statement; a gold object is one SELECT")
    if not re.match(r"^\s*(with|select)\b", body, re.IGNORECASE):
        out.append("must start with SELECT (or WITH ... SELECT)")
    hit = _WRITES.search(re.sub(r"'[^']*'", "''", body))
    if hit:
        out.append(f"uses {hit.group(1).upper()}; a gold object only reads")
    for rel in _RELATION.findall(body):
        schema = rel.split(".", 1)[0].strip('"').lower()
        if schema not in allowed_schemas:
            out.append(
                f"reads {rel}; a gold object reads silver.signals, gold.*, or this tenant's own schema"
            )
    return out


def load(project: KitProject) -> Declarations:
    d = Declarations()
    p = d.problems

    derived_file = project.folder("silver") / "derived.toml"
    if derived_file.is_file():
        label = _rel(project, derived_file)
        for i, item in enumerate(_toml(derived_file, p, label).get("derived", [])):
            name = str(item.get("name", "")).strip()
            expr = str(item.get("expression", ""))
            m = _EXPRESSION.match(expr)
            if not name:
                p.append(f"{label} [[derived]] #{i + 1}: give it a name")
                continue
            if not m:
                p.append(
                    f"{label} {name}: expression {expr!r} must be `<a> <op> <b>`, "
                    f"op one of + - * /, each side a channel name or a number"
                )
                continue
            if not item.get("unit"):
                p.append(
                    f"{label} {name}: declare its unit (a value without one is not a fact); "
                    f'write unit = "unknown" if it truly is not known'
                )
            d.derived.append(
                Derived(
                    name=name,
                    expression=expr.strip(),
                    left=m.group(1),
                    op=m.group(2),
                    right=m.group(3),
                    unit=(item.get("unit") if item.get("unit") != "unknown" else None),
                    role=item.get("role"),
                    description=str(item.get("description", "")),
                    source=derived_file,
                )
            )

    roles_file = project.folder("silver") / "roles.toml"
    if roles_file.is_file():
        d.roles = {
            str(k): str(v)
            for k, v in (_toml(roles_file, p, _rel(project, roles_file)).get("roles") or {}).items()
        }

    allowed = {"silver", "gold", project.gold_schema}
    for sql_path in sorted(project.folder("gold").glob("*.sql")):
        name, label = sql_path.stem, _rel(project, sql_path)
        if not _NAME.match(name):
            p.append(
                f"{label}: the file name is the object's name; use lowercase letters, digits and _"
            )
            continue
        sql = sql_path.read_text(encoding="utf-8")
        p.extend(f"{label}: {why}" for why in sql_problems(sql, allowed_schemas=allowed))
        meta_path = sql_path.with_suffix(".toml")
        meta = _toml(meta_path, p, _rel(project, meta_path)) if meta_path.is_file() else {}
        if not meta.get("description"):
            p.append(
                f"{label}: add {sql_path.stem}.toml with a description; it is what chat reads "
                f'to answer "what data do we have"'
            )
        d.gold.append(
            GoldObject(
                name=name,
                sql=sql.strip().rstrip(";"),
                title=str(meta.get("title", name)),
                description=str(meta.get("description", "")),
                grain=str(meta.get("grain", "")),
                source=sql_path,
            )
        )

    names = {g.name for g in d.gold}
    for verb_path in sorted(project.folder("verbs").glob("*.toml")):
        label = _rel(project, verb_path)
        doc = _toml(verb_path, p, label)
        name = str(doc.get("name", verb_path.stem))
        obj = str(doc.get("object", ""))
        if obj not in names:
            p.append(
                f"{label}: object = {obj!r} is not a gold object here ({', '.join(sorted(names)) or 'none'})"
            )
        if not doc.get("description"):
            p.append(f"{label}: add a description; chat shows it when deciding to call this verb")
        sql = str(doc.get("sql", ""))
        p.extend(
            f"{label}: {why}"
            for why in sql_problems(
                sql.replace("{object}", f"{project.gold_schema}.x"), allowed_schemas=allowed
            )
        )
        d.verbs.append(
            Verb(
                name=name,
                description=str(doc.get("description", "")),
                obj=obj,
                sql=sql,
                params={str(k): dict(v) for k, v in (doc.get("params") or {}).items()},
                source=verb_path,
            )
        )

    for chart_path in sorted(project.folder("charts").glob("*.json")):
        label = _rel(project, chart_path)
        try:
            spec = json.loads(chart_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            p.append(f"{label}: not valid JSON: {exc}")
            continue
        for key in ("object", "x", "y"):
            if not spec.get(key):
                p.append(f'{label}: needs "{key}"')
        if spec.get("object") and spec["object"] not in names:
            p.append(f"{label}: object {spec['object']!r} is not a gold object here")
        d.charts.append(Chart(name=chart_path.stem, spec=spec, source=chart_path))
    return d


__all__ = ["Chart", "Declarations", "Derived", "GoldObject", "Verb", "load", "sql_problems"]
