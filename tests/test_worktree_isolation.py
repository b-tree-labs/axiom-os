# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A test must exercise the checkout it lives in, including in a subprocess.

The workspace shares one virtualenv across every worktree, so ``pip install -e``
anchors ``axiom`` to whichever checkout ran it. ``pythonpath`` in
``pyproject.toml`` fixes that for the in-process session. It does nothing for a
child process, which inherits ``os.environ`` and resolves through
site-packages.

The consequence was invisible and had two halves, both bad. A subprocess test
passed or failed on another worktree's code, so it could be green while the
code it shipped with was broken. And a session editing the anchor worktree
red-lit every other worktree's pre-push gate, which is how this was found: eight
notifications tests failed here for an ``AttributeError`` in a file this
worktree does not contain.

The fix is in the root ``conftest.py``: ``pytest_configure`` puts the same
source roots into ``PYTHONPATH``. These tests are what keep it true.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_root_conftest():
    """The repo-root ``conftest.py``, by path.

    Named ``conftest`` like a dozen other files in this repo, so it is loaded
    from its location rather than looked up by module name.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "axiom_root_conftest", REPO_ROOT / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _child_module_path(module: str, env: dict[str, str] | None = None) -> Path:
    """Where a child process resolves ``module`` from."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; print({module}.__file__)"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip()).resolve()


class TestASubprocessRunsThisWorktree:
    @pytest.mark.parametrize("module", ["axiom", "axiom_tests"])
    def test_the_child_imports_the_checkout_the_test_lives_in(self, module):
        resolved = _child_module_path(module)
        assert resolved.is_relative_to(REPO_ROOT), (
            f"a subprocess resolved {module} to {resolved}, outside this worktree "
            f"({REPO_ROOT}). Every subprocess test would be exercising that "
            "checkout instead of this one."
        )

    def test_pythonpath_is_actually_exported(self):
        """The mechanism, not just the outcome.

        Asserted separately because the outcome above would also hold by
        accident if this worktree happened to own the editable install, and
        then the guard would pass on the one machine where it cannot fail.
        """
        assert str(REPO_ROOT / "src") in os.environ.get("PYTHONPATH", "").split(os.pathsep)


class TestTheGuardCanFail:
    """Prove the check is capable of failing before trusting that it passed."""

    def test_stripping_pythonpath_resolves_somewhere_else(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        resolved = _child_module_path("axiom", env=env)

        if resolved.is_relative_to(REPO_ROOT):
            pytest.skip(
                "this worktree owns the editable install, so the negative "
                "control cannot demonstrate anything here. It still runs, and "
                "still fails, in any worktree that does not."
            )
        assert not resolved.is_relative_to(REPO_ROOT), (
            "without PYTHONPATH the child resolved into this worktree anyway, "
            "which means the fixture under test is not what is doing the work"
        )


class TestTheRootsCannotDrift:
    def test_conftest_and_pyproject_name_the_same_source_roots(self):
        """Two lists of the same thing drift. This is what stops them.

        Read from the toml rather than trusting the constant, so adding a
        third root to ``pythonpath`` and forgetting ``conftest`` fails here
        instead of silently leaving subprocesses one root short.

        The root conftest is loaded by PATH, not by ``import conftest``. The
        repo has many files named ``conftest.py`` and the bare import resolves
        to whichever one sys.path reaches first, which depends on what else was
        collected: this test passed alone and failed in the full suite until it
        stopped asking by name.
        """
        conftest = _load_root_conftest()

        declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        from_toml = declared["tool"]["pytest"]["ini_options"]["pythonpath"]

        expected = {(REPO_ROOT / entry).resolve() for entry in from_toml}
        actual = {Path(p).resolve() for p in conftest._SOURCE_ROOTS}

        assert actual == expected, (
            "conftest._SOURCE_ROOTS and pyproject's pythonpath disagree; a "
            "subprocess would import a different set of roots than the test "
            "session does"
        )
