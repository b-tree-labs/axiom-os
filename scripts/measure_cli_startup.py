#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Measure what starting the CLI costs: wall, CPU, modules loaded.

Usage:
    python scripts/measure_cli_startup.py                 # table, 3 runs each
    python scripts/measure_cli_startup.py --runs 5 --markdown
    python scripts/measure_cli_startup.py --importtime help   # top imports for one scenario

Each scenario runs in a fresh interpreter. CPU time (user+sys) is read from
the child's rusage, which is far steadier than wall time on a shared runner,
so it is what the budget tests in ``tests/cli/test_startup_budget.py`` assert
on. Wall is reported because it is what a person waits for.
"""

from __future__ import annotations

import argparse
import os
import re
import resource
import subprocess
import sys
import tempfile
import time

#: name -> (argv after the interpreter, extra env). Completion scenarios use the
#: same environment contract argcomplete's shell hook sets.
SCENARIOS: dict[str, tuple[list[str], dict[str, str]]] = {
    "version": (["-m", "axiom.axiom_cli", "--version"], {}),
    "help": (["-m", "axiom.axiom_cli", "--help"], {}),
    "ext-verb-help": (["-m", "axiom.axiom_cli", "ext", "--help"], {}),
    "complete-top": ([], {"_COMP_LINE": "axi "}),
    "complete-sub": ([], {"_COMP_LINE": "axi signal "}),
}

HEAVY = ("numpy", "pandas", "scipy", "sklearn", "fastapi", "sqlalchemy", "mcp", "uvicorn", "starlette")


def _env_for(extra: dict[str, str]) -> tuple[dict[str, str], str | None]:
    env = {**os.environ, "AXI_NO_SOURCE_WARNING": "1", "AXIOM_DIAGNOSES_QUIET": "1",
           "AXIOM_DISABLE_UPDATE_NUDGE": "1"}
    out_file = None
    if "_COMP_LINE" in extra:
        line = extra["_COMP_LINE"]
        fd, out_file = tempfile.mkstemp(suffix=".completions")
        os.close(fd)
        env.update({
            "_ARGCOMPLETE": "1", "_ARGCOMPLETE_IFS": "\x0b",
            "COMP_LINE": line, "COMP_POINT": str(len(line)),
            "_ARGCOMPLETE_STDOUT_FILENAME": out_file,
        })
    return env, out_file


def run_once(name: str, extra_py: list[str] | None = None) -> dict:
    argv, extra = SCENARIOS[name]
    argv = argv or ["-m", "axiom.axiom_cli"]
    env, out_file = _env_for(extra)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    done = subprocess.run([sys.executable, *(extra_py or []), *argv], env=env,
                          capture_output=True, text=True, timeout=300)
    wall = time.perf_counter() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if out_file:
        os.unlink(out_file)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return {"wall": wall, "cpu": cpu, "stderr": done.stderr, "rc": done.returncode}


def modules_loaded(name: str) -> tuple[int, list[str]]:
    """Count sys.modules and name the heavy top-level packages after a scenario."""
    argv, extra = SCENARIOS[name]
    env, out_file = _env_for(extra)
    # argcomplete ends a completion call with os._exit, which would skip the
    # report; wrap it so the count is printed first.
    probe = (
        "import os, sys, runpy\n"
        "_real_exit = os._exit\n"
        f"def _report():\n"
        f"    heavy = sorted({{m.split('.')[0] for m in sys.modules}} & set({list(HEAVY)!r}))\n"
        "    print('MODS', len(sys.modules), ','.join(heavy), file=sys.stderr)\n"
        "def _exit(code):\n    _report()\n    _real_exit(code)\n"
        "os._exit = _exit\n"
        f"sys.argv = ['axi'] + {argv[2:]!r}\n"
        "try:\n    runpy.run_module('axiom.axiom_cli', run_name='__main__')\n"
        "except SystemExit:\n    pass\n"
        "_report()\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=300)
    if out_file:
        os.unlink(out_file)
    m = re.search(r"MODS (\d+) ?(\S*)", done.stderr)
    return (int(m.group(1)), [h for h in m.group(2).split(",") if h]) if m else (-1, [])


def importtime_top(name: str, top: int = 30) -> str:
    r = run_once(name, ["-X", "importtime"])
    rows = []
    for line in r["stderr"].splitlines():
        m = re.match(r"import time:\s+(\d+) \|\s+(\d+) \|\s*(.*)", line)
        if m:
            rows.append((int(m.group(2)), int(m.group(1)), m.group(3)))
    rows.sort(reverse=True)
    out = [f"{len(rows)} modules imported; top {top} by cumulative time"]
    out += [f"{c / 1000:9.1f} ms cum {s / 1000:8.1f} ms self  {n}" for c, s, n in rows[:top]]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--importtime", metavar="SCENARIO", choices=sorted(SCENARIOS))
    args = ap.parse_args()
    if args.importtime:
        print(importtime_top(args.importtime))
        return 0
    rows = []
    for name in SCENARIOS:
        run_once(name)  # warm the page cache / pyc; the first run is reported separately
        runs = [run_once(name) for _ in range(args.runs)]
        mods, heavy = modules_loaded(name)
        rows.append((name, min(r["wall"] for r in runs), min(r["cpu"] for r in runs), mods, heavy))
    if args.markdown:
        print("| scenario | wall s (best) | cpu s (best) | modules | heavy loaded |\n|---|---|---|---|---|")
        for n, w, c, m, h in rows:
            print(f"| {n} | {w:.2f} | {c:.2f} | {m} | {', '.join(h) or '-'} |")
    else:
        print(f"{'scenario':16}{'wall':>8}{'cpu':>8}{'modules':>9}  heavy")
        for n, w, c, m, h in rows:
            print(f"{n:16}{w:8.2f}{c:8.2f}{m:9d}  {', '.join(h) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
