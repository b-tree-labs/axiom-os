# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The heartbeat runs anywhere, watches one list, and by default only watches.

A launchd job starts in ``/``. The repo guard tried ``git init`` there on every
tick, and the failed init killed the heartbeat before it wrote a line.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.release import agent_cli, ci_monitor, cli
from axiom.extensions.builtins.release.actions_spend import watched_repos
from axiom.extensions.builtins.release.lander.config import configured_repos
from axiom.extensions.builtins.release.pr_check_responder import handle_flip
from axiom.extensions.builtins.release.pr_check_watcher import StateFlip
from axiom.extensions.builtins.release.tests._watch import seen_pipelines
from axiom.extensions.builtins.release.watch_list import names, watch_list_path
from axiom.infra import git_setup

REL = "axiom.extensions.builtins.release"
LIST = """
[[repo]]
url = "https://github.com/example-org/app"
ref = "trunk"

[[repo]]
url = "https://github.com/example-org/lib"
"""


def write_list(state_dir: Path) -> None:
    path = watch_list_path(state_dir)
    path.parent.mkdir(parents=True)
    path.write_text(LIST, encoding="utf-8")


@pytest.fixture()
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.delenv("AXI_CI_REPOS", raising=False)
    monkeypatch.delenv("AXIOM_ACTIONS_REPOS", raising=False)
    return state


# -- the repo guard ----------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["heartbeat"], ["ci-health"], ["ci-sweep"], ["ci-lint"], ["ci-digest"], ["land"], ["pause"], ["list", "watched"]],
)
def test_a_verb_that_needs_no_repo_skips_the_guard(argv: list[str]):
    assert not cli._needs_repo(argv)


@pytest.mark.parametrize("argv", [[], ["cut"], ["changelog"], ["status"], ["patch"], ["--dry-run"], ["unknown"]])
def test_a_repo_verb_keeps_the_guard(argv: list[str]):
    assert cli._needs_repo(argv)


def test_heartbeat_in_a_non_repo_directory_reaches_the_verb(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ensure_repo_or_offer_init", lambda *a, **k: pytest.fail("the guard ran"))
    with patch(f"{REL}.cli.release_skills.bind_default"), patch(
        "axiom.infra.skill_dispatch.invoke_capability"
    ) as invoked:
        invoked.return_value.exit_code = 0
        assert cli.main(["heartbeat"]) == 0
    assert invoked.call_args.args[1] == "release.heartbeat"
    assert not (tmp_path / ".git").exists()


def test_a_repo_verb_in_a_non_repo_directory_creates_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", open("/dev/null"))
    assert cli.main(["status"]) == 1
    assert not (tmp_path / ".git").exists()


def test_assume_yes_never_runs_git_init(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(git_setup, "git_available", lambda: True)
    monkeypatch.setattr(git_setup, "is_inside_work_tree", lambda _p: False)
    monkeypatch.setattr(git_setup, "init_repo", lambda *_a, **_k: pytest.fail("must not init"))
    out: list[str] = []
    assert git_setup.ensure_repo_or_offer_init(tmp_path, assume_yes=True, output_fn=out.append) is False
    assert any("git init" in line for line in out)


def test_a_failed_init_is_a_refusal_not_a_crash(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(git_setup, "git_available", lambda: True)
    monkeypatch.setattr(git_setup, "is_inside_work_tree", lambda _p: False)

    def refuse(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["git", "init"])

    monkeypatch.setattr(git_setup, "init_repo", refuse)
    ok = git_setup.ensure_repo_or_offer_init(tmp_path, input_fn=lambda _q: "y", interactive=True, output_fn=lambda _l: None)
    assert ok is False


# -- one list ----------------------------------------------------------------


def test_the_list_gives_owner_and_name(state_dir: Path):
    write_list(state_dir)
    assert names(state_dir) == ("example-org/app", "example-org/lib")


def test_no_list_means_no_repos(state_dir: Path):
    assert names(state_dir) == ()


def test_pipelines_read_the_one_list_when_ci_repos_is_absent(state_dir: Path):
    write_list(state_dir)
    entries = ci_monitor.load_watched_repos()
    assert [ci_monitor._repo_ref_from_entry(e)[0].repo for e in entries] == ["app", "lib"]
    assert entries[0]["branch"] == "trunk"


def test_nothing_configured_names_no_repo_at_all(state_dir: Path):
    assert ci_monitor.load_watched_repos() == []


def test_ci_repos_still_wins_when_present(state_dir: Path):
    write_list(state_dir)
    (state_dir / "ci-repos.toml").write_text('[[repo]]\nurl = "https://github.com/example-org/other"\n', encoding="utf-8")
    assert ci_monitor.load_watched_repos() == [{"url": "https://github.com/example-org/other"}]


def test_spend_and_lander_read_the_one_list(state_dir: Path):
    write_list(state_dir)
    assert watched_repos(state_dir) == ("example-org/app", "example-org/lib")
    assert configured_repos(state_dir) == ("example-org/app", "example-org/lib")


def test_a_lander_list_still_wins_for_the_lander(state_dir: Path):
    write_list(state_dir)
    (state_dir / "agents" / "rivet" / "lander-repos").write_text("example-org/only\n", encoding="utf-8")
    assert configured_repos(state_dir) == ("example-org/only",)


# -- watch-only --------------------------------------------------------------


def _flip(to_state: str = "passing") -> StateFlip:
    return StateFlip(
        pr_number=7,
        title="t",
        url="https://forge.invalid/7",
        head_branch="b",
        from_state="failing",
        to_state=to_state,
        classification="code",
        failing_rows=(),
    )


class _Armed:
    """Everything that changes state, recording any call."""

    NAMES = {
        "local sync": f"{REL}.local_sync.sync_workspace",
        "lander": f"{REL}.lander.loop.heartbeat_propose",
        "post-merge sweep": f"{REL}.pr_check_auto_closer.sweep_stale",
        "recovery close": f"{REL}.pr_check_responder.auto_close_on_recovery",
        "narrative": f"{REL}.failure_narrative.narrative_for_failure",
    }

    def __enter__(self):
        self.mocks = {}
        self.patches = []
        for label, target in self.NAMES.items():
            p = patch(target)
            mock = p.start()
            mock.return_value = [] if label != "lander" else {"ran": True, "mode": "warn", "repos": []}
            if label == "narrative":
                mock.return_value = None
            self.mocks[label] = mock
            self.patches.append(p)
        for target, value in (
            (f"{REL}.pr_check_auto_closer.current_main_state", "passing"),
            (f"{REL}.pr_check_watcher.watch_user_prs", [_flip()]),
            (f"{REL}.cross_repo_pr_watch.load_watched_repos", []),
            (
                f"{REL}.ci_monitor.observe_pipelines",
                seen_pipelines(
                    [ci_monitor.PipelineStatus(repo="r", provider="github", ref="main", status="failed", failure_reason="odd")]
                ),
            ),
        ):
            p = patch(target, return_value=value)
            p.start()
            self.patches.append(p)
        return self

    def __exit__(self, *_exc):
        for p in self.patches:
            p.stop()

    def called(self) -> set[str]:
        return {label for label, mock in self.mocks.items() if mock.called}


def _setting(value):
    return patch(f"{REL}._legacy_rivet_cli.heartbeat_watch_only", return_value=value)


def test_by_default_the_heartbeat_changes_nothing(state_dir: Path):
    with _Armed() as armed:
        agent_cli.main(["heartbeat"])
    assert armed.called() == set()
    entry = json.loads((state_dir / "agents" / "rivet" / "heartbeat.jsonl").read_text().splitlines()[-1])
    assert entry["watch_only"] is True
    assert entry["lander"]["reason"] == "watch_only"


def test_it_still_watches(state_dir: Path):
    with _Armed():
        assert agent_cli.main(["heartbeat"]) == 2
    entry = json.loads((state_dir / "agents" / "rivet" / "heartbeat.jsonl").read_text().splitlines()[-1])
    assert entry["pipelines"][0]["status"] == "failed"


def test_the_setting_turns_the_actions_back_on(state_dir: Path):
    with _Armed() as armed, _setting(False):
        agent_cli.main(["heartbeat"])
    assert armed.called() == set(_Armed.NAMES)


def test_a_recovery_flip_closes_nothing_unless_told(tmp_path: Path):
    sink = type("S", (), {"send": lambda self, **k: True})()
    with patch(f"{REL}.pr_check_responder.auto_close_on_recovery") as close:
        handle_flip(_flip(), state_dir=tmp_path, sink=sink, auto_close=False, bus=object())
    close.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("false", False), ("off", False), ("true", True), ("", True)],
)
def test_the_setting_is_read_safely(raw, expected, monkeypatch):
    from axiom.extensions.builtins.release import _legacy_rivet_cli as L
    from axiom.extensions.builtins.settings import store

    monkeypatch.setattr(store.SettingsStore, "get", lambda self, key, default=None: raw)
    assert L.heartbeat_watch_only() is expected


def test_an_unreadable_setting_stays_watch_only(monkeypatch):
    from axiom.extensions.builtins.release import _legacy_rivet_cli as L
    from axiom.extensions.builtins.settings import store

    def broken(self, key, default=None):
        raise RuntimeError("damaged")

    monkeypatch.setattr(store.SettingsStore, "get", broken)
    assert L.heartbeat_watch_only() is True


def test_watch_only_is_the_shipped_default():
    from axiom.extensions.builtins.settings.store import _DEFAULTS

    assert _DEFAULTS["release.heartbeat_watch_only"] is True


# -- the kill switch ---------------------------------------------------------


def test_the_kill_switch_reaches_the_act_step():
    import yaml

    root = Path(__file__).resolve()
    while not (root / ".github").is_dir():
        root = root.parent
    doc = yaml.safe_load((root / ".github" / "workflows" / "rivet-lander.yml").read_text(encoding="utf-8"))
    step = next(s for s in doc["jobs"]["land"]["steps"] if s.get("name") == "Land eligible pull requests")
    assert step["env"]["AXIOM_LANDER_KILL"] == "${{ vars.AXIOM_LANDER_KILL }}"
