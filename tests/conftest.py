# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared test fixtures for neut signal and publisher test suites."""

import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    """Skip @benchmark tests unless -m benchmark is explicitly requested."""
    if config.option.markexpr and "benchmark" in config.option.markexpr:
        return  # user asked for benchmarks — let them run
    skip_benchmark = pytest.mark.skip(reason="benchmark — run with: pytest -m benchmark")
    for item in items:
        if item.get_closest_marker("benchmark"):
            item.add_marker(skip_benchmark)


def pytest_configure(config):
    """Wire pytest-xdist worker id → ``AXIOM_TEST_SCHEMA_SUFFIX`` so every
    extension's ``session_for`` resolves to a worker-scoped schema under
    parallel test runs.

    Fixes the silent failure mode that trained the team toward
    ``--no-verify``: vault + notifications persisted-state fixtures share
    a single ``vault`` schema and ``TRUNCATE`` in teardown, so workers
    were nuking each other's rows. With this hook, worker gw0 sees schema
    ``vault_gw0``, gw1 sees ``vault_gw1``, etc. — production unaffected
    (env var unset outside the test runtime).

    Sets the var on every process — main + xdist workers — because xdist
    subprocesses inherit env at spawn time. For the notifications
    cross-process test, the test's ``_run`` helper copies ``os.environ``
    into the spawned axiom CLI subprocess so the suffix carries through
    to the second process too. Critical: see
    ``feedback_prepush_xdist_false_positives``.
    """
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "")
    if worker_id:
        # gw0, gw1, ... → _gw0, _gw1
        os.environ.setdefault("AXIOM_TEST_SCHEMA_SUFFIX", f"_{worker_id}")

# Ensure repo root is on path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Git-config pollution canary (defense-in-depth against the 2026-05-04 +
# 2026-05-07 tester-pollution incidents).
# ---------------------------------------------------------------------------
#
# Per-test fixtures already use git_isolated_env() + assert_test_tmp_path()
# (see axiom.extensions.builtins.hygiene._git_isolation). This session-level
# canary catches any test that slips past those guards: it snapshots the
# active worktree's user.name + user.email at session start, asserts they're
# unchanged at session end, and (best-effort) restores them if a test wrote
# pollution. Failure is loud — the user finds out before the developer's
# next commit is mis-attributed.
#
# Per `feedback_test_fixture_isolation_required.md`: "Test fixtures using
# git MUST set GIT_CONFIG_GLOBAL=/dev/null + assert path is under tmp;
# otherwise pollute shared worktree config." This canary surfaces violations.


def _read_git_config_user(repo_root: Path) -> tuple[str | None, str | None]:
    """Read user.name + user.email from the worktree-local git config.

    Returns (None, None) on any failure (no git, no .git, etc.) so the
    canary degrades gracefully outside a real worktree.
    """
    try:
        name = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--local", "--get", "user.name"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        email = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--local", "--get", "user.email"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:  # noqa: BLE001 — defensive; never raise from canary
        return None, None
    n = name.stdout.strip() if name.returncode == 0 else None
    e = email.stdout.strip() if email.returncode == 0 else None
    return n, e


def _read_head_sha(repo_root: Path) -> str | None:
    """Resolve HEAD to a SHA. None on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _read_branch(repo_root: Path) -> str | None:
    """Resolve the current branch name. None when detached or on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _write_git_config_user(repo_root: Path, name: str | None, email: str | None) -> None:
    """Restore user.name + user.email; unset if the snapshot was None."""
    try:
        if name is None:
            subprocess.run(
                ["git", "-C", str(repo_root), "config", "--local", "--unset", "user.name"],
                capture_output=True, timeout=5, check=False,
            )
        else:
            subprocess.run(
                ["git", "-C", str(repo_root), "config", "--local", "user.name", name],
                capture_output=True, timeout=5, check=False,
            )
        if email is None:
            subprocess.run(
                ["git", "-C", str(repo_root), "config", "--local", "--unset", "user.email"],
                capture_output=True, timeout=5, check=False,
            )
        else:
            subprocess.run(
                ["git", "-C", str(repo_root), "config", "--local", "user.email", email],
                capture_output=True, timeout=5, check=False,
            )
    except Exception:  # noqa: BLE001 — defensive
        pass


def _commit_authors_between(
    repo_root: Path, base_sha: str, head_sha: str
) -> list[tuple[str | None, str | None]] | None:
    """Return (author_name, author_email) for each commit in base..head.

    None on any failure (can't enumerate → can't prove the range is pure
    pollution → must NOT auto-heal).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--format=%an%x00%ae",
             f"{base_sha}..{head_sha}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    if result.returncode != 0:
        return None
    authors: list[tuple[str | None, str | None]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, _, email = line.partition("\x00")
        authors.append((name or None, email or None))
    return authors


def _worktree_has_no_tracked_changes(repo_root: Path) -> bool:
    """True iff there are NO uncommitted changes to TRACKED files.

    The auto-heal reset --hard is only safe when this holds: reset --hard
    would destroy uncommitted edits to tracked files (it once destroyed
    this very guard's own edits). Untracked files are intentionally
    ignored (``-uno``) because reset --hard leaves them untouched — so
    they pose no data-loss risk and shouldn't block healing (e.g.
    ``__pycache__`` or scratch files would otherwise veto every heal).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "-uno"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    return result.returncode == 0 and result.stdout.strip() == ""


def _reset_hard(repo_root: Path, sha: str) -> bool:
    """Hard-reset the worktree to ``sha``. True on success."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "reset", "--hard", sha],
            capture_output=True, timeout=10, check=False,
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    return result.returncode == 0


@pytest.fixture(scope="session", autouse=True)
def _guard_worktree_git_config():
    """Snapshot + assert + restore the active worktree's user.name / user.email.

    Any test that pollutes the active worktree's git config — typically by
    running a subprocess with an unisolated env, hitting `git config
    --local`, and writing under our `.git/` instead of the test's
    tmp_path — gets caught here. The assertion message names the
    snapshot vs. observed values so the offender is identifiable from
    the test session output.

    2026-05-18 follow-on: if the snapshot ITSELF is polluted (a prior
    session left ``Test`` / ``test@example.com`` and so on in the local
    config), restoring it would persist the pollution forever — the
    conftest becomes the persistence vector. Detect via
    ``tests._pollution_guard.is_polluted_snapshot`` and **unset**
    instead of restoring; the global config takes over.
    """
    from tests._pollution_guard import all_commits_are_pollution, is_polluted_snapshot

    snapshot_name, snapshot_email = _read_git_config_user(REPO_ROOT)
    snapshot_branch = _read_branch(REPO_ROOT)
    snapshot_head = _read_head_sha(REPO_ROOT)
    snapshot_was_polluted = is_polluted_snapshot(snapshot_name, snapshot_email)

    yield

    end_name, end_email = _read_git_config_user(REPO_ROOT)
    end_branch = _read_branch(REPO_ROOT)
    end_head = _read_head_sha(REPO_ROOT)

    config_polluted = (snapshot_name, snapshot_email) != (end_name, end_email)
    head_moved = (
        snapshot_branch is not None
        and end_branch == snapshot_branch
        and snapshot_head != end_head
    )

    if config_polluted:
        # Restore — UNLESS the snapshot itself was polluted, in which
        # case unset so the global identity takes over.
        if snapshot_was_polluted:
            _write_git_config_user(REPO_ROOT, None, None)
        else:
            _write_git_config_user(REPO_ROOT, snapshot_name, snapshot_email)
    elif snapshot_was_polluted:
        # No drift during the session, but the snapshot itself is
        # carrying pollution from a prior session. Clear it.
        _write_git_config_user(REPO_ROOT, None, None)
    # HEAD movement is auto-healed — but ONLY when (a) every commit in the
    # moved range was authored by a stray test-fixture identity, AND (b) the
    # worktree is otherwise clean. `git reset --hard` restores the polluted
    # files but also discards ANY uncommitted work, so we refuse to run it
    # when the developer has in-flight edits (surface loudly instead). This
    # is what the recurring "seed" corruption needed — and the clean-worktree
    # guard is why it can't eat a developer's uncommitted changes.
    head_auto_healed = False
    head_heal_blocked_dirty = False
    if head_moved:
        authors = _commit_authors_between(REPO_ROOT, snapshot_head, end_head)
        if authors is not None and all_commits_are_pollution(authors):
            if _worktree_has_no_tracked_changes(REPO_ROOT):
                head_auto_healed = _reset_hard(REPO_ROOT, snapshot_head)
            else:
                head_heal_blocked_dirty = True

    if config_polluted or head_moved or snapshot_was_polluted:
        msgs = [f"WORKTREE POLLUTION DETECTED at {REPO_ROOT}:"]
        if config_polluted:
            restored_to = "unset (snapshot was pollution)" if snapshot_was_polluted else "snapshot"
            msgs.append(
                f"  config user.name :  snapshot={snapshot_name!r} → end={end_name!r}\n"
                f"  config user.email:  snapshot={snapshot_email!r} → end={end_email!r}\n"
                f"  → {restored_to}."
            )
        elif snapshot_was_polluted:
            msgs.append(
                f"  snapshot itself was polluted: user.name={snapshot_name!r} user.email={snapshot_email!r}\n"
                "  → unset; global identity takes over. Prior session's cleanup didn't land."
            )
        if head_moved:
            if head_auto_healed:
                heal_note = (
                    "  → AUTO-HEALED: every moved commit was a stray test-fixture "
                    "author and the worktree was clean; reset --hard back to "
                    "snapshot. Fix the polluting fixture (env=git_isolated_env() + "
                    "tmp_path) so this stops recurring."
                )
            elif head_heal_blocked_dirty:
                heal_note = (
                    "  → NOT auto-healed: the worktree has uncommitted changes, so "
                    "reset --hard would destroy them. Manually `git reset --hard "
                    f"{snapshot_head}` once you've saved any real work."
                )
            else:
                heal_note = (
                    "  → NOT auto-restored (range contains a non-fixture author, or "
                    "git enumeration failed). Inspect via `git reflog`; reset only "
                    "if you can confirm those commits are stray test fixtures."
                )
            msgs.append(
                f"  branch {snapshot_branch!r} HEAD moved during test session:\n"
                f"    snapshot={snapshot_head!r}\n"
                f"    end     ={end_head!r}\n"
                + heal_note
            )
        msgs.append(
            "Some test wrote into the active worktree instead of a tmp_path-scoped repo. "
            "See axiom.extensions.builtins.hygiene._git_isolation for canonical "
            "isolation helpers; the offending test should pass env=git_isolated_env() "
            "and cwd=<tmp-resolved path> to every subprocess.run that invokes git."
        )
        full_msg = "\n".join(msgs)
        if os.environ.get("AXIOM_TEST_GIT_POLLUTION_FAIL"):
            raise AssertionError(full_msg)
        warnings.warn(full_msg, UserWarning, stacklevel=2)


@pytest.fixture
def repo_root():
    """Path to the repository root."""
    return REPO_ROOT


@pytest.fixture
def tmp_config(tmp_path):
    """Create a temporary config directory with people.md and initiatives.md."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    people_md = config_dir / "people.md"
    people_md.write_text(
        "| Name | Aliases | Usernames | Role | Initiative(s) |\n"
        "|------|---------|-----------|------|---------------|\n"
        "| Alice Smith | — | gitlab:asmith, github:alice-gh | Lead | Project Alpha |\n"
        "| Bob Jones | Bobby | gitlab:bjones, github:bob-gh | Engineer | Project Beta, Project Alpha |\n"
        "| Charlie Brown | — | gitlab:cbrown | Student | Project Gamma |\n"
    )

    initiatives_md = config_dir / "initiatives.md"
    initiatives_md.write_text(
        "| ID | Name | Status | Owners | GitLab Repos | Weight | Pause Reason |\n"
        "|----|------|--------|--------|-------------|--------|-------------|\n"
        "| 1 | Project Alpha | Active | Smith, Jones | alpha_project/* | 5 | |\n"
        "| 2 | Project Beta | Active | Jones | beta_project/* | 3 | |\n"
        "| 3 | Project Gamma | Stale | Brown | gamma_project/* | 2 | |\n"
    )

    return config_dir


@pytest.fixture
def sample_gitlab_export(tmp_path):
    """Create a minimal gitlab export JSON for testing."""
    export = {
        "exported_at": "2026-02-17T00:00:00+00:00",
        "gitlab_url": "https://gitlab.example.com",
        "group": "test-group",
        "time_window_days": 90,
        "projects": [
            {
                "info": {
                    "id": 1,
                    "name": "Alpha Project",
                    "path": "alpha-project",
                    "path_with_namespace": "test-group/alpha-project",
                    "description": "Test project",
                    "default_branch": "main",
                    "last_activity_at": "2026-02-16T10:00:00Z",
                    "web_url": "https://gitlab.example.com/test-group/alpha-project",
                },
                "activity": {
                    "commits": [
                        {
                            "sha": "abc123",
                            "author_name": "Alice Smith",
                            "author_email": "alice@example.com",
                            "created_at": "2026-02-15T10:00:00+00:00",
                            "title": "Add new feature X",
                            "message": "Add new feature X\n\nImplements the X subsystem with full test coverage.\nCloses #42.",
                        },
                        {
                            "sha": "def456",
                            "author_name": "Bob Jones",
                            "author_email": "bob@example.com",
                            "created_at": "2026-02-14T10:00:00+00:00",
                            "title": "Fix bug in module Y",
                            "message": "Fix bug in module Y\n\nThe Y module was crashing on empty input.\nAdded null check and regression test.",
                        },
                    ],
                    "contributor_summary": {"Alice Smith": 1, "Bob Jones": 1},
                    "open_issues": [
                        {
                            "iid": 1,
                            "title": "Implement feature Z",
                            "labels": ["enhancement"],
                            "assignees": ["asmith"],
                            "author": "bjones",
                            "created_at": "2026-02-10T10:00:00Z",
                            "updated_at": "2026-02-15T10:00:00Z",
                            "milestone": None,
                            "description": "Need to implement Z",
                        }
                    ],
                    "recently_closed_issues": [],
                    "issue_comments": [
                        {
                            "issue_iid": 1,
                            "issue_title": "Implement feature Z",
                            "note_id": 501,
                            "author": "asmith",
                            "body": "I started working on this. The approach looks solid.",
                            "created_at": "2026-02-12T14:00:00Z",
                        },
                        {
                            "issue_iid": 1,
                            "issue_title": "Implement feature Z",
                            "note_id": 502,
                            "author": "bjones",
                            "body": "Reviewed the draft PR. Needs more tests for edge cases.",
                            "created_at": "2026-02-13T09:30:00Z",
                        },
                    ],
                    "open_mrs": [],
                    "recently_merged_mrs": [],
                    "milestones": [],
                    "labels": ["enhancement", "bug"],
                    "active_branches": [],
                },
            }
        ],
        "summary": {
            "total_commits_by_author": {"Alice Smith": 1, "Bob Jones": 1},
            "stale_repos": ["test-group/stale-project"],
            "project_stats": [],
            "newly_discovered_projects": [],
            "total_projects": 1,
            "total_commits": 2,
            "total_open_issues": 1,
            "total_open_mrs": 0,
            "total_issue_comments": 2,
        },
    }

    path = tmp_path / "gitlab_export_2026-02-17.json"
    path.write_text(json.dumps(export, indent=2))
    return path


@pytest.fixture
def sample_gitlab_export_previous(tmp_path):
    """Create a previous gitlab export for diff testing."""
    export = {
        "exported_at": "2026-02-10T00:00:00+00:00",
        "gitlab_url": "https://gitlab.example.com",
        "group": "test-group",
        "time_window_days": 90,
        "projects": [
            {
                "info": {
                    "id": 1,
                    "name": "Alpha Project",
                    "path": "alpha-project",
                    "path_with_namespace": "test-group/alpha-project",
                    "description": "Test project",
                    "default_branch": "main",
                    "last_activity_at": "2026-02-09T10:00:00Z",
                    "web_url": "https://gitlab.example.com/test-group/alpha-project",
                },
                "activity": {
                    "commits": [
                        {
                            "sha": "old123",
                            "author_name": "Alice Smith",
                            "author_email": "alice@example.com",
                            "created_at": "2026-02-05T10:00:00+00:00",
                            "title": "Initial commit",
                            "message": "Initial commit",
                        },
                    ],
                    "contributor_summary": {"Alice Smith": 1},
                    "open_issues": [],
                    "recently_closed_issues": [],
                    "issue_comments": [
                        {
                            "issue_iid": 1,
                            "issue_title": "Implement feature Z",
                            "note_id": 500,
                            "author": "asmith",
                            "body": "Created this issue to track feature Z.",
                            "created_at": "2026-02-04T10:00:00Z",
                        },
                    ],
                    "open_mrs": [],
                    "recently_merged_mrs": [],
                    "milestones": [],
                    "labels": [],
                    "active_branches": [],
                },
            }
        ],
        "summary": {
            "total_commits_by_author": {"Alice Smith": 1},
            "stale_repos": [],
            "project_stats": [],
            "newly_discovered_projects": [],
            "total_projects": 1,
            "total_commits": 1,
            "total_open_issues": 0,
            "total_open_mrs": 0,
        },
    }

    path = tmp_path / "gitlab_export_2026-02-10.json"
    path.write_text(json.dumps(export, indent=2))
    return path


@pytest.fixture
def publisher_config(tmp_path):
    """Create a minimal publisher config for testing."""
    from axiom.extensions.builtins.publishing.config import (
        GitPolicy,
        ProviderConfig,
        PublisherConfig,
    )

    return PublisherConfig(
        git=GitPolicy(require_clean=False, require_pushed=False),
        generation=ProviderConfig(provider="pandoc-docx"),
        storage=ProviderConfig(
            provider="local",
            settings={"base_dir": str(tmp_path / "published")},
        ),
        notification=ProviderConfig(provider="terminal"),
        repo_root=REPO_ROOT,
    )
