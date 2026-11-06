# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The actor loop, the test selector, and a config that names no repository."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.fixes import Diagnosis, Fix, MechanicalFixProvider
from axiom.extensions.builtins.release.lander.actor import (
    answer,
    merge_command,
    push_command,
    run_actor,
)
from axiom.extensions.builtins.release.lander.config import LanderConfig, parse_config, watch_repos
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.selectors import (
    NullSelector,
    TestmonSelector,
    context_from_environment,
)
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore, InMemoryActionStore
from testmon.db import DATA_VERSION

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("soil" + "metrix", "b-tree-" + "labs", "ut" + "exas")


class FakeEffects:
    def __init__(self) -> None:
        self.sha = "aaa"
        self.applied: list[str] = []
        self.landed: list[tuple[int, str, str]] = []
        self.checks = "passing"

    def head(self, number: int) -> str:
        del number
        return self.sha

    def apply(self, number: int, fix: Fix, head_sha: str) -> str:
        del number
        if fix.force or "--force" in fix.diff or head_sha != self.sha:
            raise LanderRefused("refusing the apply")
        self.applied.append(fix.diff)
        self.sha = "bbb"
        return self.sha

    def rerun(self, number: int, sha: str) -> str:
        del number, sha
        return self.checks

    def land(self, number: int, sha: str, method: str) -> None:
        self.landed.append((number, sha, method))

    def close(self, number: int, sha: str) -> None:
        del number, sha


class FakeFixes:
    name = "fake"

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        if pull.checks == "passing":
            return Diagnosis("clear", "green", pull.files)
        return Diagnosis("lint", "ruff found errors", pull.files)

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        del pull
        if diagnosis.kind == "clear":
            return Fix(summary="land", kind="land")
        return Fix(summary="fmt", diff="ruff check --fix\n", commands=("ruff check --fix",), kind="lint")


def _pull(**kwargs: object) -> OpenPull:
    base: dict[str, object] = dict(
        number=4,
        title="change",
        head_sha="aaa",
        head_ref="topic/one",
        files=("src/a.py",),
        checks="failing",
        failure_log="ruff found errors",
        mergeable=True,
    )
    base.update(kwargs)
    return OpenPull(**base)  # type: ignore[arg-type]


def _config(**kwargs: object) -> LanderConfig:
    text = 'mode = "warn"\n'
    config = parse_config(text)
    if kwargs:
        from dataclasses import replace

        config = replace(config, **kwargs)  # type: ignore[arg-type]
    return config


def test_a_change_is_proposed_and_lands_only_after_the_accountable_approves(tmp_path: Path):
    effects = FakeEffects()
    gate = ApprovalGate(InMemoryActionStore())
    config = _config(approvers=("@ada:local",))
    first = run_actor(
        [_pull()],
        repo="owner/name",
        config=config,
        state_dir=tmp_path,
        effects=effects,
        gate=gate,
        provider=FakeFixes(),
    )
    assert first.outcomes[0].status == "waiting"
    assert effects.applied == []
    assert effects.landed == []
    assert "diagnose" in first.outcomes[0].steps
    assert "propose" in first.outcomes[0].steps

    answer(gate, first.outcomes[0].action_id, who="@ada:local", roster=config.approvers, accept=True)
    second = run_actor(
        [_pull()],
        repo="owner/name",
        config=config,
        state_dir=tmp_path,
        effects=effects,
        gate=gate,
        provider=FakeFixes(),
    )
    assert second.outcomes[0].status == "landed"
    assert effects.applied == ["ruff check --fix\n"]
    assert effects.landed == [(4, "bbb", "merge")]
    assert "apply" in second.outcomes[0].steps
    assert "verify" in second.outcomes[0].steps
    assert "land" in second.outcomes[0].steps


def test_a_stranger_cannot_approve_and_a_rejection_does_not_apply(tmp_path: Path):
    effects = FakeEffects()
    gate = ApprovalGate(InMemoryActionStore())
    config = _config(approvers=("@ada:local",))
    first = run_actor(
        [_pull()], repo="owner/name", config=config, state_dir=tmp_path,
        effects=effects, gate=gate, provider=FakeFixes(),
    )
    with pytest.raises(LanderRefused, match="Accountable"):
        answer(gate, first.outcomes[0].action_id, who="@other:local", roster=config.approvers, accept=True)
    answer(
        gate, first.outcomes[0].action_id, who="@ada:local", roster=config.approvers,
        accept=False, reason="no",
    )
    second = run_actor(
        [_pull()], repo="owner/name", config=config, state_dir=tmp_path,
        effects=effects, gate=gate, provider=FakeFixes(),
    )
    assert second.outcomes[0].status == "rejected"
    assert effects.applied == []


def test_a_new_push_invalidates_the_approval(tmp_path: Path):
    effects = FakeEffects()
    gate = ApprovalGate(InMemoryActionStore())
    config = _config(approvers=("@ada:local",))
    first = run_actor(
        [_pull()], repo="owner/name", config=config, state_dir=tmp_path,
        effects=effects, gate=gate, provider=FakeFixes(),
    )
    answer(gate, first.outcomes[0].action_id, who="@ada:local", roster=config.approvers, accept=True)
    effects.sha = "ccc"
    second = run_actor(
        [_pull()], repo="owner/name", config=config, state_dir=tmp_path,
        effects=effects, gate=gate, provider=FakeFixes(),
    )
    assert second.outcomes[0].status == "invalidated"
    assert effects.applied == []
    assert effects.landed == []


def test_an_unanswered_proposal_expires(tmp_path: Path):
    store = FileActionStore(
        tmp_path / "approvals.json",
        ttl_hours=1,
        now=datetime.now(UTC) + timedelta(days=3),
    )
    gate = ApprovalGate(store)
    effects = FakeEffects()
    config = _config(approvers=("@ada:local",))
    first = run_actor(
        [_pull()], repo="owner/name", config=config, state_dir=tmp_path,
        effects=effects, gate=gate, provider=FakeFixes(),
    )
    assert first.outcomes[0].status == "waiting"
    second = run_actor(
        [_pull()], repo="owner/name", config=config, state_dir=tmp_path,
        effects=effects, gate=gate, provider=FakeFixes(),
    )
    assert second.outcomes[0].status == "expired"
    assert effects.applied == []


def test_the_kill_switch_stops_before_a_proposal(tmp_path: Path):
    result = run_actor(
        [_pull()],
        repo="owner/name",
        config=_config(approvers=("@ada:local",), kill_switch_env="AXIOM_LANDER_KILL"),
        state_dir=tmp_path,
        effects=FakeEffects(),
        gate=ApprovalGate(InMemoryActionStore()),
        provider=FakeFixes(),
        environ={"AXIOM_LANDER_KILL": "1"},
    )
    assert result.stopped == "kill-switch"
    assert result.outcomes == []


def test_a_human_routed_path_is_not_given_a_diff(tmp_path: Path):
    result = run_actor(
        [_pull(files=("src/auth/session.py",))],
        repo="owner/name",
        config=_config(approvers=("@ada:local",)),
        state_dir=tmp_path,
        effects=FakeEffects(),
        gate=ApprovalGate(InMemoryActionStore()),
        provider=MechanicalFixProvider(),
    )
    assert result.outcomes[0].status == "escalated"
    assert result.outcomes[0].reason == "human_route"


def test_push_and_merge_commands_cannot_force():
    assert "--force" not in push_command("origin", "topic/one")
    assert "--force" not in merge_command(4, "merge", "abc")
    assert "--auto" in merge_command(4, "enqueue", "abc")
    with pytest.raises(LanderRefused):
        push_command("origin", "+topic/one")


def test_selector_fallbacks_and_a_real_testmon_map(tmp_path: Path):
    assert NullSelector().select(("src/a.py",), "base", "head").run_all
    missing = TestmonSelector(database=tmp_path / "absent.testmondata", context="pull_request")
    assert missing.select(("src/a.py",), "", "").reason == "missing-db"
    assert TestmonSelector(context="merge-queue").select(("src/a.py",), "", "").reason == "merge-queue"
    assert TestmonSelector(context="main").select(("src/a.py",), "", "").reason == "main"
    setup = TestmonSelector(database=tmp_path / "absent.testmondata", context="pull_request")
    assert setup.select(("tests/conftest.py",), "", "").reason == "test-setup"
    assert setup.select(("pyproject.toml",), "", "").reason == "dependencies"
    assert setup.select(("web/package-lock.json",), "", "").reason == "dependencies"
    assert setup.select(("services/Dockerfile",), "", "").reason == "dependencies"
    assert setup.select(("reqs/dev-requirements.txt",), "", "").reason == "dependencies"
    assert context_from_environment({"GITHUB_EVENT_NAME": "merge_group"}) == "merge-queue"
    assert context_from_environment({}) == "pull_request"

    database = tmp_path / ".testmondata"
    connection = sqlite3.connect(database)
    connection.execute(f"PRAGMA user_version = {DATA_VERSION}")
    connection.executescript(
        """
        CREATE TABLE metadata (dataid TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE file_fp (
            id INTEGER PRIMARY KEY, filename TEXT, method_checksums BLOB,
            mtime FLOAT, fsha TEXT
        );
        CREATE TABLE test_execution (
            id INTEGER PRIMARY KEY, environment_id INTEGER, test_name TEXT,
            duration FLOAT, failed BIT, forced BIT
        );
        CREATE TABLE test_execution_file_fp (
            test_execution_id INTEGER, fingerprint_id INTEGER
        );
        """
    )
    connection.execute("INSERT INTO metadata VALUES ('base_sha', 'base')")
    connection.execute("INSERT INTO file_fp VALUES (1, 'src/a.py', NULL, NULL, NULL)")
    connection.execute("INSERT INTO test_execution VALUES (1, 1, 'tests/test_a.py::test_one', 10, 0, NULL)")
    connection.execute("INSERT INTO test_execution VALUES (2, 1, 'tests/test_b.py::test_two', 30, 0, NULL)")
    connection.execute("INSERT INTO test_execution_file_fp VALUES (1, 1)")
    connection.commit()
    connection.close()
    selector = TestmonSelector(database=database, context="pull_request")
    chosen = selector.select(("src/a.py",), "base", "head")
    assert chosen.run_all is False
    assert chosen.tests == ("tests/test_a.py::test_one",)
    assert chosen.fraction == pytest.approx(0.25)
    assert selector.select(("src/a.py",), "other", "head").reason == "stale-db"
    assert database.is_file()


def test_config_examples_parse_and_the_watch_list_is_not_hard_coded(tmp_path: Path):
    examples = Path(__file__).resolve().parents[1] / "docs" / "examples"
    python_repo = parse_config((examples / "python-github.toml").read_text(encoding="utf-8"))
    other = parse_config((examples / "non-python.toml").read_text(encoding="utf-8"))
    assert python_repo.selector == "testmon"
    assert python_repo.fix_provider == "mechanical"
    assert python_repo.approvers == ("@approver:local",)
    assert python_repo.auto_allowed == ()
    assert python_repo.auto_approver == ""
    assert python_repo.claim_label == "rivet:claimed"
    assert python_repo.foreign_claim_labels == ()
    assert other.selector == "null"
    assert other.fix_provider == "null"
    assert other.forge == "gitlab"
    assert other.land_method == "enqueue"
    with pytest.raises(LanderRefused, match="token"):
        parse_config('token_value = "ghp_example"\n')
    assert watch_repos(tmp_path, environ={}) == ()
    assert watch_repos(tmp_path, environ={"AXIOM_LANDER_REPOS": "group/name"}) == ("group/name",)
    assert watch_repos(tmp_path, cli=("cli/name",), environ={"AXIOM_LANDER_REPOS": "group/name"}) == ("cli/name",)


def test_the_lander_and_selectors_name_no_organisation():
    roots = [
        ROOT / "lander",
        ROOT / "selectors.py",
        ROOT / "fixes.py",
        ROOT / "handoff.py",
        ROOT / "lander" / "protect.py",
        ROOT / "lander" / "claim_label.py",
        ROOT / "docs" / "examples",
        ROOT / "docs" / "specs" / "spec.md",
        ROOT / "docs" / "prds" / "prd.md",
    ]
    offenders: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix not in {".py", ".md", ".toml"}:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.startswith("# Copyright") or line.startswith("Copyright"):
                    continue
                lowered = line.lower()
                if any(word in lowered for word in FORBIDDEN):
                    offenders.append(f"{path}:{number}")
    assert offenders == []
