# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A release gate that cannot pass is a gate that always gets bypassed.

Three defects, all hit while cutting a real release.

The test gate ran pytest with a hardcoded 300-second timeout. The suite takes
about 460, so it always aborted with "Could not run tests", and the only way
to cut any release was `--skip-tests` — which turns the gate off for everyone,
permanently, by habit.

A timeout was also reported identically to a genuine failure. "Could not run
tests: TimeoutExpired" and "the tests failed" call for opposite responses:
one means raise the budget, the other means stop.

And `cut` pushed the version bump straight to `main`, which is protected and
requires status checks, so the push was always declined — and it exited 0
anyway, reporting a successful release that had published nothing. The tag
stayed local and PyPI never saw it. I only noticed by checking `git ls-remote`
rather than believing the exit code.
"""

from __future__ import annotations

import subprocess

import pytest

from axiom.extensions.builtins.release._legacy_release_cli import (
    DEFAULT_TEST_TIMEOUT_S,
)


def test_the_default_budget_fits_the_suite_it_has_to_run():
    """460s measured; a 300s budget cannot ever pass."""
    assert DEFAULT_TEST_TIMEOUT_S >= 900


def test_the_budget_is_configurable(monkeypatch):
    """A suite grows. The cliff should move without a code change."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    monkeypatch.setenv("AXI_RELEASE_TEST_TIMEOUT", "1234")

    assert mod.test_timeout() == 1234


def test_a_bad_budget_falls_back_rather_than_crashing(monkeypatch):
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    monkeypatch.setenv("AXI_RELEASE_TEST_TIMEOUT", "not-a-number")

    assert mod.test_timeout() == DEFAULT_TEST_TIMEOUT_S


def test_a_timeout_is_reported_as_a_budget_problem(monkeypatch):
    """Not as "could not run tests", which reads as a broken environment."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=300)

    monkeypatch.setattr(mod.subprocess, "run", _timeout)
    message = mod.describe_test_failure(_timeout)

    assert "budget" in message.lower() or "timed out" in message.lower()
    assert "AXI_RELEASE_TEST_TIMEOUT" in message


def test_a_real_failure_is_not_described_as_a_timeout():
    """Negative control: the two must not collapse back into one message."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    def _fail():
        raise RuntimeError("pytest exploded")

    message = mod.describe_test_failure(_fail)

    assert "AXI_RELEASE_TEST_TIMEOUT" not in message


# --- the push ---------------------------------------------------------------
#
# `_git` uses a 30-second timeout. A push runs the pre-push hook, which runs
# the suite — minutes, not seconds. So the push raises TimeoutExpired, which is
# not the RuntimeError the caller catches, and the release ends with the tag
# sitting local and nothing published. The version bump also goes to whatever
# branch is checked out: on a protected `main` that is refused outright, which
# is why releases actually go through a release/vX.Y.Z PR.


def test_a_push_is_given_longer_than_a_hook_takes():
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    assert mod.PUSH_TIMEOUT_S >= 600


def test_a_timed_out_push_is_a_push_failure_not_a_crash(monkeypatch, tmp_path):
    """It must land in the handler that prints the manual push command."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    mgr = mod.ReleaseManager.__new__(mod.ReleaseManager)
    mgr.repo_root = tmp_path
    mgr.dry_run = False

    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git push", timeout=30)

    monkeypatch.setattr(mod.subprocess, "run", _timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        mgr._git("push", "origin", "main")


def test_an_ordinary_git_failure_still_raises_runtime_error(monkeypatch, tmp_path):
    """Negative control: the timeout path must not swallow real failures."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    mgr = mod.ReleaseManager.__new__(mod.ReleaseManager)
    mgr.repo_root = tmp_path
    mgr.dry_run = False

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "protected branch hook declined"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _Result())

    with pytest.raises(RuntimeError, match="protected branch"):
        mgr._git("push", "origin", "main")


# --- a release that says it shipped must have shipped -----------------------
#
# The same gap as a delivery receipt that reports success for an alert nobody
# can read. `push_release` ran two git commands and returned; nothing confirmed
# the tag reached origin. Today a release reported success with the tag sitting
# local and nothing published, and that was found by running `git ls-remote` by
# hand — the tool never looked.
#
# So it looks. A tag read back from the remote is cheap, and it is the
# difference between "the push command returned" and "the release exists".


def test_a_pushed_tag_is_verified_against_the_remote():
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    mgr = mod.ReleaseManager.__new__(mod.ReleaseManager)
    mgr.repo_root = "."
    mgr.dry_run = False
    seen = []

    def _git(*args, **kw):
        seen.append(args)
        if args[0] == "ls-remote":
            return "abc123\trefs/tags/v9.9.9"
        return ""

    mgr._git = _git
    result = mod.verify_tag_published(mgr, "9.9.9")

    assert result.verified is True
    assert any(a[0] == "ls-remote" for a in seen), "it must ask the remote"


def test_a_tag_missing_from_the_remote_is_not_verified():
    """The exact failure: the push returned, the tag is not there."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    mgr = mod.ReleaseManager.__new__(mod.ReleaseManager)
    mgr.repo_root = "."
    mgr.dry_run = False
    mgr._git = lambda *a, **k: ""          # remote reports no such tag

    result = mod.verify_tag_published(mgr, "9.9.9")

    assert result.verified is False
    assert "9.9.9" in result.detail


def test_an_unreachable_remote_is_unverified_not_failed():
    """"I could not check" is a different claim from "it did not happen"."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    mgr = mod.ReleaseManager.__new__(mod.ReleaseManager)
    mgr.repo_root = "."
    mgr.dry_run = False

    def _boom(*a, **k):
        raise RuntimeError("network down")

    mgr._git = _boom
    result = mod.verify_tag_published(mgr, "9.9.9")

    assert result.verified is False
    assert result.checked is False


def test_a_protected_branch_rejection_names_the_release_pr_path():
    """Pushing a version bump straight at a protected main is always refused.
    The message should say what actually works rather than leave someone to
    rediscover it."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    advice = mod.explain_push_failure("remote rejected: protected branch hook declined", "1.2.3")

    assert "release/v1.2.3" in advice
    assert "pull request" in advice.lower()


def test_an_ordinary_push_failure_is_not_given_branch_protection_advice():
    """Negative control: wrong advice is worse than none."""
    from axiom.extensions.builtins.release import _legacy_release_cli as mod

    advice = mod.explain_push_failure("Could not resolve host: github.com", "1.2.3")

    assert "release/v1.2.3" not in advice
