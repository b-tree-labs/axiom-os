# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A subprocess must import the checkout under test, not the venv's anchor.

The workspace venv installs axiom editable from ONE worktree. In-process tests
are fine — pytest puts the rootdir's `src` first — but a subprocess inherits no
such thing, so it imports the anchor's code. The two disagree silently, and the
subprocess side is exactly where the CLI convention mandates coverage.

How this was found is the point: two faithful mutants of the release heartbeat
BOTH SURVIVED a subprocess smoke, because the smoke was running against another
branch entirely. Nothing was red. A test that survives every mutant is not a
test, and nothing in the suite could have told us.

So this file is the negative control for
`_pin_subprocess_to_this_checkout` in `tests/conftest.py`: delete that fixture
and these fail. Without it the pinning is an assumption, not a guarantee.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parent.parent.parent / "src"

_PROBE = "import axiom, os; print(os.path.dirname(os.path.dirname(axiom.__file__)))"
_PYTHONPATH_PROBE = "import os; print(os.environ.get('PYTHONPATH', ''))"


def _probe(env: dict[str, str] | None = None, code: str = _PROBE) -> str:
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(Path(__file__).resolve().parent),
    )
    assert r.returncode == 0, f"probe failed: {r.stderr}"
    return r.stdout.strip()


class TestASubprocessImportsThisCheckout:
    def test_an_inherited_environment_resolves_to_this_src(self) -> None:
        """The 9-of-13 case: a subprocess that passes no `env=` at all."""
        assert Path(_probe()) == REPO_SRC, (
            "a subprocess inherited the environment and imported a DIFFERENT "
            "checkout's axiom — the venv's editable anchor. Every subprocess "
            "smoke in this repo is then testing the wrong branch, silently."
        )

    def test_splatting_os_environ_resolves_to_this_src(self) -> None:
        """The common idiom `env={**os.environ, ...}` must stay pinned."""
        assert Path(_probe(env={**os.environ, "SOME_UNRELATED": "1"})) == REPO_SRC

    def test_pythonpath_is_actually_set_and_leads_with_this_src(self) -> None:
        """Assert the mechanism, not only its effect.

        If the venv's anchor ever coincides with this checkout, the two tests
        above would pass with the fixture removed — they would be checks that
        cannot fail. This one names the quantity that is free to vary.
        """
        value = os.environ.get("PYTHONPATH", "")
        assert value, "PYTHONPATH is unset — the pinning fixture did not run"
        assert value.split(os.pathsep)[0] == str(REPO_SRC), (
            f"PYTHONPATH leads with {value.split(os.pathsep)[0]!r}, "
            f"not this checkout's src {str(REPO_SRC)!r}"
        )

    def test_an_env_built_from_scratch_is_NOT_covered(self) -> None:
        """Document the limit honestly, so nobody trusts it too far.

        A test that constructs its environment from nothing bypasses the
        fixture and must pass PYTHONPATH itself.

        This asserts the MECHANISM's reach — a bare env carries no PYTHONPATH —
        rather than which checkout such a subprocess resolves to. The first
        version asserted the latter and was WRONG IN CI: there the package is
        installed FROM the checkout, so a bare env resolves to this `src`
        legitimately and the test went red while nothing was broken. That
        assertion had encoded a local-dev accident — the venv anchored to a
        different worktree — as a universal invariant. The fixture's reach is
        the same everywhere; the resolved path is not.
        """
        inherited = _probe(code=_PYTHONPATH_PROBE)
        assert str(REPO_SRC) in inherited, "precondition: the fixture is active"

        bare = _probe(
            env={"PATH": os.environ.get("PATH", "")}, code=_PYTHONPATH_PROBE
        )
        assert bare.strip() == "", (
            f"a bare env carried PYTHONPATH={bare!r} — the fixture reaches "
            "further than this file documents, so the limit recorded here is "
            "wrong."
        )
