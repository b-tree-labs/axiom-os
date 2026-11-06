# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Budgets for what starting the CLI may cost.

Partners run the CLI on old, low-power machines, so every `axi` call pays the
startup cost there. These budgets are written so they do not flap on a shared
CI runner: they assert CPU time (child rusage, not wall) with generous
ceilings, and the real guard is structural -- which heavy packages are in
``sys.modules`` after the call. A regression that drags numpy/sklearn/FastAPI
into `--help` or into tab completion is caught by import set, not by timing.

Measure with ``python scripts/measure_cli_startup.py``.
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
import tempfile

import pytest

#: Packages that no help listing, version report or tab completion may load.
HEAVY = (
    "numpy", "pandas", "scipy", "sklearn", "fastapi", "sqlalchemy",
    "mcp", "uvicorn", "starlette",
)

_PROBE = """
import os, runpy, sys
sys.argv = {argv!r}
_real_exit = os._exit
def _report():
    heavy = sorted({{m.split(".")[0] for m in sys.modules}} & set({heavy!r}))
    print("HEAVY:" + ",".join(heavy), file=sys.stderr)
    print("MODULES:" + str(len(sys.modules)), file=sys.stderr)
def _exit(code):          # argcomplete ends a completion call with os._exit
    _report()
    _real_exit(code)
os._exit = _exit
try:
    runpy.run_module("axiom.axiom_cli", run_name="__main__")
except SystemExit:
    pass
_report()
"""


def _env(complete_line: str | None = None) -> tuple[dict[str, str], str | None]:
    env = {
        **os.environ,
        "AXI_NO_SOURCE_WARNING": "1",
        "AXIOM_DIAGNOSES_QUIET": "1",
        "AXIOM_DISABLE_UPDATE_NUDGE": "1",
    }
    out = None
    if complete_line is not None:
        fd, out = tempfile.mkstemp(suffix=".completions")
        os.close(fd)
        env.update({
            "_ARGCOMPLETE": "1",
            "_ARGCOMPLETE_IFS": "\x0b",
            "COMP_LINE": complete_line,
            "COMP_POINT": str(len(complete_line)),
            "_ARGCOMPLETE_STDOUT_FILENAME": out,
        })
    return env, out


def _measure(argv: list[str], complete_line: str | None = None) -> tuple[float, list[str], int]:
    """Run the CLI in a fresh interpreter. Returns (cpu seconds, heavy loaded, module count)."""
    env, out = _env(complete_line)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    done = subprocess.run(
        [sys.executable, "-c", _PROBE.format(argv=argv, heavy=list(HEAVY))],
        capture_output=True, text=True, env=env, timeout=180,
    )
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if out:
        try:
            os.unlink(out)
        except OSError:
            pass
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    heavy: list[str] = []
    modules = -1
    for line in done.stderr.splitlines():
        if line.startswith("HEAVY:"):
            heavy = [p for p in line[len("HEAVY:"):].split(",") if p]
        elif line.startswith("MODULES:"):
            modules = int(line[len("MODULES:"):])
    assert modules >= 0, f"probe did not report; stderr:\n{done.stderr[-800:]}"
    return cpu, heavy, modules


def test_importing_the_cli_module_loads_no_heavy_package():
    """`import axiom.axiom_cli` is what every entry point pays first."""
    code = (
        "import sys; import axiom.axiom_cli; "
        f"print(','.join(sorted({{m.split('.')[0] for m in sys.modules}} & set({list(HEAVY)!r}))))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.stdout.strip() == "", (
        f"`import axiom.axiom_cli` pulled in {done.stdout.strip()}; the dispatcher module "
        f"must stay free of anything a command needs only when it runs"
    )


def test_version_budget():
    cpu, heavy, _mods = _measure(["axi", "--version"])
    assert heavy == [], f"`axi --version` loaded {heavy}"
    assert cpu < 1.0, f"`axi --version` took {cpu:.2f}s CPU (budget 1.0s)"


def test_help_budget():
    cpu, heavy, _mods = _measure(["axi", "--help"])
    assert heavy == [], f"`axi --help` loaded {heavy}"
    assert cpu < 1.0, f"`axi --help` took {cpu:.2f}s CPU (budget 1.0s)"


@pytest.mark.parametrize("line", ["axi ", "axi signal ", "axi -"])
def test_completion_budget(line, tmp_path, monkeypatch):
    """A TAB press reads the stored completion tree and imports no extension.

    The first press after an install builds that tree (it has to import every
    verb), so it is made once here, in a private state dir, before measuring.
    """
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    _measure(["axi"], complete_line="axi ")  # prime: cold build, not budgeted
    cpu, heavy, _mods = _measure(["axi"], complete_line=line)
    assert heavy == [], f"tab completion for {line!r} loaded {heavy}"
    assert cpu < 0.5, f"tab completion for {line!r} took {cpu:.2f}s CPU (budget 0.5s)"
