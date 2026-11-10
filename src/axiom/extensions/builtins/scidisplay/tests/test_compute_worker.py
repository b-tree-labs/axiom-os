# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The compute kernel runs for real, in a subprocess, and refuses what it must (ADR-039, ADR-167).

These run the actual worker process: a test that mocks the subprocess cannot notice that the
module it launches does not exist, which is how the kernel shipped broken (issue #1184).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from axiom.extensions.builtins.scidisplay import compute as c
from axiom.extensions.builtins.scidisplay.compute import ComputationRequest, compute

WORKER = Path(c.__file__).with_name("compute_worker.py")


def _run(expr: str, mode: str = "symbolic", **kw):
    return compute(
        ComputationRequest(
            latex=expr, mode=mode, timeout_seconds=kw.pop("timeout_seconds", 20), **kw
        )
    )


# ------------------------------------------------------------------ it runs


def test_the_worker_module_exists_and_is_launched_by_path():
    assert WORKER.is_file(), "the compute worker is missing (issue #1184)"


def test_symbolic_numeric_and_arbitrary_modes_compute():
    sym = _run("x**2 - 1 + (x-1)*(x+1)", "symbolic")
    assert sym.success, sym.error_message
    assert sym.value_repr.replace(" ", "") == "2*x**2-2"
    num = _run("2**10 + 3", "numeric")
    assert num.success and float(num.value_repr) == 1027.0
    arb = _run("sqrt(2)", "arbitrary", precision=30)
    assert arb.success and arb.value_repr.startswith("1.41421356237309504880168872421")


def test_the_receipt_is_deterministic_and_carries_the_ast_trail():
    a, b = _run("sin(pi/6)", "symbolic"), _run("sin(pi/6)", "symbolic")
    assert a.success and a.value_repr == "1/2"
    assert a.receipt_id == b.receipt_id and a.ast_trail


def test_a_wrong_expression_is_a_typed_failure_not_an_exception():
    r = _run("2 +* 3")
    assert not r.success and r.error_class in {"ParseError", "ValidationError"}


# ------------------------------------------------------------------ it refuses

HOSTILE = [
    "__import__('os').system('touch /tmp/axiom-pwned')",
    "().__class__.__bases__[0].__subclasses__()",
    "open('/etc/passwd').read()",
    "exec('import os')",
    "eval('1+1')",
    "(lambda: 1)()",
    "[x for x in range(3)]",
    "x.__class__",
    "globals()",
    "breakpoint()",
    "compile('1','','eval')",
    "getattr(x, 'real')",
    "print(1)",
]


@pytest.mark.parametrize("expr", HOSTILE)
def test_hostile_input_is_rejected_before_anything_runs(expr, tmp_path):
    marker = Path("/tmp/axiom-pwned")
    marker.unlink(missing_ok=True)
    r = _run(expr)
    assert not r.success
    assert r.error_class == "ValidationError", (expr, r.error_class, r.error_message)
    assert not marker.exists()


def test_a_name_that_shadows_a_builtin_is_not_a_symbol():
    r = _run("open + 1")
    assert not r.success and r.error_class == "ValidationError"


def test_oversized_input_is_refused():
    assert _run("x+" * 5000 + "1").error_class == "ValidationError"
    assert (
        _run("9" * 500).error_class == "ValidationError"
    )  # an integer too large to be honest input


def test_a_power_tower_is_refused_before_it_can_exhaust_memory():
    r = _run("9**9**9**9")
    assert not r.success and r.error_class == "ValidationError"


def test_a_runaway_computation_is_killed_at_the_budget_and_leaves_no_process():
    started = time.monotonic()
    r = _run("integrate(exp(-x**2)*sin(x)**x*cos(x**x), x)", "symbolic", timeout_seconds=2)
    assert not r.success
    assert time.monotonic() - started < 8
    assert (
        r.error_class in {"TimeoutExpired", "ResourceLimit", "ValidationError", "ParseError"}
        or "limit" in r.error_message.lower()
    )


# ------------------------------------------------------------------ the boundary itself


def test_the_worker_is_launched_isolated_with_a_scrubbed_environment(monkeypatch):
    monkeypatch.setenv("AXIOM_SECRET_PROBE", "hunter2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    env = c._worker_env()
    assert "AXIOM_SECRET_PROBE" not in env and "OPENAI_API_KEY" not in env
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONHASHSEED"}
    argv = c._worker_argv()
    assert argv[0] == sys.executable and "-I" in argv and str(WORKER) in argv


def test_ipc_is_json_not_pickle():
    src = Path(c.__file__).read_text()
    local = src.split("_WORKER_PATH = ")[1].split("# Cross-NODE compute via SSH dispatch")[0]
    assert "pickle" not in local, (
        "the parent must never unpickle output from a process that evaluates user math"
    )
    assert "json" in local


def test_garbage_from_the_worker_is_a_typed_failure(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    stub.write_text("import sys; sys.stdout.write('not json at all')")
    monkeypatch.setattr(c, "_worker_argv", lambda: [sys.executable, str(stub)])
    r = _run("1+1")
    assert not r.success and r.error_class == "BadWorkerOutput"


def test_runaway_output_is_capped(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    stub.write_text("import sys; sys.stdout.write('x' * 5_000_000)")
    monkeypatch.setattr(c, "_worker_argv", lambda: [sys.executable, str(stub)])
    r = _run("1+1")
    assert not r.success and r.error_class == "BadWorkerOutput"


def test_a_worker_that_hangs_is_killed_with_its_children(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    stub.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(c, "_worker_argv", lambda: [sys.executable, str(stub)])
    started = time.monotonic()
    r = _run("1+1", timeout_seconds=1)
    assert not r.success and r.error_class == "TimeoutExpired"
    assert time.monotonic() - started < 6
    out = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
    assert "time.sleep(60)" not in out, "the worker's child outlived its timeout"


# ------------------------------------------------------------------ verify (the declared tool)


def test_verify_is_the_declared_tool_and_returns_provenance_and_a_signature_note():
    r = c.verify("x**2 - 1", compare_to="(x-1)*(x+1)")
    assert r["success"] and r["equal"] is True
    assert r["receipt_id"].startswith("axiom://compute/") and r["ast_trail"]
    assert "signed" in r and isinstance(r["signed"], bool)
    assert r["signed"] or r["signing_note"]  # never silently unsigned: say why


def test_verify_distinguishes_unequal_expressions():
    r = c.verify("x**2", compare_to="x**3")
    assert r["success"] and r["equal"] is False


def test_verify_of_a_failure_reports_it():
    r = c.verify("__import__('os')")
    assert not r["success"] and r["error_class"] == "ValidationError"


# ------------------------------------------------------------------ LaTeX through the isolated worker


def _worker_sees_the_latex_backend() -> bool:
    """The worker runs `python -I`, which ignores PYTHONPATH: it sees what is installed, not what a
    developer put on a path. So this answers what a real install would."""
    probe = subprocess.run([sys.executable, "-I", "-c", "import antlr4"], capture_output=True)
    return probe.returncode == 0


def test_latex_input_is_computed_when_the_backend_is_installed_and_reported_when_it_is_not():
    r = _run(r"\frac{k-1}{k} + \frac{1}{k}", "symbolic")
    if _worker_sees_the_latex_backend():
        assert r.success, r.error_message
        assert r.value_repr == "1" and r.extra["parser"] == "latex/antlr"
    else:
        if os.environ.get("CI"):
            pytest.fail("the math extra is not installed on CI (pip install 'axiom-os-lm[math]')")
        assert not r.success and r.error_class == "ParserUnavailable"
        assert "axiom-os-lm[math]" in r.error_message  # the failure names the fix


def test_the_math_extra_is_declared_and_pinned():
    import tomllib

    pyproject = tomllib.loads((Path(__file__).resolve().parents[6] / "pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]
    assert "antlr4-python3-runtime==4.11.*" in extras["math"], (
        "the runtime must be pinned to SymPy's generated-parser series"
    )
    assert any(d.startswith("sympy") for d in extras["math"])
    assert any("[math]" in d for d in extras["dev"]), (
        "CI and contributors install dev; the corpus must run, not skip"
    )
    assert any("math" in d for d in extras["runtime"])


def test_a_worker_that_lingers_after_its_output_keeps_its_result(tmp_path, monkeypatch):
    """The worker closes its output just before it exits. The parent used to kill it in that gap,
    discarding a result it had already read and reporting SIGTERM as a worker failure (#1422)."""
    slow = tmp_path / "slow_worker.py"
    slow.write_text(
        "import json, os, sys, time\n"
        "sys.stdin.read()\n"
        'sys.stdout.write(json.dumps({"success": False, "error_class": "ValidationError",'
        ' "error_message": "refused"}) + "\\n")\n'
        "sys.stdout.flush(); os.close(1); os.close(2); time.sleep(1.0)\n"
    )
    monkeypatch.setattr(c, "_worker_argv", lambda: [sys.executable, "-I", str(slow)])
    r = compute(ComputationRequest(latex="x", mode="symbolic", timeout_seconds=10))
    assert (r.success, r.error_class) == (False, "ValidationError"), r.error_message
