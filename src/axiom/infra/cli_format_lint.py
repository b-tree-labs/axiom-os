# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Where CLI layout is decided, and how a departure becomes visible.

:mod:`axiom.infra.cli_format` holds the rules — the standard indent,
ANSI-aware widths, terminal-aware layout, wrapping that keeps a wrapped
cell reading as one cell, rules, ``kv_line``, ``section_header``. Sixteen
modules used it. A seventeenth was written beside it by hand, and the
result was two verbs indenting the same table differently.

Nothing caught that because nothing was looking. A module is only a single
source of truth if using it is easy AND departing from it is visible. This
is the visible half, and it is deliberately shared between two callers:

* the in-tree test, which ratchets — existing debt is recorded, anything
  new fails
* ``axi ext lint``, which is the only moment an extension composed in from
  outside this tree is inspected at all

Both ask the same question, because a rule that differs between the code we
write and the code we accept is two rules.

What it looks for is narrow on purpose. Not "don't format strings" — that
would be absurd. Just the two shapes that mean somebody laid out a TABLE by
hand:

``{x:<24}``  a hardcoded column width, which cannot adapt to content and
             silently shifts every column after it when a value is wider
``"-" * 90`` a rule drawn by repeating a character, at a width picked by
             hand that matches neither the header nor the rows
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: A padded field: {value:<24}, {n:>8}, {x!s:<38}. Two digits or more,
#: because {x:>2} is a small number lining up, not a column.
PADDED_FIELD = re.compile(r"\{[^{}]*[:!][^{}]*[<>^]\d{2,}[^{}]*\}")

#: A hand-drawn rule: "-" * 80, "─" * width.
DRAWN_RULE = re.compile(r"""["'][-─=_]["']\s*\*\s*\w+""")


@dataclass(frozen=True)
class Offence:
    """One line that lays out its own table."""

    path: Path
    line: int
    text: str
    kind: str  # padded-field | drawn-rule

    @property
    def remedy(self) -> str:
        if self.kind == "padded-field":
            return (
                "size the column from its content: "
                "axiom.infra.cli_format.table(rows, columns)"
            )
        return "axiom.infra.cli_format.table(..., rules=True) draws its own rule"


def scan_text(text: str, path: Path) -> list[Offence]:
    """Offences in one file's source."""
    out: list[Offence] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            # A comment describing the shape is not the shape. Without this
            # the rule flags the documentation that explains the rule.
            continue
        if PADDED_FIELD.search(line):
            out.append(Offence(path, lineno, stripped[:100], "padded-field"))
        elif DRAWN_RULE.search(line):
            out.append(Offence(path, lineno, stripped[:100], "drawn-rule"))
    return out


def scan_tree(root: Path, *, skip_tests: bool = True) -> list[Offence]:
    """Offences under ``root``. Tests are skipped by default: a test that
    asserts on a hand-rolled layout is describing one, not drawing one."""
    out: list[Offence] = []
    for path in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        if skip_tests and ("tests" in path.parts or path.name.startswith("test_")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        out.extend(scan_text(text, path))
    return out


__all__ = ["DRAWN_RULE", "PADDED_FIELD", "Offence", "scan_text", "scan_tree"]
