# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""LOOP-S6: a release is cut only from a main commit whose smoke journeys passed.

A real git repository with a real origin. The verdict is the deploy's own smoke run, kept in
the journey store by commit (LOOP-S5); an ancestor's result serves when nothing shipped
changed since. No CI job runs the journeys per commit. The release lock refusing a second
release is the cut's own and is proven in ``test_release_lock.py``; the store's newest-run
rule in ``synthetic/tests``. Behind the ``release_smoke_gate`` flag (ADR-257 D4).
"""

from __future__ import annotations

import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import axiom
from axiom.extensions.builtins.release import _legacy_release_cli as cut
from axiom.extensions.builtins.release import release_lock as RL
from axiom.extensions.builtins.synthetic import store

pytestmark = pytest.mark.acceptance("LOOP-S6")

REPO = "example-org/app"


def git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class LockForge(RL.NullLockForge):
    def __init__(self) -> None:
        self.variable: str | None = None

    def get_variable(self, repo, name):
        return self.variable

    def set_variable(self, repo, name, value):
        self.variable = value

    def delete_variable(self, repo, name):
        held, self.variable = self.variable is not None, None
        return held


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-b", "main", str(work))
    git(work, "config", "user.email", "t@example.invalid")
    git(work, "config", "user.name", "T")
    git(work, "config", "commit.gpgsign", "false")
    git(work, "config", "tag.gpgsign", "false")
    (work / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1.0"\n')
    git(work, "add", "pyproject.toml")
    git(work, "commit", "-m", "first")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-u", "origin", "main")
    monkeypatch.setattr(axiom, "REPO_ROOT", work)
    monkeypatch.setattr(cut.ReleaseManager, "repo_slug", lambda self: REPO)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setattr(cut, "_lock_state_dir", lambda: state)
    forge = LockForge()
    monkeypatch.setattr(cut, "_lock_forge", lambda slug: forge)
    monkeypatch.setattr(cut, "_lock_recipients", lambda: ())
    monkeypatch.setenv("AXIOM_FLAG_RELEASE_SMOKE_GATE", "on")
    return work, state, forge


def _commit(work: Path, path: str, text: str) -> str:
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(work, "add", path)
    git(work, "commit", "-m", f"change {path}")
    git(work, "push", "origin", "main")
    return git(work, "rev-parse", "HEAD")


def _deployed(state: Path, sha: str, status: str = "pass", failing=()) -> None:
    store.record(state, commit=sha, source="deploy",
                 result={"status": status, "role": "serve", "failing": list(failing)})


def _tags(work: Path) -> list[str]:
    return git(work, "tag", "--list").split()


def test_a_commit_with_no_smoke_result_is_not_released(repo, capsys):
    work, _, _ = repo
    assert cut.main(["patch", "--skip-tests", "--no-push"]) == 1
    out = capsys.readouterr().out
    assert "smoke" in out and git(work, "rev-parse", "--short=10", "HEAD") in out
    assert _tags(work) == [] and "0.1.0" in (work / "pyproject.toml").read_text(), "nothing bumped, nothing tagged"


def test_a_commit_whose_deploy_smoke_passed_is_released_and_a_failed_one_is_not(repo, capsys):
    work, state, _ = repo
    sha = git(work, "rev-parse", "HEAD")
    _deployed(state, sha, "fail", ["serve-up: readiness answers ready"])
    assert cut.main(["patch", "--skip-tests", "--no-push"]) == 1
    assert "readiness answers ready" in capsys.readouterr().out
    _deployed(state, sha)
    assert cut.main(["patch", "--skip-tests", "--no-push"]) == 0
    assert _tags(work) == ["v0.1.1"]


def test_an_ancestor_s_result_serves_only_when_nothing_shipped_changed_since(repo, capsys):
    work, state, _ = repo
    _deployed(state, git(work, "rev-parse", "HEAD"))
    _commit(work, "docs/notes.md", "words\n")
    assert cut.main(["patch", "--dry-run"]) == 0
    assert "nothing shipped changed since" in capsys.readouterr().out
    _commit(work, "src/app/code.py", "x = 1\n")
    assert cut.main(["patch", "--skip-tests", "--no-push"]) == 1
    assert "shipped code changed since (src/app/code.py)" in capsys.readouterr().out


def test_a_commit_that_is_not_on_main_is_refused_even_with_a_passing_smoke(repo, capsys):
    work, state, _ = repo
    (work / "x.txt").write_text("local only")
    git(work, "add", "x.txt")
    git(work, "commit", "-m", "not pushed")
    _deployed(state, git(work, "rev-parse", "HEAD"))
    assert cut.main(["patch", "--skip-tests", "--no-push"]) == 1
    assert "not on origin/main" in capsys.readouterr().out
    assert _tags(work) == []


def test_dry_run_reports_the_verdict_and_changes_nothing(repo, capsys):
    work, _, _ = repo
    assert cut.main(["patch", "--dry-run"]) == 0
    assert "would refuse" in capsys.readouterr().out
    assert _tags(work) == []


def test_with_the_flag_off_the_cut_behaves_as_before(repo, monkeypatch):
    work, _, _ = repo
    monkeypatch.setenv("AXIOM_FLAG_RELEASE_SMOKE_GATE", "off")
    assert cut.main(["patch", "--skip-tests", "--no-push"]) == 0
    assert _tags(work) == ["v0.1.1"]


def test_run_smoke_runs_the_journeys_once_on_demand_and_records_them(repo, tmp_path):
    work, state, _ = repo

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "14")
            self.end_headers()
            self.wfile.write(b'{"ready":true}')

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (tmp_path / "ready.toml").write_text(
        '[journey]\nname = "ready"\nbase_url = "http://x"\n\n[[step]]\nname = "ready"\npath = "/readyz"\n'
        'expect = { status = 200 }\n')
    (tmp_path / "smoke.toml").write_text('[[role]]\nname = "serve"\njourneys = ["ready.toml"]\n')
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        assert cut.main(["patch", "--skip-tests", "--no-push", "--run-smoke", url,
                         "--smoke-manifest", str(tmp_path / "smoke.toml")]) == 0
    finally:
        srv.shutdown()
        srv.server_close()
    assert store.verdict(state, git(work, "rev-parse", "HEAD~1"))["source"] == "on-demand"
    assert _tags(work) == ["v0.1.1"]
