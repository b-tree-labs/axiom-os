# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``emit`` — the one place a verb decides between JSON and a human.

:mod:`.table_spec` settled what a table *is*. This settles the step every
CLI verb performs immediately after: it holds a result and a ``--json``
flag, and has to turn the two into output.

Left to each extension, that step is four lines of obvious code — which is
why it gets rewritten per extension and drifts. The drift is not
theoretical. One extension's version was::

    def _emit(value, as_json):
        print(json.dumps(value, indent=2, default=str))
        return 0 if as_json else 0

It printed JSON down both paths, so eight verbs had no human output at
all and ``--json`` changed nothing. Nobody saw it, because the obvious
code is exactly the code nobody reads twice.

So the decision lives here once:

- ``--json`` prints JSON and nothing else — no banner, no indent, so a
  pipe into ``jq`` works. That is the entire reason the flag exists.
- Otherwise the value is rendered for a person: rows become a table
  through the spec, a single record becomes labelled fields, and an empty
  result says it is empty instead of printing a heading over nothing.

What it deliberately does not do is guess. A caller that declares columns
gets those columns in that order; a caller that declares none gets the
keys of the first row, which is a fallback rather than a feature.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from axiom.infra.cli_format import _STANDARD_INDENT, kv_line

from .table_spec import tabulate

__all__ = ["emit"]


def _is_row_list(value: Any) -> bool:
    """A sequence of records — the shape a table can be built from.

    Strings and mappings are sequences too, and neither is a list of rows;
    excluding them here keeps the caller from having to.
    """
    if isinstance(value, (str, bytes, Mapping)):
        return False
    if not isinstance(value, Sequence):
        return False
    return all(isinstance(item, Mapping) for item in value)


def _indented(text: str, indent: int) -> str:
    """Indent RAW text only.

    Not for anything from ``cli_format``: ``table`` and ``kv_line`` already
    apply the standard indent themselves, and re-applying it here is what
    printed every table four spaces deep instead of two.
    """
    pad = " " * indent
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def emit(
    value: Any,
    *,
    as_json: bool,
    title: str = "",
    columns: Any = (),
    indent: int = _STANDARD_INDENT,
    empty: str = "",
    show_title: bool = False,
    show_footer: bool = False,
    page: int = 1,
    page_size: int | None = None,
    sort: str = "",
    direction: str = "asc",
    stream: TextIO | None = None,
) -> int:
    """Print ``value`` the way the caller's ``--json`` flag asked for.

    Returns the verb's exit code, which is ``0`` — a verb with something
    to report has succeeded. Failures are the caller's to print and are
    not routed through here, because an error belongs on stderr and this
    writes to stdout.
    """
    out = stream if stream is not None else sys.stdout

    if as_json:
        # No indent and no heading: the output is for a program.
        print(json.dumps(value, indent=2, default=str), file=out)
        return 0

    noun = title or "rows"

    if value is None:
        print(_indented(empty or f"no {noun}", indent), file=out)
        return 0

    if _is_row_list(value):
        rows = list(value)
        if not rows:
            print(_indented(empty or f"no {noun}", indent), file=out)
            return 0
        declared = columns or tuple(
            (key, key, True, "right" if isinstance(rows[0][key], (int, float)) else "left")
            for key in rows[0]
        )
        text = tabulate(
            rows,
            columns=declared,
            title=noun,
            show_title=show_title,
            show_footer=show_footer,
            page=page,
            # A verb that names no page means "what I fetched", not "the
            # first 25 of it" — silently dropping rows the caller already
            # paid to read is worse than a long table.
            page_size=page_size or max(len(rows), 1),
            sort=sort,
            direction=direction,
        )["text"]
        # already indented by cli_format.table — see _indented's docstring
        print(text, file=out)
        return 0

    if isinstance(value, Mapping):
        if not value:
            print(_indented(empty or f"no {noun}", indent), file=out)
            return 0
        width = max(len(str(k)) for k in value) + 2  # +1 colon, +1 space
        for key, item in value.items():
            # kv_line carries its own two-space indent
            print(kv_line(str(key), str(item), key_width=width), file=out)
        return 0

    print(_indented(str(value), indent), file=out)
    return 0
