# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Expand/contract for database migrations (ADR-170, "never down from our own causes").

Both releases run against one database during a blue/green deploy, so a new
migration may only add. A contracting change (dropping or renaming a table or
column, or making a column NOT NULL) must carry

    # contract: <what> unused since <released version>

naming a version that is already released. CI runs ``python -m
axiom.infra.deploy.migration_guard`` over the migrations added since the last
release tag.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

#: Contracting SQL, matched against the literal text of a raw statement.
_SQL = [
    (re.compile(r"\bDROP\s+(COLUMN|TABLE)\b", re.I), "drops a column or table (SQL)"),
    (re.compile(r"\bRENAME\s+(COLUMN|TO)\b", re.I), "renames (SQL)"),
    (re.compile(r"\bSET\s+NOT\s+NULL\b", re.I), "makes a column NOT NULL (SQL)"),
    (re.compile(r"\bALTER\s+COLUMN\b.*\bTYPE\b", re.I | re.S), "changes a column's type (SQL)"),
]
_MARKER = re.compile(r"#\s*contract:\s*(?P<what>.+?)\s+unused since\s+(?P<version>\d+\.\d+\.\d+)")


def _op(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "op":
        return node.attr
    return ""


def _kw(call: ast.Call, key: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == key), None)


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _not_null_without_default(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) != "Column":
        return False
    return _is_false(_kw(node, "nullable")) and _kw(node, "server_default") is None


def _literal_sql(node: ast.expr) -> str:
    """A statement's literal text: constants, f-string parts, concatenations.

    Interpolated parts (a schema, an index name) read as a placeholder, which is
    what lets ``f"CREATE INDEX ... ON {schema}.t"`` be read at all.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            " _ " if isinstance(v, ast.FormattedValue) else _literal_sql(v) for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _literal_sql(node.left) + " " + _literal_sql(node.right)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return _literal_sql(node.func.value)  # "...".format(...)
        if node.args:
            return _literal_sql(node.args[0])  # sa.text("...")
    return ""


def contracting_operations(text: str) -> list[str]:
    """What ``upgrade()`` does that the previous release cannot run against.

    Read from the syntax tree rather than by line: a call split over lines, a
    keyword after a nested call, and a statement assembled from parts are all
    ordinary migration code. ``downgrade()`` is not read: it may undo what its
    upgrade added. A migration that does not parse cannot be checked, and so
    does not pass.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"cannot be parsed ({exc.msg}), so it cannot be checked"]
    upgrade = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None
    )
    if upgrade is None:
        return []
    found: list[str] = []
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        name = _op(node.func)
        if name == "drop_column":
            found.append("drops a column")
        elif name == "drop_table":
            found.append("drops a table")
        elif name == "rename_table":
            found.append("renames a table")
        elif name == "alter_column":
            if _kw(node, "new_column_name") is not None:
                found.append("renames a column")
            if _is_false(_kw(node, "nullable")):
                found.append("makes a column NOT NULL")
            if _kw(node, "type_") is not None:
                found.append("changes a column's type")
        elif name == "add_column":
            if any(_not_null_without_default(a) for a in node.args):
                found.append("adds a NOT NULL column with no default")
        elif name == "execute":
            sql = _literal_sql(node.args[0]) if node.args else ""
            if not sql.strip(" _"):
                found.append("runs SQL with no literal text to check")
            else:
                found.extend(why for rx, why in _SQL if rx.search(sql))
    return list(dict.fromkeys(found))


def guard(paths: list[Path], *, released: set[str]) -> list[str]:
    problems = []
    for p in paths:
        text = p.read_text(encoding="utf-8")
        ops = contracting_operations(text)
        if not ops:
            continue
        markers = list(_MARKER.finditer(text))
        if not markers:
            problems.append(
                f"{p}: {', '.join(ops)} without '# contract: <what> unused since <released version>'. "
                "Both releases run against one database during a deploy: add now, drop in a later release."
            )
            continue
        for mk in markers:
            if mk["version"] not in released:
                problems.append(
                    f"{p}: contract names {mk['version']}, which is not a released version"
                )
    return problems


def _released() -> set[str]:
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"], capture_output=True, text=True, check=False
    ).stdout
    return {t[1:] for t in tags.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)}


def _changed_since_last_release() -> list[Path]:
    last = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    rng = [f"{last}...HEAD"] if last else []
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", *rng],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    added = [
        Path(f)
        for f in out.split()
        if re.search(r"/migrations/(versions/)?[^/]+\.py$", f)
        and not f.endswith("__init__.py")
        and Path(f).exists()
    ]
    if not last:
        return added
    # An extension the last release does not have: that release reads none of
    # its tables, so its own migrations cannot break it.
    released = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", last],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    released_dirs = {Path(f).parent.as_posix() for f in released}
    return [p for p in added if p.parent.as_posix() in released_dirs]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "paths",
        nargs="*",
        help="migrations to check (default: those added since the last release tag)",
    )
    a = ap.parse_args(argv)
    paths = [Path(p) for p in a.paths] or _changed_since_last_release()
    problems = guard(paths, released=_released())
    for p in problems:
        print(p, file=sys.stderr)
    print(f"migration guard: {len(paths)} checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
