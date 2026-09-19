# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

# Root conftest — makes shared fixtures available to ALL test directories,
# including colocated extension tests in src/axiom/extensions/builtins/.
#
# Fixtures are defined in tests/conftest.py and re-exported here so that
# pytest discovers them regardless of which testpath a test lives under.

import os
import sys
from pathlib import Path

from tests.conftest import *  # noqa: F401,F403,E402

_ROOT = Path(__file__).resolve().parent

#: The same roots ``[tool.pytest.ini_options] pythonpath`` names. Kept in step
#: with it by ``tests/test_worktree_isolation.py``, which reads the toml rather
#: than trusting this list.
_SOURCE_ROOTS = (_ROOT / "src", _ROOT / "packages" / "axiom-tests" / "src")


def pytest_configure(config):
    """Make child processes import THIS worktree, not whichever one owns the install.

    ``pythonpath`` in ``pyproject.toml`` puts these roots on ``sys.path`` for
    the test session, so in-process tests correctly exercise the checkout they
    live in. A subprocess inherits ``os.environ`` and nothing else, so it
    resolves ``axiom`` through site-packages — which points at whichever
    worktree last ran ``pip install -e``. Every one of the 100-plus test files
    that spawns ``sys.executable`` was therefore testing that checkout instead
    of its own.

    That is a check that cannot fail, and worse, one that can fail for reasons
    with nothing to do with the code under test: a session editing the anchor
    worktree red-lights every other worktree's pre-push gate, which is how this
    was found.

    Setting ``PYTHONPATH`` here rather than in each test fixes all of them at
    once and needs no per-test discipline. It is set in ``os.environ`` so a
    child gets it whether or not the spawning test remembered to pass ``env``.
    """
    roots = [str(p) for p in _SOURCE_ROOTS if p.is_dir()]
    if not roots:  # pragma: no cover — a source tree that is not there
        return
    existing = os.environ.get("PYTHONPATH", "")
    parts = roots + [p for p in existing.split(os.pathsep) if p and p not in roots]
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)

    # The in-process path matters too when pytest is invoked in a way that does
    # not apply the ini setting (``python -m pytest`` from another directory,
    # some IDE runners). Idempotent, and ordered so the worktree wins.
    for root in reversed(roots):
        if root not in sys.path:
            sys.path.insert(0, root)
