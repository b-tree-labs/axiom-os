# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An absent optional dependency must skip, and must stay distinguishable.

The v0.52.0 deploy failed here, and the interesting part is that the skip
branch already existed:

    # db/cli.py
    try:
        ok = run_migrations("upgrade", revision)
    except ModuleNotFoundError as exc:
        print(f"   signals:  skipped — {missing!r} is not installed")

It could never fire. `run_migrations` ends in ``except Exception`` and returns
``False``, so the exception died one frame down and arrived at the caller as a
bool. "this extension's dependency is absent" and "this migration is broken"
became the same value, and a bool cannot carry the difference.

The deploy then did the right thing for the wrong reason — it stopped, and
refused to restart services onto a database behind the code. But it stopped on
a node that was fine.

So this file checks the property from BOTH ends:

  * the call sites treat an absent extra as a skip, not a failure
  * nothing between them flattens ``ModuleNotFoundError`` into a bool again

The second is the one that matters. Fixing the three call sites closes this
instance; a static check closes the class, because the next `except Exception`
added above a `raise` is invisible at every call site downstream.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


def _swallows_module_not_found(fn) -> bool:
    """Does *fn* catch ModuleNotFoundError broadly without re-raising?

    An ``except Exception`` that does not re-raise catches ModuleNotFoundError,
    because it is an ImportError, which is an Exception. A preceding, narrower
    ``except ModuleNotFoundError``/``except ImportError`` handler takes
    precedence, so one that re-raises makes the broad handler unreachable for
    this type — which is exactly the fix.
    """
    tree = ast.parse(inspect.getsource(fn).lstrip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            name = getattr(handler.type, "id", None)
            if name in {"ModuleNotFoundError", "ImportError"}:
                # narrower handler wins; re-raising leaves the broad one unreachable
                if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
                    break
            if name in {"Exception", "BaseException"} or handler.type is None:
                if not any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
                    return True
    return False


def test_run_migrations_lets_an_absent_dependency_through():
    """The frame where the distinction used to die."""
    from axiom.extensions.builtins.signals.migrations import run_migrations

    assert not _swallows_module_not_found(run_migrations), (
        "run_migrations catches ModuleNotFoundError in a broad handler and "
        "returns a bool — the caller's skip branch becomes unreachable, which "
        "is how the v0.52.0 deploy failed on a node that was fine"
    )


def test_the_check_can_actually_fail():
    """Negative control: this guard has to be able to see the old shape."""

    def swallower():
        try:
            pass
        except Exception:  # noqa: BLE001
            return False

    def reraiser():
        try:
            pass
        except ModuleNotFoundError:
            raise
        except Exception:  # noqa: BLE001
            return False

    assert _swallows_module_not_found(swallower)
    assert not _swallows_module_not_found(reraiser)


@pytest.mark.parametrize(
    "module,needle",
    [
        ("axiom.extensions.builtins.db.cli", "skipped"),
        ("axiom.extensions.builtins.update.cli", "skipped"),
        ("axiom.extensions.builtins.signals.cli", "Skipped"),
    ],
)
def test_every_caller_has_somewhere_for_a_skip_to_land(module: str, needle: str):
    """Each of the three has an explicit ModuleNotFoundError branch that skips."""
    source = Path(__import__(module, fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert "except ModuleNotFoundError" in source, f"{module} has no skip branch"
    assert needle in source, f"{module}'s skip branch does not say it skipped"


def test_a_skip_names_the_missing_module_not_just_that_something_failed():
    """'not installed' with no name sends the operator looking in the wrong place."""
    from axiom.infra.db import provision_extension

    source = inspect.getsource(provision_extension)
    assert "getattr(exc, \"name\", None)" in source, (
        "the skip message must name the module — ModuleNotFoundError.name is "
        "the only part of it an operator can act on"
    )


# ---------------------------------------------------------------------------
# The deploy step itself, not just the frame that broke.
#
# `axi db migrate upgrade head` is what the ut-triga-site deploy runs before it
# will restart services. On 2026-09-17 it exited 1 on a node that was fine, and
# the deploy correctly refused to restart — so the node kept running old code
# while every other step reported success.
#
# The unit-level guards above prove the exception survives its frame. This
# proves the thing the deploy actually depends on: the command exits 0.
# ---------------------------------------------------------------------------


def test_the_deploy_step_exits_zero_when_an_extras_dependency_is_absent():
    """The literal command in .github/workflows/deploy.yml."""
    import contextlib
    import io
    from unittest import mock

    from axiom.extensions.builtins.db import cli as dbcli

    def absent(*a, **k):
        raise ModuleNotFoundError("No module named 'pgvector'", name="pgvector")

    class _Provisioned:
        def __init__(self, name):
            self.extension = name
            self.ok = True

        @property
        def summary(self):
            return f"{self.extension}: up to date"

    buf = io.StringIO()
    with (
        mock.patch("axiom.extensions.builtins.signals.migrations.run_migrations", absent),
        mock.patch(
            "axiom.extensions.builtins.signals.migrations.ensure_pgvector_extension",
            lambda *a, **k: None,
        ),
        mock.patch(
            "axiom.infra.db.extensions_with_migrations",
            return_value=[("signals", "/x"), ("schedule", "/y"), ("webapp", "/z")],
        ),
        mock.patch("axiom.infra.db.provision_extension", side_effect=_Provisioned),
        contextlib.redirect_stdout(buf),
    ):
        rc = dbcli.main(["migrate", "upgrade", "head"])

    out = buf.getvalue()
    assert rc == 0, (
        f"exit {rc}: the deploy step fails and the node is left running old code "
        f"while every other step reports success.\n{out}"
    )
    assert "skipped" in out, "a skip must say it skipped, not go quiet"
    assert "pgvector" in out, "name the module — it is the only actionable part"
    # The extensions that CAN migrate still must, or a skip becomes a no-op deploy.
    assert "schedule" in out and "webapp" in out


def test_a_genuinely_broken_migration_still_fails_the_deploy():
    """The skip must not become a blanket amnesty. Negative control for the fix."""
    import contextlib
    import io
    from unittest import mock

    from axiom.extensions.builtins.db import cli as dbcli

    class _Failed:
        def __init__(self, name):
            self.extension = name
            self.ok = False

        @property
        def summary(self):
            return f"{self.extension}: ❌ upgrade failed"

    buf = io.StringIO()
    with (
        mock.patch(
            "axiom.extensions.builtins.signals.migrations.run_migrations",
            lambda *a, **k: True,
        ),
        mock.patch(
            "axiom.extensions.builtins.signals.migrations.ensure_pgvector_extension",
            lambda *a, **k: None,
        ),
        mock.patch(
            "axiom.extensions.builtins.signals.migrations.check_migrations",
            lambda *a, **k: {"current": "0001"},
        ),
        mock.patch(
            "axiom.infra.db.extensions_with_migrations", return_value=[("schedule", "/y")]
        ),
        mock.patch("axiom.infra.db.provision_extension", side_effect=_Failed),
        contextlib.redirect_stdout(buf),
    ):
        rc = dbcli.main(["migrate", "upgrade", "head"])

    assert rc == 1, "a real migration failure must still stop the deploy"
