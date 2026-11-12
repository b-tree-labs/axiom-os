# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""No data-lane route is an ``async def`` that makes a blocking call (#1827).

FastAPI runs a plain ``def`` handler on a worker thread and an ``async def`` one on the
event loop. A blocking database or network call inside ``async def`` therefore holds every
other request on the server behind it: on a deployed node one slow figure did exactly that,
and the browser gave up on every chart (HTTP 499).

So a data-lane handler that does blocking I/O is a plain ``def``, or it awaits genuinely
async calls. This walks every route handler in the chart and data lanes and fails on an
``async def`` whose body makes a call that looks blocking and is not awaited.
"""

from __future__ import annotations

import ast
from pathlib import Path

import axiom.extensions.builtins as builtins

#: The chart, figure, series, table and data-read lanes.
LANES = ("webapp", "scidisplay", "charts", "data_platform")

_ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete", "api_route", "head", "options"}

#: Call names that block the thread they run on.
_BLOCKING_ATTRS = {
    "execute", "executemany", "fetchall", "fetchone", "fetchmany", "cursor", "commit",
    "scalar", "scalars", "query", "connect", "urlopen", "session_scope", "session_for",
    "read_text", "read_bytes", "write_text", "write_bytes", "forward", "figure", "sleep",
}
_BLOCKING_MODULES = {"requests", "urllib", "psycopg", "psycopg2", "sqlite3", "subprocess", "time"}

#: ``path:function`` -> why it is allowed. Empty today: keep it short, and justify each.
ALLOWED: dict[str, str] = {}


def _is_route(fn: ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in _ROUTE_DECORATORS:
            return True
    return False


def _blocking_calls(fn: ast.AsyncFunctionDef) -> list[str]:
    awaited: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Await):
            awaited.update(id(n) for n in ast.walk(node.value))
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or id(node) in awaited:
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            root = f
            while isinstance(root, ast.Attribute):
                root = root.value
            name = root.id if isinstance(root, ast.Name) else ""
            if f.attr in _BLOCKING_ATTRS or name in _BLOCKING_MODULES:
                found.append(f"{name}.{f.attr}" if name else f.attr)
        elif isinstance(f, ast.Name) and f.id in {"urlopen", "session_scope", "session_for", "open"}:
            found.append(f.id)
    return found


def _offenders() -> list[str]:
    root = Path(builtins.__file__).parent
    out = []
    for lane in LANES:
        for path in sorted((root / lane).rglob("*.py")):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef) and _is_route(node):
                    calls = _blocking_calls(node)
                    key = f"{path.relative_to(root)}:{node.name}"
                    if calls and key not in ALLOWED:
                        out.append(f"{key} (line {node.lineno}): {', '.join(sorted(set(calls)))}")
    return out


def test_no_async_route_in_a_data_lane_makes_a_blocking_call():
    offenders = _offenders()
    assert not offenders, (
        "an async route handler makes a blocking call, which holds the event loop for every "
        "request on the server; make it a plain `def` (FastAPI runs it on a worker thread) or "
        "await a genuinely async call:\n  " + "\n  ".join(offenders))


def test_the_guard_sees_a_blocking_async_handler():
    """The walk itself must catch the shape that stalled a node, or it proves nothing."""
    tree = ast.parse(
        "@router.get('/x')\n"
        "async def figure(site):\n"
        "    with store.session_scope() as s:\n"
        "        return s.execute('select 1').fetchall()\n"
        "@router.get('/y')\n"
        "async def fine(site):\n"
        "    return await lane.read(site)\n")
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    assert all(_is_route(f) for f in fns)
    assert _blocking_calls(fns[0])
    assert not _blocking_calls(fns[1])
