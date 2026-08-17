# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for RIVET's local-main sync (``local_sync``).

RIVET keeps every local default branch current with its remote. The sync
is **non-destructive**: it fast-forwards only a clean, non-diverged default
branch; anything dirty, ahead, or diverged is *surfaced*, never touched
(that honors the RIVET-makes-green / TIDY-removes-brown boundary, ADR-046).

These tests use real git repos under ``tmp_path`` with file-path remotes,
so ``git fetch`` works offline and there is no git internals to mock. That
mirrors the house pattern in the hygiene/drift and ``infra.git`` tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.release import local_sync
from axiom.infra.git import git_available, run_git

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


# ---------------------------------------------------------------------------
# Real-git fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return run_git(repo, *args)


def _commit(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)


def _make_upstream_and_clone(tmp_path: Path, repo_name: str = "proj") -> tuple[Path, Path]:
    """Create a bare 'upstream' repo plus a working clone of it.

    Returns ``(upstream_bare, local_clone)``. The clone's ``origin`` is the
    bare repo on the local filesystem, so fetch/pull need no network.
    """
    seed = tmp_path / f"{repo_name}-seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "Tester")
    _commit(seed, "README.md", "v1\n", "initial")

    bare = tmp_path / f"{repo_name}.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))

    local = tmp_path / repo_name
    _git(tmp_path, "clone", str(bare), str(local))
    _git(local, "config", "user.email", "t@example.com")
    _git(local, "config", "user.name", "Tester")
    return bare, local


def _advance_upstream(tmp_path: Path, bare: Path, n: int = 1) -> None:
    """Push ``n`` new commits to the bare upstream via a throwaway clone."""
    work = tmp_path / "advance-work"
    if work.exists():
        import shutil

        shutil.rmtree(work)
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "up@example.com")
    _git(work, "config", "user.name", "Upstream")
    for i in range(n):
        _commit(work, f"up{i}.txt", f"upstream {i}\n", f"upstream commit {i}")
    _git(work, "push", "origin", "main")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_finds_git_repos_skips_non_git(tmp_path, monkeypatch):
    _make_upstream_and_clone(tmp_path, "alpha")
    _make_upstream_and_clone(tmp_path, "beta")
    (tmp_path / "not-a-repo").mkdir()
    (tmp_path / "loose-file.txt").write_text("x", encoding="utf-8")

    monkeypatch.setenv("AXI_WORKSPACE_ROOT", str(tmp_path))
    repos = local_sync.discover_workspace_repos()
    names = {p.name for p in repos}

    assert "alpha" in names
    assert "beta" in names
    assert "not-a-repo" not in names  # plain dir, no .git
    # seed/bare/work scaffolding from the fixture must not be synced as
    # working clones: the bare repos end in .git, the seeds aren't origins
    # we manage. Discovery returns working clones only.
    assert all(not n.endswith(".git") for n in names)


def test_discover_explicit_root_arg_overrides_env(tmp_path, monkeypatch):
    _make_upstream_and_clone(tmp_path, "gamma")
    monkeypatch.delenv("AXI_WORKSPACE_ROOT", raising=False)
    repos = local_sync.discover_workspace_repos(root=tmp_path)
    assert any(p.name == "gamma" for p in repos)


# ---------------------------------------------------------------------------
# Provider labeling — host-agnostic (GitHub + GitLab + self-hosted)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,provider,host",
    [
        ("git@github.com:example-org/CoreForge.git", "github", "github.com"),
        ("https://github.com/b-tree-labs/axiom.git", "github", "github.com"),
        (
            "https://oauth2:tok@gitlab.example.org/example-org/x/y.git",
            "gitlab",
            "gitlab.example.org",
        ),
        ("git@gitlab.com:group/proj.git", "gitlab", "gitlab.com"),
    ],
)
def test_provider_label_maps_host(url, provider, host):
    assert local_sync.provider_label(url) == (provider, host)


def test_provider_label_unknown_host_degrades_to_git():
    prov, host = local_sync.provider_label("file:///tmp/whatever.git")
    assert prov == "git"


# ---------------------------------------------------------------------------
# sync_repo — the core state machine
# ---------------------------------------------------------------------------


def test_up_to_date_is_noop(tmp_path):
    _bare, local = _make_upstream_and_clone(tmp_path)
    res = local_sync.sync_repo(local)
    assert res.action == "up_to_date"
    assert res.behind == 0 and res.ahead == 0
    assert res.default_branch == "main"


def test_behind_clean_main_is_fast_forwarded(tmp_path):
    bare, local = _make_upstream_and_clone(tmp_path)
    before = local_sync._git_head(local)
    _advance_upstream(tmp_path, bare, n=2)

    res = local_sync.sync_repo(local, apply=True)

    assert res.action == "fast_forwarded"
    assert res.behind == 2
    # Local main actually advanced.
    assert local_sync._git_head(local) != before
    # And is now even with the remote.
    follow = local_sync.sync_repo(local, apply=True)
    assert follow.action == "up_to_date"


def test_behind_with_apply_false_does_not_move_branch(tmp_path):
    bare, local = _make_upstream_and_clone(tmp_path)
    before = local_sync._git_head(local)
    _advance_upstream(tmp_path, bare, n=1)

    res = local_sync.sync_repo(local, apply=False)

    assert res.action == "behind"  # would fast-forward, but didn't
    assert res.behind == 1
    assert local_sync._git_head(local) == before  # untouched


def test_behind_but_dirty_main_is_surfaced_not_touched(tmp_path):
    bare, local = _make_upstream_and_clone(tmp_path)
    before = local_sync._git_head(local)
    _advance_upstream(tmp_path, bare, n=1)
    # Dirty the working tree on the checked-out default branch.
    (local / "README.md").write_text("local edit\n", encoding="utf-8")

    res = local_sync.sync_repo(local, apply=True)

    assert res.action == "behind_dirty"
    assert res.dirty is True
    assert local_sync._git_head(local) == before  # never fast-forwarded over local edits


def test_diverged_is_surfaced_not_touched(tmp_path):
    bare, local = _make_upstream_and_clone(tmp_path)
    _advance_upstream(tmp_path, bare, n=1)  # remote moves ahead
    _commit(local, "local.txt", "local work\n", "local divergent commit")  # local moves too
    before = local_sync._git_head(local)

    res = local_sync.sync_repo(local, apply=True)

    assert res.action == "diverged"
    assert res.ahead >= 1 and res.behind >= 1
    assert local_sync._git_head(local) == before  # never merged/rebased


def test_ahead_only_reports_ahead(tmp_path):
    _bare, local = _make_upstream_and_clone(tmp_path)
    _commit(local, "local.txt", "unpushed\n", "unpushed local commit")

    res = local_sync.sync_repo(local, apply=True)

    assert res.action == "ahead"
    assert res.ahead >= 1 and res.behind == 0


def test_default_branch_fast_forwards_even_when_not_checked_out(tmp_path):
    bare, local = _make_upstream_and_clone(tmp_path)
    main_before = local_sync._rev_parse(local, "main")
    # Check out a feature branch; main is now not the working branch.
    _git(local, "checkout", "-b", "feature/x")
    _commit(local, "feat.txt", "feature\n", "feature commit")
    _advance_upstream(tmp_path, bare, n=1)

    res = local_sync.sync_repo(local, apply=True)

    assert res.action == "fast_forwarded"
    # The local `main` ref advanced even though `feature/x` is checked out...
    assert local_sync._rev_parse(local, "main") != main_before
    # ...and the checked-out feature branch was left alone.
    assert local_sync._git_branch(local) == "feature/x"


def test_no_remote_is_reported(tmp_path):
    seed = tmp_path / "solo"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "Tester")
    _commit(seed, "f.txt", "x\n", "init")

    res = local_sync.sync_repo(seed)
    assert res.action == "no_remote"


# ---------------------------------------------------------------------------
# sync_workspace — aggregation across the workspace
# ---------------------------------------------------------------------------


def test_sync_workspace_aggregates_repos(tmp_path, monkeypatch):
    bare_a, _local_a = _make_upstream_and_clone(tmp_path, "one")
    _bare_b, _local_b = _make_upstream_and_clone(tmp_path, "two")
    _advance_upstream(tmp_path, bare_a, n=1)  # "one" is behind

    monkeypatch.setenv("AXI_WORKSPACE_ROOT", str(tmp_path))
    results = local_sync.sync_workspace(apply=True)
    by_name = {r.repo: r for r in results}

    assert by_name["one"].action == "fast_forwarded"
    assert by_name["two"].action == "up_to_date"


def test_results_are_json_serializable(tmp_path):
    _bare, local = _make_upstream_and_clone(tmp_path)
    res = local_sync.sync_repo(local)
    d = res.to_dict()
    import json

    json.loads(json.dumps(d))  # must not raise
    assert d["action"] == "up_to_date"
    assert d["repo"] == "proj"


# ---------------------------------------------------------------------------
# CLI: `axi release sync`
# ---------------------------------------------------------------------------


def test_sync_verb_in_parser():
    from axiom.extensions.builtins.release.agent_cli import build_parser

    args = build_parser().parse_args(["sync", "--plan", "--root", "/tmp/x"])
    assert args.action == "sync"
    assert args.dry_run is True
    assert args.root == "/tmp/x"


def test_cli_sync_subprocess_smoke(tmp_path):
    """End-to-end: run the real `axi rivet` entry point as a subprocess.

    Unit/in-process tests miss entry-point wiring (handler registration,
    arg parsing, import-time errors). This drives the actual module main
    against a tmp workspace holding one behind-by-one clone and asserts the
    fast-forward shows up in `--format json`.
    """
    import json
    import os
    import subprocess
    import sys

    import axiom

    bare, _local = _make_upstream_and_clone(tmp_path, "smoke")
    _advance_upstream(tmp_path, bare, n=1)  # "smoke" is now behind by one

    env = dict(os.environ)
    env["AXI_WORKSPACE_ROOT"] = str(tmp_path)
    # Exercise the *source under test*: point the subprocess at the same
    # `axiom` this test imported, not whatever may be installed in
    # site-packages (which can lag the working tree on a non-editable install).
    src_root = str(Path(axiom.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.release.agent_cli",
         "sync", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    # Parse the JSON array from stdout and find the "smoke" repo's outcome.
    payload = json.loads(result.stdout)
    by_name = {r["repo"]: r for r in payload}
    assert by_name["smoke"]["action"] == "fast_forwarded"
    assert by_name["smoke"]["behind"] == 1
