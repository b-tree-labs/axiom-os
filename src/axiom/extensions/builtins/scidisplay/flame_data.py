# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A hierarchy of named values, for a flame or icicle graph.

A node is ``{"name", "value", "self"?, "category"?, "children"?}`` and its
children sum to at most its value: what is left over is the node's own
(``self``) share. Three forms arrive and all become that one tree:

- **a nested tree**, as above;
- **folded stacks**, ``a;b;c 42`` per line: Brendan Gregg's format, the one
  ``perf script | stackcollapse``, ``py-spy record --format raw`` and
  async-profiler's collapsed output all write;
- **parent-id rows**, ``[{id, parent, name, value, self?, category?}]``: what a
  query over a stored hierarchy returns.

Then the operations a reader performs on one: :func:`invert` (the reverse
flame graph: who calls this frame), :func:`subtree` (zoom to a path),
:func:`search` (which frames match, and what share of the whole they are,
counting a match inside another match once), and :func:`layout`, which places
every frame at least ``min_px`` wide and folds the narrower ones of each parent
into one ``other`` sliver, so fifty thousand frames draw as the few thousand a
screen can show.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

OTHER = "(other)"


class HierarchyError(ValueError):
    """A hierarchy that cannot be read, and why."""


@dataclass
class Node:
    name: str
    value: float
    self_value: float = 0.0
    category: str | None = None
    children: list[Node] = field(default_factory=list)

    def child(self, name: str) -> Node:
        for c in self.children:
            if c.name == name:
                return c
        c = Node(name, 0.0)
        self.children.append(c)
        return c

    def count(self) -> int:
        n, stack = 0, [self]
        while stack:
            node = stack.pop()
            n += 1
            stack.extend(node.children)
        return n

    def depth(self) -> int:
        best, stack = 0, [(self, 1)]
        while stack:
            node, d = stack.pop()
            best = max(best, d)
            stack.extend((c, d + 1) for c in node.children)
        return best

    def to_document(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "value": _clean(self.value)}
        if self.self_value:
            out["self"] = _clean(self.self_value)
        if self.category:
            out["category"] = self.category
        if self.children:
            out["children"] = [c.to_document() for c in self.children]
        return out


def _clean(v: float) -> float | int:
    return int(v) if float(v).is_integer() else round(float(v), 6)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _finish(node: Node) -> None:
    """Fill values and self shares bottom-up; a node is at least its children."""
    order, stack = [], [node]
    while stack:
        n = stack.pop()
        order.append(n)
        stack.extend(n.children)
    for n in reversed(order):
        kids = sum(c.value for c in n.children)
        if n.value == 0 or n.value < kids:
            # No value given (or one smaller than its children): the node is
            # its children plus its own share.
            n.value = kids + n.self_value
        n.self_value = max(0.0, n.value - kids)


def from_tree(raw: Mapping[str, Any]) -> Node:
    """A nested ``{name, value, children}`` document as a tree."""
    if not isinstance(raw, Mapping):
        raise HierarchyError("a tree is a table with a name and a value")

    def build(d: Mapping[str, Any], where: str) -> Node:
        name = d.get("name")
        if not isinstance(name, str) or not name:
            raise HierarchyError(f"{where}: every frame has a name")
        value = _num(d.get("value", 0)) or 0.0
        if value < 0:
            raise HierarchyError(f"{where} ({name}): a value cannot be negative")
        node = Node(name, value, _num(d.get("self")) or 0.0,
                    d.get("category") if isinstance(d.get("category"), str) else None)
        for i, c in enumerate(d.get("children") or []):
            node.children.append(build(c, f"{where}.children[{i}]"))
        if node.value == 0 and node.self_value:
            node.value = node.self_value
        return node

    root = build(raw, "tree")
    _finish(root)
    return root


_FOLDED = re.compile(r"^(?P<stack>.*\S)\s+(?P<count>-?\d+(?:\.\d+)?)\s*$")


def from_folded(text: str | Iterable[str], *, root: str = "all") -> Node:
    """Folded stacks (``a;b;c 42`` per line) as a tree. Blank lines and ``#`` comments are skipped."""
    lines = text.splitlines() if isinstance(text, str) else list(text)
    top = Node(root, 0.0)
    bad = 0
    # Children by name while building: a profile's hot frames have hundreds of
    # callees, and a linear search per frame per line was the whole cost.
    index: dict[int, dict[str, Node]] = {}
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _FOLDED.match(line)
        if not m:
            bad += 1
            continue
        count = float(m.group("count"))
        if count <= 0:
            continue
        node = top
        node.value += count
        for frame in m.group("stack").split(";"):
            name = frame or "?"
            kids = index.get(id(node))
            if kids is None:
                kids = index[id(node)] = {}
            nxt = kids.get(name)
            if nxt is None:
                nxt = kids[name] = Node(name, 0.0)
                node.children.append(nxt)
            node = nxt
            node.value += count
        node.self_value += count
    if bad and not top.children:
        raise HierarchyError(f"no line read as 'frame;frame;frame count' ({bad} could not be read)")
    _finish(top)
    return top


def from_rows(rows: Sequence[Mapping[str, Any]]) -> Node:
    """Parent-id rows as a tree. Exactly one row has no parent, or a root is made."""
    by_id: dict[Any, Node] = {}
    parents: dict[Any, Any] = {}
    for i, r in enumerate(rows):
        if "id" not in r:
            raise HierarchyError(f"rows[{i}]: every row has an id")
        if r["id"] in by_id:
            raise HierarchyError(f"rows[{i}]: id {r['id']!r} appears twice")
        by_id[r["id"]] = Node(str(r.get("name") or r["id"]), _num(r.get("value")) or 0.0,
                              _num(r.get("self")) or 0.0,
                              r.get("category") if isinstance(r.get("category"), str) else None)
        parents[r["id"]] = r.get("parent")
    roots = []
    for rid, pid in parents.items():
        if pid in (None, ""):
            roots.append(by_id[rid])
        elif pid not in by_id:
            raise HierarchyError(f"row {rid!r} names parent {pid!r}, which is not a row")
        else:
            by_id[pid].children.append(by_id[rid])
    # A cycle leaves nodes unreachable from any root.
    reach = sum(r.count() for r in roots)
    if reach != len(by_id):
        raise HierarchyError("the rows form a cycle; a hierarchy has one way up from every frame")
    if len(roots) == 1:
        root = roots[0]
    else:
        root = Node("all", 0.0, children=roots)
    _finish(root)
    return root


_READ_CACHE: dict[str, Node] = {}
_READ_CACHE_MAX = 8


def read(data: Mapping[str, Any]) -> Node:
    """The ``data.tree`` block of a spec, whichever form it carries.

    Read once per distinct block: validation and drawing both need the tree,
    and a moved control redraws the same one. Callers never mutate a tree
    (invert, subtree and layout all build their own), so sharing is safe.
    """
    import hashlib
    import json

    key = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    hit = _READ_CACHE.get(key)
    if hit is not None:
        return hit
    tree = _read(data)
    if len(_READ_CACHE) >= _READ_CACHE_MAX:
        _READ_CACHE.pop(next(iter(_READ_CACHE)))
    _READ_CACHE[key] = tree
    return tree


def _read(data: Mapping[str, Any]) -> Node:
    if "root" in data:
        return from_tree(data["root"])
    if "folded" in data:
        return from_folded(data["folded"], root=str(data.get("name") or "all"))
    if "rows" in data:
        return from_rows(data["rows"])
    raise HierarchyError("data.tree carries one of root, folded or rows")


def subtree(root: Node, path: Sequence[str]) -> tuple[Node, list[Node]]:
    """The node at *path* (names below the root) and its ancestors, root first.

    A path that leaves the tree stops at the deepest frame it reached, so a
    stale link still opens near where it pointed rather than at nothing.
    """
    node, chain = root, [root]
    for name in path:
        nxt = next((c for c in node.children if c.name == name), None)
        if nxt is None:
            break
        node = nxt
        chain.append(node)
    return node, chain[:-1]


def invert(root: Node) -> Node:
    """The reverse flame graph: each frame's callers above it.

    Built from self shares: every frame that did work of its own contributes
    that work along its stack read backwards, so the bottom row is where time
    was actually spent and the rows above are who called it.
    """
    out = Node(root.name, 0.0)
    stack: list[tuple[Node, list[Node]]] = [(c, []) for c in root.children]
    while stack:
        node, above = stack.pop()
        line = [*above, node]
        if node.self_value > 0:
            cur = out
            cur.value += node.self_value
            for frame in reversed(line):
                cur = cur.child(frame.name)
                if frame.category and not cur.category:
                    cur.category = frame.category
                cur.value += node.self_value
            cur.self_value += node.self_value
        stack.extend((c, line) for c in node.children)
    if root.self_value:
        out.value += root.self_value
    _finish(out)
    return out


@dataclass
class Match:
    count: int
    value: float


def search(root: Node, term: str) -> tuple[set[int], Match]:
    """Frames whose name contains *term* (case-insensitive), by ``id(node)``.

    The share counts a match inside another match once: a recursive function
    is not twice as expensive because it appears twice on one stack.
    """
    needle = term.strip().lower()
    hits: set[int] = set()
    if not needle:
        return hits, Match(0, 0.0)
    total = 0.0
    stack: list[tuple[Node, bool]] = [(root, False)]
    count = 0
    while stack:
        node, inside = stack.pop()
        hit = needle in node.name.lower()
        if hit:
            hits.add(id(node))
            count += 1
            if not inside:
                total += node.value
        stack.extend((c, inside or hit) for c in node.children)
    return hits, Match(count, total)


@dataclass
class Frame:
    node: Node | None
    name: str
    value: float
    x: float          # left edge, 0..1 of the drawn width
    w: float          # width, 0..1
    depth: int
    parent_value: float
    path: tuple[str, ...]
    merged: int = 0   # frames folded into an "other" sliver


def layout(top: Node, *, width_px: float, min_px: float = 1.0, max_depth: int = 400,
           ancestors: Sequence[Node] = ()) -> tuple[list[Frame], int]:
    """Every drawable frame, and how many were folded into slivers.

    ``top`` spans the full width. Children narrower than ``min_px`` are summed
    per parent into one ``(other)`` frame, drawn only if the sum reaches
    ``min_px``. ``ancestors`` (root first) are drawn as full-width rows beneath
    a zoomed frame, as flamegraph.pl does, so a reader sees where it sits.
    """
    frames: list[Frame] = []
    folded = 0
    base_path = tuple(a.name for a in ancestors[1:]) if ancestors else ()
    for d, a in enumerate(ancestors):
        frames.append(Frame(a, a.name, a.value, 0.0, 1.0, d,
                            ancestors[d - 1].value if d else a.value, tuple(x.name for x in ancestors[1:d + 1])))
    d0 = len(ancestors)
    total = top.value or 1.0
    own_path = base_path + ((top.name,) if ancestors else ())
    frames.append(Frame(top, top.name, top.value, 0.0, 1.0, d0,
                        ancestors[-1].value if ancestors else top.value, own_path))
    unit = width_px / total
    stack: list[tuple[Node, float, int, tuple[str, ...]]] = [(top, 0.0, d0, own_path)]
    while stack:
        node, x, depth, path = stack.pop()
        if depth - d0 >= max_depth:
            continue
        cx = x
        small_value, small_n, small_x = 0.0, 0, None
        for c in node.children:
            w = c.value / total
            if c.value * unit < min_px:
                small_value += c.value
                small_n += c.count()
                small_x = cx if small_x is None else small_x
                cx += w
                continue
            cpath = path + (c.name,)
            frames.append(Frame(c, c.name, c.value, cx, w, depth + 1, node.value, cpath))
            stack.append((c, cx, depth + 1, cpath))
            cx += w
        if small_n:
            folded += small_n
            if small_value * unit >= min_px:
                frames.append(Frame(None, OTHER, small_value, small_x or x, small_value / total, depth + 1,
                                    node.value, path + (OTHER,), merged=small_n))
                folded -= 1 if small_n == 1 else 0
    return frames, folded


__all__ = ["Frame", "HierarchyError", "Match", "Node", "OTHER", "from_folded", "from_rows", "from_tree",
           "invert", "layout", "read", "search", "subtree"]
