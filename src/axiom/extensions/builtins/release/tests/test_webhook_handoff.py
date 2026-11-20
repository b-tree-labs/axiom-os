# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Webhook handoff, opt-in auto-approval, and claim labels."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.fixes import Diagnosis, Fix
from axiom.extensions.builtins.release.handoff import (
    FIX_REQUEST_SCHEMA,
    HandoffError,
    SideBranch,
    WebhookHandoffFixProvider,
    build_fix_request,
    fix_branch_name,
    post_fix_request,
)
from axiom.extensions.builtins.release.lander.actor import (
    commands_for_side_branch,
    label_command,
    merge_command,
    push_command,
    run_actor,
)
from axiom.extensions.builtins.release.lander.claim_label import claim_record, record_claim
from axiom.extensions.builtins.release.lander.config import parse_config
from axiom.extensions.builtins.release.lander.model import FailingCheck, OpenPull
from axiom.infra.orchestrator.actions import Action
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore
from axiom.infra.switchplan.ledger import FileLedger

KEY = "sekrit-key-value"
ENV = {
    "RELEASE_FIX_WEBHOOK_URL": "https://hooks.example/fix",
    "RELEASE_FIX_WEBHOOK_KEY": KEY,
}
HEAD = "0123456789abcdef"
EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples"
_FORCE = ("--force", "--force-with-lease", "-f")


class MemoryBranches:
    def __init__(self) -> None:
        self.rows: dict[str, SideBranch] = {}

    def put(self, branch: SideBranch) -> None:
        self.rows[branch.name] = branch

    def get(self, repo: str, branch: str, head_sha: str) -> SideBranch | None:
        del repo, head_sha
        return self.rows.get(branch)


class LabelEffects:
    def __init__(self) -> None:
        self.sha = HEAD
        self.applied: list[str] = []
        self.landed: list[tuple[int, str, str]] = []
        self.reran: list[str] = []
        self.added: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []
        self.checks = "passing"

    def head(self, number: int) -> str:
        del number
        return self.sha

    def apply(self, number: int, fix: Fix, head_sha: str) -> str:
        del number
        assert not fix.force
        assert "--force" not in fix.diff
        assert head_sha == self.sha
        self.applied.append(fix.diff)
        self.sha = "bbbcccddddeeefff"
        return self.sha

    def rerun(self, number: int, sha: str) -> str:
        del number
        self.reran.append(sha)
        return self.checks

    def land(self, number: int, sha: str, method: str) -> None:
        self.landed.append((number, sha, method))

    def close(self, number: int, sha: str) -> None:
        del number, sha

    def add_label(self, number: int, label: str) -> None:
        self.added.append((number, label))

    def remove_label(self, number: int, label: str) -> None:
        self.removed.append((number, label))


class Boom:
    name = "boom"

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        raise AssertionError(f"skipped pull {pull.number} was diagnosed")

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        del diagnosis, pull
        raise AssertionError("skipped pull was drafted")


def _config(text: str | None = None, **kwargs: object):
    raw = text or (
        'mode = "warn"\n'
        'language = "python"\n'
        "[fix_provider]\n"
        'name = "handoff"\n'
        "[fix_provider.webhook]\n"
        'url_env = "RELEASE_FIX_WEBHOOK_URL"\n'
        'key_env = "RELEASE_FIX_WEBHOOK_KEY"\n'
        'key_header = "X-Fix-Key"\n'
        "separate_reviewer = true\n"
        "[raci]\n"
        'approvers = ["@approver:local"]\n'
    )
    config = parse_config(raw)
    if kwargs:
        config = replace(config, **kwargs)
    return config


def _pull(**kwargs: object) -> OpenPull:
    base: dict[str, object] = dict(
        number=4,
        title="change",
        head_sha=HEAD,
        head_ref="topic/one",
        base_ref="main",
        files=("src/a.py",),
        checks="failing",
        failure_log="Some never-before-seen exotic failure in the build",
        mergeable=True,
        failing_checks=(FailingCheck("unit", "https://ci.example/logs/1"),),
    )
    base.update(kwargs)
    return OpenPull(**base)  # type: ignore[arg-type]


def _provider(tmp_path: Path, branches: MemoryBranches, posts: list, **kwargs: object):
    def poster(url: str, body: dict, *, header: str, key: str) -> None:
        posts.append({"url": url, "body": body, "header": header, "key": key})

    return WebhookHandoffFixProvider(
        poster=poster,
        branches=branches,
        config=_config(**kwargs),
        state_dir=tmp_path,
        repo="owner/name",
        environ=dict(ENV),
    )


def _run(tmp_path: Path, pulls, *, provider, config, effects=None, gate=None):
    return run_actor(
        pulls,
        repo="owner/name",
        config=config,
        state_dir=tmp_path,
        effects=effects or LabelEffects(),
        gate=gate or ApprovalGate(InMemoryActionStore()),
        provider=provider,
        environ=dict(ENV),
    )


def test_a_blocked_pull_requests_a_fix_once(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    posts: list = []
    branches = MemoryBranches()
    provider = _provider(tmp_path, branches, posts)
    pull = _pull()
    diagnosis = provider.diagnose(pull)
    with caplog.at_level("INFO"):
        first = provider.draft(diagnosis, pull)
        second = provider.draft(diagnosis, pull)
    assert first.pending and second.pending
    assert len(posts) == 1
    body = posts[0]["body"]
    assert list(body) == list(FIX_REQUEST_SCHEMA["required"])
    assert body == build_fix_request(repo="owner/name", pull=pull, diagnosis=diagnosis)
    assert body["branch"] == fix_branch_name(4, HEAD) == "rivet-fix/4-0123456"
    assert body["diagnosis"] == body["blocker"] == "failing_test"
    assert body["failing_checks"] == [{"name": "unit", "log_url": "https://ci.example/logs/1"}]
    assert posts[0]["header"] == "X-Fix-Key"
    assert posts[0]["key"] == KEY
    records = [item for item in FileLedger(tmp_path / "agents" / "rivet" / "lander-records").records() if item.kind == "release.handoff"]
    assert len(records) == 1
    assert records[0].state["head_sha"] == HEAD
    blob = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file())
    assert KEY not in blob
    assert KEY not in caplog.text


def test_a_moved_head_sends_a_fresh_request_and_a_stale_branch_is_ignored(tmp_path: Path):
    posts: list = []
    branches = MemoryBranches()
    provider = _provider(tmp_path, branches, posts)
    pull = _pull()
    diagnosis = provider.diagnose(pull)
    provider.draft(diagnosis, pull)
    branches.put(
        SideBranch(
            name=fix_branch_name(4, HEAD),
            sha="fff",
            base_sha="not-the-head",
            author="@writer:hook",
            diff="stale diff\n",
            paths=("src/a.py",),
        )
    )
    ignored = provider.draft(diagnosis, pull)
    assert ignored.pending
    assert ignored.diff == ""
    assert len(posts) == 1

    moved = _pull(head_sha="deadbeefcafebabe")
    provider.draft(provider.diagnose(moved), moved)
    assert len(posts) == 2
    assert posts[1]["body"]["head_sha"] == "deadbeefcafebabe"
    assert posts[1]["body"]["branch"] == "rivet-fix/4-deadbee"
    assert posts[0]["body"]["branch"] != posts[1]["body"]["branch"]


def test_a_fresh_side_branch_is_proposed_like_a_built_in_fix(tmp_path: Path):
    posts: list = []
    branches = MemoryBranches()
    branches.put(
        SideBranch(
            name=fix_branch_name(4, HEAD),
            sha="fff",
            base_sha=HEAD,
            author="@writer:hook",
            diff="fixed the test\n",
            paths=("src/a.py",),
        )
    )
    provider = _provider(tmp_path, branches, posts)
    config = _config()
    effects = LabelEffects()
    gate = ApprovalGate(InMemoryActionStore())
    first = _run(tmp_path, [_pull()], provider=provider, config=config, effects=effects, gate=gate)
    assert posts == []
    assert first.outcomes[0].status == "waiting"
    assert effects.applied == []
    assert effects.landed == []
    stored = gate.get(first.outcomes[0].action_id)
    assert stored is not None
    assert stored.params["branch"] == "rivet-fix/4-0123456"
    assert stored.params["head_sha"] == HEAD
    assert stored.params["diff"] == "fixed the test\n"
    from axiom.extensions.builtins.release.lander.actor import answer

    answer(gate, first.outcomes[0].action_id, who="@approver:local", roster=config.approvers, accept=True)
    second = _run(tmp_path, [_pull()], provider=provider, config=config, effects=effects, gate=gate)
    assert second.outcomes[0].status == "landed"
    assert effects.applied == ["fixed the test\n"]
    assert effects.landed == [(4, effects.sha, "merge")]
    assert effects.reran[-1] == effects.landed[0][1]
    assert "--match-head-commit" in merge_command(4, "merge", effects.landed[0][1])


def test_side_branch_and_label_commands_never_force():
    commands = commands_for_side_branch("topic/one", "rivet-fix/4-0123456", HEAD)
    flat = [arg for command in commands for arg in command]
    assert not any(flag in flat for flag in _FORCE)
    assert not any(arg.startswith("+") for arg in flat)
    assert ("git", "merge", "--ff-only", "FETCH_HEAD") in commands
    assert ("git", "merge", "--no-ff", "--no-edit", "FETCH_HEAD") in commands
    pushed = commands[-1]
    assert pushed[:3] == ("git", "push", "origin")
    assert "--force" not in push_command("origin", "topic/one")
    for remove in (False, True):
        command = label_command("owner/name", 4, "rivet:claimed", remove=remove)
        assert not any(flag in command for flag in _FORCE)

    from axiom.extensions.builtins.release.lander.actor import GitEffects
    from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused

    class Recording(GitEffects):
        def __init__(self) -> None:
            super().__init__(repo="owner/name", token_env="GH_TOKEN")
            self.calls: list[list[str]] = []

        def _run(self, args: list[str]) -> str:
            self.calls.append(list(args))
            if tuple(args[:3]) == ("git", "merge", "--ff-only"):
                raise LanderRefused("not a fast-forward")
            if args[:2] == ["git", "rev-parse"]:
                return "abc\n"
            return ""

    effects = Recording()
    fix = Fix(summary="side", diff="fixed\n", branch="rivet-fix/4-0123456", target_ref="topic/one", kind="repair")
    assert effects.apply(4, fix, HEAD) == "abc"
    seen = [arg for call in effects.calls for arg in call]
    assert not any(flag in seen for flag in _FORCE)
    assert ["git", "merge", "--no-ff", "--no-edit", "FETCH_HEAD"] in effects.calls
    assert ["git", "push", "origin", "HEAD:topic/one"] in effects.calls


def test_auto_approval_is_off_until_a_repo_opts_in(tmp_path: Path):
    enabled = parse_config((EXAMPLES / "auto-approve.toml").read_text(encoding="utf-8"))
    assert enabled.auto_approver == "@reviewer:local"
    assert enabled.claim_label == "rivet:claimed"
    assert parse_config('mode = "warn"\n[raci]\napprovers = ["@approver:local"]\n').auto_approver == ""
    posts: list = []
    provider = _provider(tmp_path, MemoryBranches(), posts)
    effects = LabelEffects()
    gate = ApprovalGate(InMemoryActionStore())
    result = _run(tmp_path, [_pull()], provider=provider, config=_config(), effects=effects, gate=gate)
    assert result.outcomes[0].status == "waiting"
    assert effects.landed == []
    assert not list((tmp_path / "agents" / "rivet" / "lander-records").glob("*.json")) or all(
        json.loads(path.read_text(encoding="utf-8")).get("kind") != "release.auto-approve"
        for path in (tmp_path / "agents" / "rivet" / "lander-records").glob("*.json")
    )


def test_a_different_reviewer_auto_approves_a_routine_fix_only_when_ci_is_green(tmp_path: Path):
    posts: list = []
    branches = MemoryBranches()
    branches.put(
        SideBranch(
            name=fix_branch_name(4, HEAD),
            sha="fff",
            base_sha=HEAD,
            author="@writer:hook",
            diff="fixed the test\n",
            paths=("src/a.py",),
        )
    )
    provider = _provider(tmp_path, branches, posts, auto_approver="@reviewer:local")
    config = _config(auto_approver="@reviewer:local")
    effects = LabelEffects()
    gate = ApprovalGate(InMemoryActionStore())
    result = _run(tmp_path, [_pull()], provider=provider, config=config, effects=effects, gate=gate)
    assert result.outcomes[0].status == "landed"
    assert effects.landed[0][1] == effects.reran[-1]
    assert effects.landed[0][1] != HEAD
    records = [
        item
        for item in FileLedger(tmp_path / "agents" / "rivet" / "lander-records").records()
        if item.kind == "release.auto-approve"
    ]
    assert len(records) == 1
    assert records[0].state["reviewer"] == "@reviewer:local"
    assert "differs from fix writer @writer:hook" in records[0].state["rationale"]
    assert records[0].approver == "@reviewer:local"

    red = LabelEffects()
    red.checks = "failing"
    red_gate = ApprovalGate(InMemoryActionStore())
    capped = _run(
        tmp_path / "red",
        [_pull()],
        provider=provider,
        config=replace(config, max_rounds=1),
        effects=red,
        gate=red_gate,
    )
    assert capped.outcomes[0].status == "escalated"
    assert capped.outcomes[0].reason == "round cap"
    assert red.landed == []
    assert red.applied == ["fixed the test\n"]


def test_the_fix_writer_cannot_auto_approve_and_protected_paths_stay_human(tmp_path: Path):
    posts: list = []
    branches = MemoryBranches()

    def plant(author: str, path: str) -> None:
        branches.rows.clear()
        branches.put(
            SideBranch(
                name=fix_branch_name(4, HEAD),
                sha="fff",
                base_sha=HEAD,
                author=author,
                diff="change\n",
                paths=(path,),
            )
        )

    plant("@reviewer:local", "src/a.py")
    provider = _provider(tmp_path, branches, posts, auto_approver="@reviewer:local")
    config = _config(auto_approver="@reviewer:local")
    same = _run(tmp_path, [_pull()], provider=provider, config=config, effects=LabelEffects(), gate=ApprovalGate(InMemoryActionStore()))
    assert same.outcomes[0].status == "waiting"

    protected = [
        "src/behavior/rule.py",
        "src/pricing/list.py",
        "src/auth/session.py",
        "migrations/001.sql",
        "tests/conftest.py",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "Jenkinsfile",
    ]
    for path in protected:
        plant("@writer:hook", path)
        directory = tmp_path / path.replace("/", "_")
        outcome = _run(
            directory,
            [_pull(files=(path,))],
            provider=provider,
            config=config,
            effects=LabelEffects(),
            gate=ApprovalGate(InMemoryActionStore()),
        )
        assert outcome.outcomes[0].status == "waiting", path
        assert outcome.outcomes[0].reason != "handoff"


def test_an_expired_approval_is_not_applied(tmp_path: Path):
    fix = Fix(summary="fmt", diff="hello\n", kind="lint", writer="@writer:hook")
    gate = ApprovalGate(InMemoryActionStore())
    action = Action(
        name="release.repair",
        params={
            "pr": 4,
            "action_type": "repair",
            "head_sha": HEAD,
            "diff": fix.diff,
            "diff_sha": fix.diff_sha,
            "paths": ["src/a.py"],
            "land_if_green": True,
            "approved_until": "2000-01-01T00:00:00+00:00",
            "writer": "@writer:hook",
        },
    )
    action.approve(decided_by="@reviewer:local")
    gate.record(action)

    class SameDiff:
        name = "same"

        def diagnose(self, pull: OpenPull) -> Diagnosis:
            return Diagnosis("lint", "ruff", pull.files)

        def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
            del diagnosis, pull
            return fix

    effects = LabelEffects()
    result = _run(
        tmp_path,
        [_pull(failure_log="ruff found errors")],
        provider=SameDiff(),
        config=_config(auto_approver="@reviewer:local"),
        effects=effects,
        gate=gate,
    )
    assert result.outcomes[0].status == "expired"
    assert effects.applied == []
    assert effects.landed == []


def test_a_foreign_claim_is_skipped_and_an_expired_claim_is_removed(tmp_path: Path):
    config = _config(foreign_claim_labels=("external:working",))
    effects = LabelEffects()
    skipped = _run(
        tmp_path,
        [_pull(labels=("external:working",))],
        provider=Boom(),
        config=config,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
    )
    assert skipped.outcomes[0].status == "skipped"
    assert skipped.outcomes[0].reason == "foreign-claim:external:working"
    assert effects.added == []
    assert effects.removed == []
    assert effects.applied == []

    old = (datetime.now(UTC) - timedelta(seconds=config.claim_ttl_s + 5)).isoformat()
    path = tmp_path / "agents" / "rivet" / "claim-labels.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"owner/name#4": {"label": "rivet:claimed", "head_sha": HEAD, "at": old}}),
        encoding="utf-8",
    )
    expired_effects = LabelEffects()
    expired = _run(
        tmp_path,
        [_pull(labels=("rivet:claimed",))],
        provider=Boom(),
        config=config,
        effects=expired_effects,
        gate=ApprovalGate(InMemoryActionStore()),
    )
    assert expired.outcomes[0].status == "claim-expired"
    assert expired_effects.removed == [(4, "rivet:claimed")]
    assert expired_effects.added == []
    assert claim_record(tmp_path, "owner/name", 4) is None

    fresh = LabelEffects()
    acted = _run(
        tmp_path / "fresh",
        [_pull()],
        provider=WebhookHandoffFixProvider(
            poster=lambda *args, **kwargs: None,
            branches=MemoryBranches(),
            config=config,
            state_dir=tmp_path / "fresh",
            repo="owner/name",
            environ=dict(ENV),
        ),
        config=config,
        effects=fresh,
        gate=ApprovalGate(InMemoryActionStore()),
    )
    assert acted.outcomes[0].status == "waiting"
    assert fresh.added == [(4, "rivet:claimed")]
    held = claim_record(tmp_path / "fresh", "owner/name", 4)
    assert held is not None
    assert held["label"] == "rivet:claimed"


def test_the_kill_switch_still_stops_before_a_claim_or_an_auto_approval(tmp_path: Path):
    effects = LabelEffects()
    result = run_actor(
        [_pull()],
        repo="owner/name",
        config=_config(auto_approver="@reviewer:local"),
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=Boom(),
        environ={"AXIOM_LANDER_KILL": "1"},
    )
    assert result.stopped == "kill-switch"
    assert result.outcomes == []
    assert effects.added == []
    assert effects.landed == []


def test_the_sender_key_is_redacted_when_the_post_fails(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch):
    def explode(request, timeout=30):
        del request, timeout
        raise OSError(f"connection failed while sending {KEY}")

    monkeypatch.setattr("axiom.extensions.builtins.release.handoff.urllib.request.urlopen", explode)
    body = {"repo": "owner/name", "pr": 4, "branch": "rivet-fix/4-0123456", "blocker": "failing_test"}
    with caplog.at_level("INFO"):
        with pytest.raises(HandoffError, match="redacted") as caught:
            post_fix_request("https://hooks.example/fix", body, header="X-Fix-Key", key=KEY)
    assert KEY not in str(caught.value)
    assert KEY not in caplog.text


def test_record_claim_round_trip(tmp_path: Path):
    record_claim(tmp_path, repo="owner/name", number=4, label="rivet:claimed", head_sha=HEAD)
    assert claim_record(tmp_path, "owner/name", 4)["head_sha"] == HEAD
