#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Show what the table surface actually looks like.

A visual change is not reviewable from a diff or a test name. Tests prove a
table has the right columns; they do not prove it reads well. Alignment,
density, wording and what draws the eye are judgements a person makes, and
once merged they are what a partner sees.

So this prints the cases worth judging, together, in one screen:

    python scripts/preview_table_spec.py

Nothing here is a test. It is a thing to look at.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from axiom.extensions.builtins.scidisplay import table_spec as T  # noqa: E402

READINGS = (
    ("ts", "Time", True, "left"),
    ("channel", "Channel", True, "left"),
    ("value", "Value", True, "right"),
    ("unit", "Unit", False, "left"),
    ("source_class", "Class", True, "left"),
)

ROWS = [
    {"ts": "2026-09-21T10:00:00.000Z", "channel": "inlet_temp_c", "value": 51.24,
     "unit": "degC", "source_class": "measured"},
    {"ts": "2026-09-21T10:00:01.000Z", "channel": "flow_gpm", "value": 12.4,
     "unit": "gal/min", "source_class": "measured"},
    {"ts": "2026-09-21T10:00:02.000Z", "channel": "valve_position_percent",
     "value": 45.0, "unit": "pct", "source_class": "measured"},
    {"ts": "2026-09-21T10:00:03.000Z", "channel": "pump_hz", "value": 1234.5678,
     "unit": None, "source_class": "measured"},
    {"ts": "2026-09-21T10:00:04.000Z", "channel": "rom_predicted_outlet_temp_c",
     "value": None, "unit": "degC", "source_class": "predicted"},
]


def show(label: str, why: str, **kw) -> None:
    print(f"\n\033[1m{label}\033[0m")
    print(f"  \033[2m{why}\033[0m\n")
    rows = kw.pop("rows", ROWS)
    out = T.tabulate(rows, columns=READINGS, title="senna-loop — readings", **kw)
    # Do not prefix blank lines — it leaves trailing whitespace, which
    # is invisible here and shows up in a diff later.
    print("\n".join(("  " + ln) if ln else "" for ln in out["text"].splitlines()))


def main() -> int:
    print("\033[1m" + "=" * 72)
    print("  TABLE SURFACE — what a person actually sees")
    print("=" * 72 + "\033[0m")

    show("1. Default", "title, header, rule, rows, paging footer")
    show("2. Sorted ascending", "the arrow is part of the heading and must not shift the columns after it",
         sort="value", direction="asc")
    show("3. Sorted descending", "same, other direction; absent values sort LAST both ways",
         sort="value", direction="desc")
    show("4. Inside a list verb", "no title, no footer — the command name already says what this is",
         show_title=False, show_footer=False)
    show("5. One row", "does the rule still look right at minimum width?",
         rows=ROWS[:1], show_title=False, show_footer=False)
    show("6. Empty", "a table with nothing in it must not look like a table that failed",
         rows=[], show_title=False)
    show("7. Paged", "page 2 of a 5-row set at 2 per page", page=2, page_size=2, sort="ts")
    show("8. Mixed types in a sorted column",
         "a reading and the reason one is missing; must not raise",
         rows=[{"ts": "t", "channel": "c", "value": v, "unit": "u",
                "source_class": "measured"}
               for v in (3.0, "n/a", 1.0, None)],
         sort="value", show_title=False)

    print("\n\033[1m9. Precision\033[0m")
    print("  \033[2mnever a digit the value lacks; never one it has, unless an "
          "uncertainty says so\033[0m\n")
    precision_rows = [
        {"what": "reading as measured", "value": 51.2, "uncertainty": None},
        {"what": "four decimals, all real", "value": 1234.5678, "uncertainty": None},
        {"what": "integer-valued", "value": 45.0, "uncertainty": None},
        {"what": "governed by ±0.01", "value": 51.2456, "uncertainty": 0.01},
        {"what": "governed by ±0.5", "value": 51.2456, "uncertainty": 0.5},
        {"what": "very small", "value": 0.0000001234, "uncertainty": None},
        {"what": "very large", "value": 1.23e18, "uncertainty": None},
        {"what": "no reading", "value": None, "uncertainty": None},
    ]
    out = T.tabulate(
        precision_rows,
        columns=(
            ("what", "What", False, "left"),
            {"id": "value", "label": "Value", "sortable": True, "align": "right",
             "uncertainty_from": "uncertainty"},
            ("uncertainty", "±", False, "right"),
        ),
        title="precision", show_title=False, show_footer=False,
    )
    print("\n".join(("  " + ln) if ln else "" for ln in out["text"].splitlines()))
    print("\n  \033[2m:g would have shown 1234.57 for the second row and could\n"
          "  not have shown the last two at all.\033[0m")

    print("\n\033[1m10. The same spec, as the web component takes it\033[0m")
    print("  \033[2mthese props feed shared/data-table with no translation layer\033[0m\n")
    props = T.tabulate(ROWS, columns=READINGS, title="senna-loop — readings",
                       sort="value", direction="desc")["datatable_props"]
    for key in ("sortColumn", "sortDirection", "pageSize", "page"):
        print(f"    {key:<14} {props[key]!r}")
    print(f"    {'columns':<14} {[c['id'] for c in props['columns']]}")
    print("\n  The terminal above and this list come from ONE spec. If they ever")
    print("  disagree about column order or which column is sorted, that is a bug.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
