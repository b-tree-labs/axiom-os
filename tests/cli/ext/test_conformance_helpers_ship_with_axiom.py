# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The conformance test helpers ship inside Axiom (#1793, decided by the owner 2026-11-12), so
``ext doctor`` on a fresh scaffold passes with nothing extra to install, and its advice says so."""

from __future__ import annotations

import builtins
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.acceptance("LB-F4-A1")
def test_the_wheel_carries_the_helper_package_beside_axiom():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "packages/axiom-tests/src/axiom_tests" in packages and "src/axiom" in packages
    assert (ROOT / "packages/axiom-tests/src/axiom_tests/__init__.py").is_file()
    # No new dependency in the base install.
    assert not any(d.startswith(("pytest", "hypothesis")) for d in data["project"]["dependencies"])


_NO_PYTEST = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name == "pytest" or name.startswith(("pytest.", "_pytest")):
            raise ImportError("pytest is not installed")
sys.meta_path.insert(0, Block())
import axiom, axiom_tests
assert axiom_tests.validate_manifest
print("ok")
"""


@pytest.mark.acceptance("LB-F4-A1")
def test_axiom_and_the_helpers_import_without_pytest():
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT / 'packages/axiom-tests/src'}"}
    run = subprocess.run([sys.executable, "-c", _NO_PYTEST], capture_output=True, text=True,
                         env=env, timeout=120)  # fmt: skip
    assert run.returncode == 0 and run.stdout.strip() == "ok", run.stderr[-2000:]


@pytest.mark.acceptance("LB-F4-A1")
def test_the_doctor_advice_no_longer_names_a_separate_package(monkeypatch):
    from axiom.cli.ext.commands import doctor

    real = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "axiom_tests":
            raise ImportError("no module named axiom_tests")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    got = doctor._check_axiom_tests_importable()
    assert not got.ok
    assert "pip install axiom-tests" not in got.remediation
    assert "axiom-os-lm" in got.remediation and "ship inside" in got.remediation
