# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A creator on a released install can test, validate and publish an extension.

`axiom-tests` (the standard AEOS conformance suite) is development
infrastructure and is not on PyPI. On 2026-10-06 a creator walk from a clean
`pip install` found every step after `ext init` blocked on it:

- `ext test` refused to run at all and told the reader to
  `pip install axiom-tests`, which does not resolve;
- the scaffold's `test_standard.py` imports it, so plain pytest stopped at
  collection and the creator's own tests never ran;
- `ext validate` and `ext publish` failed `standard_tests` on the import.

The standard tests are re-checked in CI when an extension is reviewed, the
same posture `ext lint` already takes for the schema check (AEOS023). So when
the suite is absent it is reported NOT RUN, visibly, and the creator's own
tests still run. Absent is never reported as passed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.cli.ext.commands import test_verb, validate
from axiom.cli.ext.templates import registry as template_registry

ALL_TEMPLATE_IDS = sorted(t.id for t in template_registry())


def _scaffold(template_id: str, dest: Path, name: str = "probe_ext") -> Path:
    template = next(t for t in template_registry() if t.id == template_id)
    ext_dir = dest / name
    template.create(ext_dir, name=name, owner="an-institution", license="Apache-2.0",
                    description=f"{name} scaffolded in a test")
    return ext_dir


def _without_axiom_tests(tmp_path: Path) -> dict[str, str]:
    """An environment where `import axiom_tests` fails, as on a released install."""
    shadow = tmp_path / "shadow"
    (shadow / "axiom_tests").mkdir(parents=True)
    (shadow / "axiom_tests" / "__init__.py").write_text(
        "raise ModuleNotFoundError(\"No module named 'axiom_tests'\")\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(shadow), env.get("PYTHONPATH", "")])
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_a_scaffold_runs_its_own_tests_without_the_standard_suite(template_id, tmp_path):
    ext_dir = _scaffold(template_id, tmp_path)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rs"],
        cwd=ext_dir, capture_output=True, text=True, timeout=180,
        env=_without_axiom_tests(tmp_path),
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode in (0, 5), out  # 5: nothing left to run in the compound scaffold
    assert "error" not in out.lower().split("short test summary")[0][-200:], out
    assert "skipped" in out.lower(), "the standard suite must show as skipped, not vanish"


def test_ext_test_runs_the_creators_tests_when_the_standard_suite_is_absent(
    tmp_path, monkeypatch, capsys
):
    ext_dir = _scaffold("conform", tmp_path)
    monkeypatch.setattr(test_verb, "missing_test_deps", lambda: ["axiom_tests"])
    code = test_verb.run_pytest(ext_dir, [])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "NOT RUN" in out and "axiom-tests" in out
    assert "pip install axiom-tests" not in out, "that install does not resolve from PyPI"


def test_ext_test_still_refuses_without_pytest(tmp_path, monkeypatch, capsys):
    ext_dir = _scaffold("conform", tmp_path)
    monkeypatch.setattr(test_verb, "missing_test_deps", lambda: ["pytest"])
    assert test_verb.run_pytest(ext_dir, []) == 1
    assert "pip install pytest" in capsys.readouterr().out


def test_validate_reports_the_standard_suite_not_run_rather_than_failed(tmp_path, monkeypatch):
    ext_dir = _scaffold("conform", tmp_path)
    monkeypatch.setattr(validate, "_standard_suite_available", lambda: False)
    result = validate.run_standard_tests(ext_dir)
    assert result.ok and result.not_run
    assert "NOT RUN" in result.detail


def test_validate_still_fails_when_the_suite_is_present_and_red(tmp_path, monkeypatch):
    """The other half: present-and-failing must still block."""
    ext_dir = _scaffold("conform", tmp_path)
    monkeypatch.setattr(validate, "_standard_suite_available", lambda: True)
    std = ext_dir / "tests" / "unit_tests" / "test_standard.py"
    std.write_text("def test_red():\n    assert False\n", encoding="utf-8")
    result = validate.run_standard_tests(ext_dir)
    assert not result.ok and not result.not_run


@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_no_scaffolded_file_keeps_an_unreplaced_placeholder(template_id, tmp_path):
    """`__CLI__ data conform_try` reached a creator's README verbatim."""
    ext_dir = _scaffold(template_id, tmp_path)
    for f in ext_dir.rglob("*"):
        if f.is_file() and f.suffix in {".md", ".py", ".toml"}:
            text = f.read_text(encoding="utf-8")
            left = re.findall(r"__(?:CLI|NAME|KIND|MAIN|MAINMOD|DRYRUN|RECIPE)__", text)
            assert not left, f"{f.relative_to(ext_dir)} still says {left}"


def test_the_conform_dry_run_names_the_verb_the_cli_has(tmp_path):
    readme = (_scaffold("conform", tmp_path) / "README.md").read_text(encoding="utf-8")
    assert "conform_try" not in readme


def test_the_scaffold_says_how_a_tool_reaches_an_assistant(tmp_path):
    """Declaring a tool under `provides` alone does not expose it, and the
    handler gets one dict. Both were learned by reading the aggregator."""
    manifest = (_scaffold("compound", tmp_path) / "axiom-extension.toml").read_text(encoding="utf-8")
    assert "[[extension.mcp.tool]]" in manifest
    assert "def func(args: dict)" in manifest
