# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared git-isolation helpers for hygiene + cross-cutting tests.

The 2026-05-04 tester-pollution incident proved that *every* test which
runs `git init` against a path resolved from `tmp_path` is a candidate
contamination vector. If the test process inherits a ~/.gitconfig that
sets `core.worktree` or any path-coupling option, OR if a parent git
repo's local config is reachable from the chosen path, `git config`
writes inside the test can bleed into the developer's real worktree
config — silently committing to the wrong branch.

This module is the *single* canonical source of:

  - ``git_isolated_env()`` — environment that pins git to ONLY this
    invocation's local config (``GIT_CONFIG_GLOBAL=/dev/null`` +
    ``GIT_CONFIG_SYSTEM=/dev/null``).
  - ``assert_test_tmp_path()`` — refuses to operate on any path that
    doesn't unambiguously resolve under a tmp prefix.

Every test helper that wraps ``subprocess.run(["git", ...])`` MUST use
both. Drift will detect any commit authored by ``Test`` / ``tester`` /
``T`` etc. and the prevention machinery will recommend quarantine —
but the cheaper fix is to never let the pollution happen in the first
place.

If you add a new git-running test fixture, import from here. Don't
re-roll the helpers locally — the more copies, the more places future
fixtures can drift from the contract.
"""

from __future__ import annotations

from pathlib import Path

# macOS resolves /tmp/* to /private/tmp/*; pytest's tmp_path lives under
# /private/var/folders/* on macOS and /tmp/pytest-of-* on Linux. All four
# prefixes are below — anything else is rejected.
SAFE_TMP_PREFIXES: tuple[str, ...] = (
    "/tmp/",
    "/private/tmp/",
    "/var/folders/",
    "/private/var/folders/",
)


def git_isolated_env() -> dict[str, str]:
    """Return an env dict that pins git to this invocation's local config only.

    Sets both ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_SYSTEM`` to
    ``/dev/null`` so any ``git config`` write cannot bleed into:

      - the user's ``~/.gitconfig``,
      - the system gitconfig,
      - or — most dangerously — into a *parent* git repo's local config
        when the test path resolves inside a real worktree.

    Also **strips every ``GIT_*`` environment variable from the parent
    process** before re-adding the explicit isolation keys. Git hooks
    (notably pre-push) and worktree contexts propagate ``GIT_DIR``,
    ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``, ``GIT_OBJECT_DIRECTORY``,
    ``GIT_COMMON_DIR``, and friends into subprocesses. Inheriting them
    silently overrides any ``-C <path>`` or ``cwd=`` the caller passes —
    the test ends up operating on the host repo instead of its own
    ``tmp_path``-scoped one. That is the 2026-05-11 hook-context
    pollution vector: standalone test runs passed, the pre-push hook
    run failed because ``git push`` set ``GIT_DIR`` before the hook
    spawned pytest.

    Always pass this env (or a superset) to every ``subprocess.run``
    that invokes git from a test fixture.
    """
    from axiom.infra.git import safe_git_env

    env = safe_git_env()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return env


def assert_test_tmp_path(path: Path | str) -> None:
    """Refuse to touch any path that doesn't resolve under a tmp prefix.

    Belt-and-suspenders alongside :func:`git_isolated_env` — even if a
    caller's path resolution is wrong, this aborts before any git write.

    Raises:
        AssertionError: when ``path`` resolves outside SAFE_TMP_PREFIXES.
    """
    resolved = str(Path(path).resolve())
    if not any(resolved.startswith(p) for p in SAFE_TMP_PREFIXES):
        raise AssertionError(
            f"git fixture refuses to operate on {resolved!r}: "
            f"path must resolve under one of {SAFE_TMP_PREFIXES}. "
            "This guard exists because the 2026-05-04 tester-pollution "
            "incident polluted real worktree configs; never disable "
            "without a fix to the underlying contamination vector."
        )
