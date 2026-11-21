# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Run the whole matrix in parallel and print/emit it.

    python -m state_chaos.scan [artifact-id ...]      # from tests/chaos
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from .artifacts import BY_ID
from .matrix import cells, run_cell_tmp, verdict


def scan(only: list[str] | None = None, workers: int = 6):
    todo = [c for c in cells() if not only or c[0] in only]
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(lambda c: run_cell_tmp(*c), todo))


def _main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--regen-known":
        regen_known(args[1:] or None)
        return
    out = scan(args or None)
    rows = [
        {"artifact": c.artifact, "damage": c.damage, "class": c.final, "reader": c.klass,
         "why": c.why, "violation": c.violation, "verdict": verdict(BY_ID[c.artifact], c)}
        for c in out
    ]
    json.dump(rows, sys.stdout, indent=1)


def grid(rows: list[dict]) -> str:
    ab = {"recovered": "R", "degraded-loudly": "D", "silent-default": "s", "crash": "X"}
    arts = list(dict.fromkeys(r["artifact"] for r in rows))
    kinds = list(dict.fromkeys(r["damage"] for r in rows))
    lines = [" " * 22 + " ".join(k[:5].ljust(5) for k in kinds)]
    for a in arts:
        line = a.ljust(22)
        for k in kinds:
            m = [r for r in rows if r["artifact"] == a and r["damage"] == k]
            line += (ab[m[0]["class"]] if m else ".").center(6)
        lines.append(line)
    return "\n".join(lines)


def regen_known(only: list[str] | None = None) -> None:
    """Drop fixed cells from ``known_findings.KNOWN`` (never adds: new failures must be triaged)."""
    import re
    from pathlib import Path

    from .known_findings import KNOWN

    todo = [c for c in cells() if not only or c[0] in only]
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda c: run_cell_tmp(*c), todo))
    still = {(c.artifact, c.damage) for c in results if verdict(BY_ID[c.artifact], c)}
    new = sorted(k for k in still if k not in KNOWN)
    if new:
        print("NEW FAILURES (triage them):", new)
    scoped = {c.artifact for c in results}
    keep = {k: v for k, v in KNOWN.items() if k in still or k[0] not in scoped}
    path = Path(__file__).with_name("known_findings.py")
    text = path.read_text()
    body = "".join(f"    ({a!r}, {d!r}): {f!r},\n" for (a, d), f in sorted(keep.items()))
    text = re.sub(r"KNOWN: dict\[tuple\[str, str\], str\] = \{\n.*?\n\}\n", "KNOWN: dict[tuple[str, str], str] = {\n" + body + "}\n", text, count=1, flags=re.S)
    path.write_text(text)
    print(f"known findings: {len(KNOWN)} -> {len(keep)}")


if __name__ == "__main__":
    _main()
