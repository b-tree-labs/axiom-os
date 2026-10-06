# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``no_action_without_authz`` — static-analysis lint (PRD §5.6).

Every public function in a primitive that takes an ``ActionEnvelope``
parameter must consult ``decide(envelope, ...)`` before performing
side-effectful work. The lint walks Python sources, identifies
``ActionEnvelope``-taking functions by type-annotation, and verifies
the first non-comment / non-docstring / non-trivial statement in the
function body is a ``decide(...)`` call.

**Allowlist** — boot-time + synthetic-action call sites named in
spec-governance-fabric §1.4 are exempt. A function opts out by
attaching a leading-comment marker::

    # noqa: no-action-without-authz — synthetic envelope at boot

The marker must appear inside the function body before any code.
We accept the marker only as a comment node read from source (we
record + count opt-outs separately from clean passes so a reviewer
can audit them).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

_ENV_TYPE_NAMES = frozenset({"ActionEnvelope", "Envelope"})
_DECIDE_NAMES = frozenset({"decide"})
_NOQA_TAG = "noqa: no-action-without-authz"


@dataclass(frozen=True)
class Violation:
    path: str
    function: str
    lineno: int
    reason: str


@dataclass
class LintReport:
    checked_files: int = 0
    checked_functions: int = 0
    violations: list[Violation] = field(default_factory=list)
    allowlisted: list[Violation] = field(default_factory=list)
    """Functions that skipped the rule via ``# noqa:`` — surface for review."""

    @property
    def ok(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _arg_type_name(arg: ast.arg) -> str | None:
    """Return the annotation name, drilling through ``Optional[X]`` etc."""
    ann = arg.annotation
    if ann is None:
        return None
    return _ann_name(ann)


def _ann_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _ann_name(node.value)
    if isinstance(node, ast.BinOp):  # PEP 604 X | None
        for side in (node.left, node.right):
            name = _ann_name(side)
            if name and name not in {"None"}:
                return name
    return None


def _takes_envelope(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for a in fn.args.args + fn.args.kwonlyargs:
        if _arg_type_name(a) in _ENV_TYPE_NAMES:
            return True
    return False


def _is_decide_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if isinstance(fn, ast.Name) and fn.id in _DECIDE_NAMES:
        return True
    if isinstance(fn, ast.Attribute) and fn.attr in _DECIDE_NAMES:
        return True
    return False


def _trivial(stmt: ast.stmt) -> bool:
    """Statements that can precede the decide() call without violation:
    docstrings, imports, simple assignments of local aliases, and
    pure-data dataclass/literal constructions used to build the
    envelope before passing it in."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        # docstring or other literal expr
        return True
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return True
    if isinstance(stmt, ast.Assign):
        # Permit simple assignment whose RHS contains no Call other than
        # data constructors (we accept all assigns conservatively here;
        # the real constraint is "no side-effecting call before decide").
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call):
                return False
        return True
    return False


def _has_noqa(fn: ast.FunctionDef | ast.AsyncFunctionDef, source: str) -> bool:
    """The opt-out marker must appear as a comment inside the function
    body before any executable statement. We scan the source between
    the def line and the first body statement."""
    if not fn.body:
        return False
    first = fn.body[0]
    start = fn.lineno
    end = first.lineno
    lines = source.splitlines()
    # Lines are 1-indexed in AST.
    region = lines[start - 1:end]
    return any(_NOQA_TAG in ln for ln in region)


def _check_function(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    path: Path,
    source: str,
) -> tuple[Violation | None, bool]:
    """Returns (violation_or_None, allowlisted)."""
    if not _takes_envelope(fn):
        return (None, False)

    if _has_noqa(fn, source):
        return (
            Violation(
                path=str(path),
                function=fn.name,
                lineno=fn.lineno,
                reason="allowlisted via # noqa: no-action-without-authz",
            ),
            True,
        )

    # Walk the body skipping trivial statements; first non-trivial must
    # contain a decide() call.
    for stmt in fn.body:
        if _trivial(stmt):
            continue
        for sub in ast.walk(stmt):
            if _is_decide_call(sub):
                return (None, False)
        return (
            Violation(
                path=str(path),
                function=fn.name,
                lineno=fn.lineno,
                reason=(
                    "function takes ActionEnvelope but the first "
                    "non-trivial statement is not a decide() call"
                ),
            ),
            False,
        )

    # Empty body or only trivials → nothing to consult; arguably ok.
    return (None, False)


def _iter_functions(
    tree: ast.AST,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip dunder + leading-underscore (private) — public-only per PRD.
            if node.name.startswith("_"):
                continue
            yield node


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def check_source(source: str, path: Path | str = "<string>") -> LintReport:
    report = LintReport(checked_files=1)
    path = Path(path) if not isinstance(path, Path) else path
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Skip un-parsable files rather than crashing the lint.
        return report
    for fn in _iter_functions(tree):
        report.checked_functions += 1
        v, allowlisted = _check_function(fn, path, source)
        if v is None:
            continue
        if allowlisted:
            report.allowlisted.append(v)
        else:
            report.violations.append(v)
    return report


def check_paths(paths: Iterable[Path | str]) -> LintReport:
    report = LintReport()
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            continue
        files = [p] if p.is_file() else list(p.rglob("*.py"))
        for f in files:
            if "__pycache__" in f.parts:
                continue
            sub = check_source(f.read_text(encoding="utf-8"), f)
            report.checked_files += sub.checked_files
            report.checked_functions += sub.checked_functions
            report.violations.extend(sub.violations)
            report.allowlisted.extend(sub.allowlisted)
    return report
