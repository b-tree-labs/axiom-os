#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Render the skill-chaining debt report (ADR-185). Generated, never committed (#1774).

    python scripts/skill_chaining_debt.py              # print the report
    python scripts/skill_chaining_debt.py --out PATH   # write it to a file (CI publishes it)
    python scripts/skill_chaining_debt.py --seed       # print the enforcing debt list

The enforcing list (``tests/infra/skill_chaining_debt.txt``) is edited by hand,
by deleting a skill's line once the skill is clean. ``--seed`` exists only to
create it the first time; it is never run to re-grow the list.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from axiom.infra.skill_chain_lint import (  # noqa: E402
    debt_lines,
    lint,
    render_report,
    unbound_lines,
)


def main(argv: list[str]) -> int:
    result = lint()
    if "--seed" in argv:
        print("\n".join(debt_lines(result) + unbound_lines(result)))
        return 0
    text = render_report(result)
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"wrote {out}")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
