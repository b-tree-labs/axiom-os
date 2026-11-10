# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What printing help is allowed to cost.

`get_parser()` exists only for completion and `--help`; dispatch goes
through importlib separately. It nevertheless imported every builtin
extension's CLI module so argcomplete would see a full tree, and those
packages re-export their serving surface — so asking the CLI for help
imported FastAPI, SQLAlchemy and the MCP SDK.

Measured before this guard: `neut --help` loaded 463 modules and took 1.6 s
warm on a fast SSD. The cost is paid in file reads, so where reads are slow
it stops looking like slowness and starts looking like breakage: on a venv
living on a Windows drive mounted into WSL, a colleague pressed Ctrl-C
during `rich/segment.py` and reported the CLI as hung.

The fix is not to make the imports faster. It is to not do them: a verb's
parser is built when that verb is the one being run, and completion — which
genuinely needs the whole tree — asks for it by setting `_ARGCOMPLETE`.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Packages no help listing has any business loading. Each is a serving
#: dependency pulled in through an extension package's ``__init__``.
HEAVY = ("fastapi", "sqlalchemy", "mcp", "uvicorn", "starlette")

_PROBE = """
import sys
sys.argv = {argv!r}
try:
    from axiom.axiom_cli import main
    main()
except SystemExit:
    pass
except Exception as exc:                      # a broken CLI is a separate failure
    print("PROBE-ERROR:" + type(exc).__name__ + ":" + str(exc)[:200], file=sys.stderr)
loaded = sorted({{m.split(".")[0] for m in sys.modules}} & set({heavy!r}))
print("HEAVY:" + ",".join(loaded), file=sys.stderr)
print("MODULES:" + str(len(sys.modules)), file=sys.stderr)
"""


_STATE: dict[str, str] = {}


def _isolated_state() -> dict[str, str]:
    """One private, already-warm state tree for every probe in this module.

    A probe that reads the worker's real state dir measures whatever ran
    before it: on a cold one the first `axi --help` also builds the startup
    index cache and writes update-state, and those imports were counted
    against the parser. Warm it once in both modes, then measure."""
    if not _STATE:
        import os
        import tempfile

        root = tempfile.mkdtemp(prefix="axi-startup-cost-")
        _STATE.update({
            "HOME": root,
            "AXI_STATE_DIR": os.path.join(root, ".axi"),
            "AXIOM_DISABLE_UPDATE_NUDGE": "1",
        })
        for extra in ({}, {"AXI_FULL_PARSER": "1"}):
            _probe(["axi", "--help"], {**_STATE, **extra})
    return _STATE


def _run(argv: list[str], env_extra: dict[str, str] | None = None):
    """Run the CLI in a fresh interpreter, in its own warm state, and report what it loaded."""
    return _probe(argv, {**_isolated_state(), **(env_extra or {})})


def _probe(argv: list[str], env_extra: dict[str, str]):
    import os

    env = {**os.environ, "AXI_NO_SOURCE_WARNING": "1", "AXIOM_DIAGNOSES_QUIET": "1"}
    env.pop("_ARGCOMPLETE", None)
    env.pop("AXI_FULL_PARSER", None)
    env.update(env_extra)
    done = subprocess.run(
        [sys.executable, "-c", _PROBE.format(argv=argv, heavy=list(HEAVY))],
        capture_output=True, text=True, env=env, timeout=180,
    )
    heavy: list[str] = []
    modules = 0
    for line in done.stderr.splitlines():
        if line.startswith("HEAVY:"):
            heavy = [p for p in line[len("HEAVY:"):].split(",") if p]
        elif line.startswith("MODULES:"):
            modules = int(line[len("MODULES:"):])
        elif line.startswith("PROBE-ERROR:"):
            pytest.fail(f"the CLI raised while running {argv}: {line}")
    return done, heavy, modules


def test_help_does_not_import_the_serving_stack():
    _done, heavy, modules = _run(["axi", "--help"])
    assert heavy == [], (
        f"`axi --help` imported {', '.join(heavy)}. Printing a list of verbs must "
        f"not load a web framework or an ORM; something imported an extension's "
        f"package to read its parser."
    )


def test_help_loads_far_less_than_the_eager_parser_did():
    """A relative budget, not an absolute one: how many modules a CLI loads
    depends on which extras are installed, so a fixed ceiling would flap from
    machine to machine. What must hold everywhere is that building parsers for
    verbs nobody asked for costs a large fraction of startup, and that we no
    longer pay it. `AXI_FULL_PARSER` forces the old behaviour, so the two are
    measured in one environment."""
    _d1, _h1, lazy = _run(["axi", "--help"])
    _d2, _h2, eager = _run(["axi", "--help"], {"AXI_FULL_PARSER": "1"})
    assert lazy < eager * 0.8, (
        f"help loaded {lazy} modules against {eager} with every parser built; "
        f"the lazy path has stopped saving anything, so something imports the "
        f"extension packages again"
    )


def test_a_named_verb_still_gets_its_real_parser():
    """Laziness must not cost a reader the help they asked for: `axi ext
    --help` still lists the verbs the ext parser declares."""
    done, _heavy, _modules = _run(["axi", "ext", "--help"])
    out = done.stdout + done.stderr
    for verb in ("lint", "init", "templates"):
        assert verb in out, f"`axi ext --help` no longer lists {verb!r}:\n{out[:600]}"


def test_completion_is_what_asks_for_the_whole_tree():
    """Completion is the one caller that needs every subparser, and it asks by
    setting `_ARGCOMPLETE` — which `tests/cli/test_autocomplete.py` exercises
    end to end. Here we only check that the switch is read, so the two tests
    cannot drift apart silently."""
    from axiom.axiom_cli import _parser_wanted_for

    import os

    assert _parser_wanted_for("ext") is False or sys.argv[1:2] == ["ext"]
    os.environ["_ARGCOMPLETE"] = "1"
    try:
        assert _parser_wanted_for("anything-at-all") is True
    finally:
        del os.environ["_ARGCOMPLETE"]


def test_the_measurement_does_not_depend_on_what_ran_before():
    """CI main 28859d93 measured help at 552 modules lazy against 509 eager:
    the probes read the worker's real state dir, and whichever ran first on a
    cold one paid for the first-run work (startup index cache, update-state).
    Two lazy measurements in a row must agree, so the budget compares parsers
    and not the order the tests ran in."""
    _d1, _h1, first = _run(["axi", "--help"])
    _d2, _h2, second = _run(["axi", "--help"])
    assert first == second, (
        f"`axi --help` loaded {first} then {second} modules: the probe is "
        f"measuring first-run state, not the parser"
    )
