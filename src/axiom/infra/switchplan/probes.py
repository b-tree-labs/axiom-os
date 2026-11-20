# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Declared probes: "ready" and "healthy" defined once, as data (ADR-197 D2).

A probe is a declaration, not a function: it can be listed, fingerprinted, shown
by ``axi switch`` and read by the boot-order wait-for, the supervisor and the
gates alike. Six kinds. A probe that cannot be evaluated FAILS; there is no
"unknown" that passes.

``relation_read`` is the one that would have caught the swap of 2026-10-20: it
asks the database planner which relations a read through a NAME would scan, and
requires the new one among them and the old one not.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

KINDS = ("http", "tcp", "command", "file", "relation_read", "callable")
WHEN = ("pre", "in_commit", "post")

__all__ = ["KINDS", "WHEN", "EvalContext", "Probe", "ProbeRegistry", "ProbeResult", "REGISTRY", "evaluate", "fingerprint",
           "relations_scanned", "wait_for"]


@dataclass(frozen=True)
class Probe:
    name: str
    kind: Literal["http", "tcp", "command", "file", "relation_read", "callable"]
    spec: Mapping[str, Any] = field(default_factory=dict)
    when: Literal["pre", "in_commit", "post"] = "pre"
    timeout_s: float = 10.0
    #: ``"read_through"`` marks a probe (of any kind) as the check that a read through
    #: the original name lands on the new relation, which a data plan must declare.
    proves: str = ""
    #: for kind="callable": ``fn(ctx) -> bool | (bool, detail)``. Not serialized.
    fn: Callable[..., Any] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"probe {self.name!r}: kind {self.kind!r} is not one of {', '.join(KINDS)}")
        if self.when not in WHEN:
            raise ValueError(f"probe {self.name!r}: when {self.when!r} is not one of {', '.join(WHEN)}")
        if self.kind == "callable" and self.fn is None:
            raise ValueError(f"probe {self.name!r}: a callable probe needs fn")
        if self.kind == "relation_read" and not (self.spec.get("name") and self.spec.get("must_scan")):
            raise ValueError(f"probe {self.name!r}: relation_read needs spec name and must_scan")

    def declaration(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("fn", None)
        return d


@dataclass
class ProbeResult:
    name: str
    when: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class EvalContext:
    """What a probe may need: a database cursor for ``relation_read``, and the
    plan's own state for a ``callable``."""

    cursor: Any = None
    state: Mapping[str, Any] = field(default_factory=dict)


def fingerprint(probes: Iterable[Probe]) -> str:
    """A stable hash of the declarations. A prove that passed for one set of
    probes does not license a commit that declares another."""
    body = json.dumps(sorted((p.declaration() for p in probes), key=lambda d: (d["name"], d["when"])),
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


# -- relation_read ------------------------------------------------------------


def _tree(cur, oid: int) -> set[int]:
    cur.execute(
        "WITH RECURSIVE t(o) AS (SELECT %s::oid UNION ALL SELECT i.inhrelid FROM pg_inherits i JOIN t ON i.inhparent = t.o) "
        "SELECT o::int8 FROM t", (oid,))
    return {int(r[0]) for r in cur.fetchall()}


def _oid(cur, qualified: str | int) -> int | None:
    if isinstance(qualified, int):  # an oid captured before a rename, which a name cannot survive
        return qualified
    cur.execute("SELECT to_regclass(%s)::oid::int8", (qualified,))
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def relations_scanned(cur, name: str) -> set[int]:
    """The oids of the relations a ``SELECT *`` from ``name`` would scan, read from
    its plan (not run). Raises ``LookupError`` for a relation the plan names and
    we cannot resolve: that is not "nothing"."""
    cur.execute(f"EXPLAIN (FORMAT JSON, VERBOSE) SELECT * FROM {name}")
    plan = cur.fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    found: list[tuple[str | None, str]] = []

    def walk(node: dict) -> None:
        if node.get("Relation Name"):
            found.append((node.get("Schema"), node["Relation Name"]))
        for child in node.get("Plans", ()):
            walk(child)

    walk(plan[0]["Plan"])
    out: set[int] = set()
    for schema, rel in found:
        qualified = f'"{schema}"."{rel}"' if schema else f'"{rel}"'
        oid = _oid(cur, qualified)
        if oid is None:
            raise LookupError(f"the plan reads {qualified}, which cannot be identified")
        out.add(oid)
    return out


def _relation_read(p: Probe, ctx: EvalContext) -> tuple[bool, str]:
    cur = ctx.cursor
    if cur is None:
        return False, "no database cursor was given to evaluate a relation_read probe"
    name = p.spec["name"]
    try:
        scanned = relations_scanned(cur, name)
    except LookupError as exc:
        return False, str(exc)
    want: set[int] = set()
    has_children = False
    for rel in p.spec["must_scan"]:
        oid = _oid(cur, rel)
        if oid is None:
            return False, f"{rel} (must be read through {name}) does not exist"
        tree = _tree(cur, oid)
        has_children = has_children or len(tree) > 1
        want |= tree
    bad: set[int] = set()
    for rel in p.spec.get("must_not_scan", ()):
        oid = _oid(cur, rel)
        if oid is not None:
            bad |= _tree(cur, oid)

    def show(oids: Iterable[int]) -> str:
        cur.execute("SELECT coalesce(string_agg(oid::oid::regclass::text, ', '), 'nothing') FROM unnest(%s::int8[]) oid",
                    (sorted(oids),))
        return cur.fetchone()[0]

    if scanned & bad:
        return False, f"a read through {name} would scan {show(scanned & bad)}, which it must not"
    # An empty relation with no children plans to no scan at all; that is a pass
    # only when nothing in the wanted tree has children to scan.
    if not scanned & want and (scanned or has_children):
        return False, f"a read through {name} would scan {show(scanned)}, not {', '.join(str(r) for r in p.spec['must_scan'])}"
    return True, f"a read through {name} scans {show(scanned & want) if scanned else 'no rows yet'}"


# -- the other kinds ----------------------------------------------------------


def _http(p: Probe, ctx: EvalContext) -> tuple[bool, str]:
    url = p.spec["url"]
    expect = int(p.spec.get("status", 200))
    try:
        with urllib.request.urlopen(url, timeout=p.timeout_s) as r:  # noqa: S310 - declared by the plan's author
            status, body = r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{url}: {exc}"
    if status != expect:
        return False, f"{url} answered {status}, expected {expect}"
    needle = p.spec.get("contains")
    if needle and needle not in body:
        return False, f"{url} answered {status} without {needle!r}"
    return True, f"{url} answered {status}"


def _tcp(p: Probe, ctx: EvalContext) -> tuple[bool, str]:
    host, port = p.spec.get("host", "127.0.0.1"), int(p.spec["port"])
    try:
        with socket.create_connection((host, port), timeout=p.timeout_s):
            return True, f"{host}:{port} accepts connections"
    except OSError as exc:
        return False, f"{host}:{port}: {exc}"


def _command(p: Probe, ctx: EvalContext) -> tuple[bool, str]:
    argv = list(p.spec["argv"])
    expect = int(p.spec.get("exit", 0))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=p.timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{argv[0]}: {exc}"
    if r.returncode != expect:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
        return False, f"{' '.join(argv)} exited {r.returncode}, expected {expect}" + (f": {tail[0][:200]}" if tail else "")
    return True, f"{argv[0]} exited {expect}"


def _file(p: Probe, ctx: EvalContext) -> tuple[bool, str]:
    path = p.spec["path"]
    try:
        st = os.stat(path)
    except OSError as exc:
        return False, f"{path}: {exc}"
    if "contains" in p.spec:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                if p.spec["contains"] not in fh.read(1 << 20):
                    return False, f"{path} does not contain {p.spec['contains']!r}"
        except OSError as exc:
            return False, f"{path}: {exc}"
    if "newer_than" in p.spec and st.st_mtime < float(p.spec["newer_than"]):
        return False, f"{path} is older than required"
    return True, f"{path} is in place"


def _callable(p: Probe, ctx: EvalContext) -> tuple[bool, str]:
    out = p.fn(ctx)  # type: ignore[misc]
    if isinstance(out, tuple):
        return bool(out[0]), str(out[1])
    return bool(out), ""


_EVALUATORS = {"http": _http, "tcp": _tcp, "command": _command, "file": _file,
               "relation_read": _relation_read, "callable": _callable}


def evaluate(p: Probe, ctx: EvalContext | None = None) -> ProbeResult:
    """Run one probe. An exception is a failure carrying its message."""
    ctx = ctx or EvalContext()
    t0 = time.monotonic()
    try:
        ok, detail = _EVALUATORS[p.kind](p, ctx)
    except Exception as exc:  # noqa: BLE001 - a probe that cannot be evaluated fails, with the reason
        ok, detail = False, f"could not be evaluated: {type(exc).__name__}: {exc}"
    return ProbeResult(p.name, p.when, ok, detail, time.monotonic() - t0)


def wait_for(probes: Sequence[Probe], *, timeout_s: float = 60.0, interval_s: float = 0.5,
             ctx: EvalContext | None = None, alive: Callable[[], bool] | None = None) -> list[ProbeResult]:
    """Until every probe passes, ``alive()`` says the thing died, or time runs out.
    The one readiness wait (ADR-197 D2): callers pass the declared probes."""
    deadline = time.monotonic() + timeout_s
    while True:
        results = [evaluate(p, ctx) for p in probes]
        if all(r.ok for r in results) or time.monotonic() >= deadline or (alive is not None and not alive()):
            return results
        time.sleep(interval_s)


class ProbeRegistry:
    """Probes declared by owner (a service, a slot, a switch), read by everyone."""

    def __init__(self) -> None:
        self._by_owner: dict[str, list[Probe]] = {}

    def declare(self, owner: str, probes: Iterable[Probe]) -> None:
        self._by_owner[owner] = list(probes)

    def declared(self, owner: str) -> list[Probe]:
        return list(self._by_owner.get(owner, ()))

    def owners(self) -> list[str]:
        return sorted(self._by_owner)

    def wait_for(self, owner: str, **kw: Any) -> list[ProbeResult]:
        probes = self.declared(owner)
        if not probes:
            raise KeyError(f"no probes are declared for {owner!r}")
        return wait_for(probes, **kw)


#: The process-wide registry the boot-order wait-for, the supervisor and the gates read.
REGISTRY = ProbeRegistry()
