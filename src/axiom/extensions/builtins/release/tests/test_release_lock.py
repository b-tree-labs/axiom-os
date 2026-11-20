# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The release lock: no landing between "bump opened" and "tag pushed" (#1782).

Every forge call goes through a fake that records it. Nothing here touches a
network, arms a real pull request, or sends a real notification.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.release import release_lock as RL
from axiom.extensions.builtins.release.ci_health.alerts import Alert
from axiom.extensions.builtins.release.lander.skill import land
from axiom.infra.skills import SkillContext, SkillRegistry

REPO = "example-org/app"
NOW = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)


class FakeForge:
    """Armed pull requests, one repository variable, and a log of every write."""

    def __init__(self, armed=None, *, variable: str | None = None, arm_fails: set[int] | None = None) -> None:
        self.armed = list(armed or [])
        self.variable = variable
        self.arm_fails = arm_fails or set()
        self.disarmed: list[int] = []
        self.rearmed: list[tuple[int, str, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.variable_writes: list[str] = []

    def get_variable(self, repo: str, name: str) -> str | None:
        assert repo == REPO and name == RL.LOCK_VARIABLE
        return self.variable

    def set_variable(self, repo: str, name: str, value: str) -> None:
        self.variable = value
        self.variable_writes.append(value)

    def delete_variable(self, repo: str, name: str) -> bool:
        existed = self.variable is not None
        self.variable = None
        return existed

    def armed_pulls(self, repo: str) -> list[dict]:
        return [dict(row) for row in self.armed]

    def disarm(self, repo: str, number: int) -> None:
        self.disarmed.append(number)
        self.armed = [row for row in self.armed if row["number"] != number]

    def arm(self, repo: str, number: int, head_sha: str, method: str) -> None:
        if number in self.arm_fails:
            raise RL.ForgeWriteFailed(f"#{number}: head moved; expected {head_sha}")
        self.rearmed.append((number, head_sha, method))

    def comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))


ARMED = [
    {"number": 7, "head_sha": "h7", "merge_method": "MERGE"},
    {"number": 9, "head_sha": "h9", "merge_method": "SQUASH"},
]


def _sender(sent: list):
    def send(recipient: str, alert: Alert) -> str:
        sent.append((recipient, alert))
        return f"receipt-{len(sent)}"

    return send


def _take(tmp_path: Path, forge: FakeForge, *, sent=None, minutes: int = 30, now: datetime = NOW) -> RL.ReleaseLock:
    return RL.take(
        tmp_path,
        REPO,
        reason="release v0.71.0",
        held_by="@releaser",
        forge=forge,
        now=now,
        minutes=minutes,
        sender=_sender(sent) if sent is not None else None,
        recipients=("@operator",) if sent is not None else (),
    )


# -- take ------------------------------------------------------------------------


def test_taking_the_lock_disarms_armed_pull_requests_comments_and_marks_the_repo(tmp_path: Path):
    forge = FakeForge(ARMED)
    sent: list = []
    lock = _take(tmp_path, forge, sent=sent)
    assert forge.disarmed == [7, 9]
    assert [number for number, _ in forge.comments] == [7, 9]
    body = forge.comments[0][1]
    assert "release v0.71.0" in body and "@releaser" in body and "22:30Z" in body, body
    assert "re-arm" in body.lower()
    # The repo-level marker is a GitHub Actions repository variable; its value is the lock.
    assert lock.marker == f"variable:{RL.LOCK_VARIABLE}"
    payload = json.loads(forge.variable)
    assert payload["reason"] == "release v0.71.0" and [d["number"] for d in payload["disarmed"]] == [7, 9]
    # The small state file remembers what to re-arm.
    saved = RL.load(tmp_path)
    assert saved is not None and [d.number for d in saved.disarmed] == [7, 9]
    assert saved.disarmed[1].merge_method == "squash"
    assert saved.expires_at == (NOW + timedelta(minutes=30)).isoformat()
    # HERALD hears about the window once, with the roster configured.
    assert len(sent) == 1 and sent[0][0] == "@operator"
    assert sent[0][1].kind == "release_lock" and "22:30Z" in sent[0][1].summary
    assert RL.current(tmp_path, REPO, forge, now=NOW) is not None


def test_a_second_release_cannot_take_a_held_lock(tmp_path: Path):
    forge = FakeForge()
    _take(tmp_path, forge)
    with pytest.raises(RL.LockHeld) as held:
        RL.take(tmp_path, REPO, reason="another", held_by="@someone", forge=forge, now=NOW + timedelta(minutes=5))
    assert "release v0.71.0" in str(held.value)


def test_no_roster_means_no_announcement_and_no_failure(tmp_path: Path):
    forge = FakeForge(ARMED)
    lock = _take(tmp_path, forge)
    assert lock.announced is False


# -- while held: the land skill arms nothing and disarms what it finds ------------------


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("lock-test"))


def _pull_row(number: int, sha: str) -> dict:
    return {
        "number": number,
        "title": "change",
        "head_sha": sha,
        "head_ref": f"topic/{number}",
        "checks": "passing",
        "mergeable": True,
        "files": [f"src/{number}.py"],
    }


def test_the_land_skill_refuses_to_merge_while_the_lock_is_held_and_disarms_newcomers(tmp_path: Path, capsys):
    forge = FakeForge(ARMED)
    _take(tmp_path, forge, now=datetime.now(UTC))  # the skill reads the real clock
    # A person armed #11 after the lock was taken.
    forge.armed.append({"number": 11, "head_sha": "h11", "merge_method": "MERGE"})
    (tmp_path / ".axiom").mkdir()
    result = land(
        {
            "repo": REPO,
            "act": True,
            "pulls": [_pull_row(11, "h11"), _pull_row(12, "h12")],
            "_lock_forge": forge,
            "format": "json",
            "state_dir": str(tmp_path),
            "no_ci_lint": True,
        },
        _ctx(tmp_path),
    )
    assert result.ok
    assert result.value["stopped"] == "release-lock"
    assert result.value["outcomes"] == []
    assert "release v0.71.0" in result.value["reason"]
    assert 11 in forge.disarmed and result.value["disarmed"] == [11]
    assert [d.number for d in RL.load(tmp_path).disarmed] == [7, 9, 11]
    assert json.loads(capsys.readouterr().out)["stopped"] == "release-lock"


def test_a_lock_taken_on_another_machine_is_seen_through_the_repo_marker(tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    forge = FakeForge(ARMED)
    _take(elsewhere, forge)
    here = tmp_path / "here"
    seen = RL.current(here, REPO, forge, now=NOW + timedelta(minutes=10))
    assert seen is not None
    assert seen.marker.startswith("variable:") and seen.reason == "release v0.71.0"
    assert [d.number for d in seen.disarmed] == [7, 9]


# -- lift: on tag ----------------------------------------------------------------


def test_lifting_on_tag_rearms_what_was_disarmed_comments_and_clears_the_marker(tmp_path: Path):
    forge = FakeForge(ARMED)
    sent: list = []
    _take(tmp_path, forge, sent=sent)
    forge.comments.clear()
    result = RL.lift(
        tmp_path, REPO, forge=forge, now=NOW + timedelta(minutes=12), why="tag pushed",
        sender=_sender(sent), recipients=("@operator",),
    )
    assert result.rearmed == [7, 9] and result.not_rearmed == []
    assert forge.rearmed == [(7, "h7", "merge"), (9, "h9", "squash")]
    assert [number for number, _ in forge.comments] == [7, 9]
    assert "tag pushed" in forge.comments[0][1] and "re-armed" in forge.comments[0][1]
    assert forge.variable is None
    assert RL.load(tmp_path) is None
    assert RL.current(tmp_path, REPO, forge, now=NOW + timedelta(minutes=13)) is None
    history = [json.loads(line) for line in (RL.history_path(tmp_path)).read_text().splitlines()]
    assert [row["event"] for row in history] == ["taken", "lifted"]
    assert len(sent) == 2 and "closed" in sent[1][1].summary.lower()


def test_a_pull_request_whose_head_moved_is_not_rearmed_and_the_comment_says_so(tmp_path: Path):
    forge = FakeForge(ARMED, arm_fails={9})
    _take(tmp_path, forge)
    forge.comments.clear()
    result = RL.lift(tmp_path, REPO, forge=forge, now=NOW + timedelta(minutes=5))
    assert result.rearmed == [7]
    assert result.not_rearmed == [(9, "#9: head moved; expected h9")]
    assert any(number == 9 and "not re-armed" in body for number, body in forge.comments)


def test_lifting_a_lock_already_lifted_elsewhere_rearms_nothing_twice(tmp_path: Path):
    forge = FakeForge(ARMED)
    _take(tmp_path, forge)
    forge.variable = None  # another machine lifted it and deleted the marker
    result = RL.lift(tmp_path, REPO, forge=forge, now=NOW + timedelta(minutes=5))
    assert result.rearmed == [] and forge.rearmed == []
    assert RL.load(tmp_path) is None


def test_lifting_with_nothing_held_is_a_quiet_no_op(tmp_path: Path):
    forge = FakeForge()
    result = RL.lift(tmp_path, REPO, forge=forge, now=NOW)
    assert result.lock is None and result.rearmed == []


# -- the timeout -----------------------------------------------------------------


def test_the_lock_lifts_itself_after_the_timeout_and_rearms(tmp_path: Path):
    forge = FakeForge(ARMED)
    _take(tmp_path, forge, minutes=30)
    before = NOW + timedelta(minutes=29)
    assert RL.current(tmp_path, REPO, forge, now=before) is not None
    assert RL.expire_due(tmp_path, REPO, forge=forge, now=before) is None
    after = NOW + timedelta(minutes=31)
    assert RL.current(tmp_path, REPO, forge, now=after) is None, "an expired lock is not in force"
    result = RL.expire_due(tmp_path, REPO, forge=forge, now=after)
    assert result is not None and result.why == "timeout"
    assert result.rearmed == [7, 9]
    assert forge.variable is None and RL.load(tmp_path) is None


def test_the_land_skill_lifts_an_expired_lock_before_it_acts(tmp_path: Path):
    forge = FakeForge(ARMED)
    sent: list = []
    _take(tmp_path, forge, now=datetime.now(UTC) - timedelta(hours=1))
    result = land(
        {
            "repo": REPO,
            "act": True,
            "pulls": [],
            "_lock_forge": forge,
            "_sender": _sender(sent),
            "format": "json",
            "state_dir": str(tmp_path),
            "no_ci_lint": True,
        },
        _ctx(tmp_path),
    )
    # The lock lifted and re-armed before the actor was consulted (which, with no
    # approver configured, refuses: that refusal is not the lock's).
    assert (result.value or {}).get("stopped") != "release-lock"
    assert forge.rearmed == [(7, "h7", "merge"), (9, "h9", "squash")]
    assert RL.load(tmp_path) is None
    assert sent and "closed" in sent[0][1].summary.lower()


# -- the verbs for a person ------------------------------------------------------------


def test_lock_unlock_and_status_verbs_are_thin_skills(tmp_path: Path, capsys):
    from axiom.extensions.builtins.release import skills as S

    forge = FakeForge(ARMED)
    ctx = _ctx(tmp_path)
    taken = S.lock({"repo": REPO, "reason": "manual window", "minutes": "20", "_lock_forge": forge, "format": "json"}, ctx)
    assert taken.ok and taken.value["lock"]["reason"] == "manual window"
    assert json.loads(capsys.readouterr().out)["held"] is True
    shown = S.lock_status({"repo": REPO, "_lock_forge": forge}, ctx)
    assert shown.ok and shown.value["held"] is True
    assert "manual window" in capsys.readouterr().out
    again = S.lock({"repo": REPO, "_lock_forge": forge}, ctx)
    assert not again.ok and "held" in again.errors[0]
    lifted = S.unlock({"repo": REPO, "_lock_forge": forge}, ctx)
    assert lifted.ok and lifted.value["rearmed"] == [7, 9]
    assert S.lock_status({"repo": REPO, "_lock_forge": forge}, ctx).value["held"] is False
    assert "no release lock" in capsys.readouterr().out


def test_the_cli_has_the_verbs_and_they_need_no_checkout():
    from axiom.extensions.builtins.release.cli import _needs_repo, build_parser

    parser = build_parser()
    assert parser.parse_args(["lock", "--repo", REPO, "--reason", "x", "--minutes", "15"]).minutes == 15
    assert parser.parse_args(["unlock", "--repo", REPO]).verb == "unlock"
    assert parser.parse_args(["lock-status", "--repo", REPO]).verb == "lock-status"
    for verb in ("lock", "unlock", "lock-status"):
        assert not _needs_repo([verb, "--repo", REPO])


def test_the_skills_are_registered_with_risk_and_requires():
    from axiom.extensions.builtins.release.skills import bind
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    for name, risk in (("release.lock", "reversible-write"), ("release.unlock", "reversible-write"), ("release.lock_status", "read")):
        spec = reg.spec(name)
        assert spec is not None, name
        assert spec.risk == risk and spec.requires == ()


# -- the cut flow takes it and lifts it ---------------------------------------------------


class _Mgr:
    def __init__(self, slug: str = REPO) -> None:
        self.slug = slug

    def repo_slug(self) -> str:
        return self.slug


def test_the_cut_flow_takes_the_lock_before_the_bump_and_lifts_it_after_the_tag(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.release import _legacy_release_cli as cut

    forge = FakeForge(ARMED)
    monkeypatch.setattr(cut, "_lock_forge", lambda repo: forge)
    monkeypatch.setattr(cut, "_lock_state_dir", lambda: tmp_path)
    lock = cut._lock_begin(_Mgr(), "release v0.71.0", now=NOW)
    assert lock is not None and forge.disarmed == [7, 9]
    assert RL.load(tmp_path) is not None
    cut._lock_end(_Mgr(), lock, why="tag pushed", now=NOW + timedelta(minutes=3))
    assert forge.rearmed == [(7, "h7", "merge"), (9, "h9", "squash")]
    assert RL.load(tmp_path) is None


def test_the_cut_flow_refuses_when_another_release_holds_the_lock(tmp_path: Path, monkeypatch, capsys):
    from axiom.extensions.builtins.release import _legacy_release_cli as cut

    forge = FakeForge()
    monkeypatch.setattr(cut, "_lock_forge", lambda repo: forge)
    monkeypatch.setattr(cut, "_lock_state_dir", lambda: tmp_path)
    RL.take(tmp_path, REPO, reason="release v0.70.9", held_by="@other", forge=forge, now=NOW)
    with pytest.raises(RL.LockHeld):
        cut._lock_begin(_Mgr(), "release v0.71.0", now=NOW + timedelta(minutes=1))


def test_the_cut_flow_without_a_forge_slug_still_locks_locally(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.release import _legacy_release_cli as cut

    monkeypatch.setattr(cut, "_lock_state_dir", lambda: tmp_path)
    lock = cut._lock_begin(_Mgr(slug=""), "release v0.71.0", now=NOW)
    assert lock is not None and lock.marker == "" and lock.repo == "local"
    cut._lock_end(_Mgr(slug=""), lock, why="tag pushed", now=NOW)
    assert RL.load(tmp_path) is None


def test_repo_slug_is_parsed_from_the_origin_url():
    from axiom.extensions.builtins.release._legacy_release_cli import ReleaseManager

    mgr = ReleaseManager.__new__(ReleaseManager)
    for url in ("git@github.com:example-org/app.git", "https://github.com/example-org/app", "https://github.com/example-org/app.git\n"):
        mgr._git = lambda *a, url=url, **k: url  # type: ignore[method-assign]
        assert mgr.repo_slug() == REPO
    mgr._git = lambda *a, **k: ""  # type: ignore[method-assign]
    assert mgr.repo_slug() == ""


# -- the gh adapter builds the right commands and never a force flag -----------------------


def test_the_gh_adapter_commands(monkeypatch):
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(list(args))
        joined = " ".join(args)
        if "actions/variables/" in joined and "-X" not in args:
            raise RL.ForgeWriteFailed("HTTP 404: Not Found")
        if "pr list" in joined:
            return json.dumps(
                [
                    {"number": 7, "headRefOid": "h7", "autoMergeRequest": {"mergeMethod": "MERGE"}},
                    {"number": 8, "headRefOid": "h8", "autoMergeRequest": None},
                ]
            )
        if "-X POST" in joined and joined.endswith("actions/variables") is False and "variables" in joined and "-f name=" in joined:
            raise RL.ForgeWriteFailed("HTTP 409: already exists")
        return ""

    forge = RL.GhLockForge(REPO, runner=runner)
    assert forge.get_variable(REPO, RL.LOCK_VARIABLE) is None
    assert forge.armed_pulls(REPO) == [{"number": 7, "head_sha": "h7", "merge_method": "MERGE"}]
    forge.disarm(REPO, 7)
    forge.arm(REPO, 7, "h7", "squash")
    forge.comment(REPO, 7, "hello")
    forge.set_variable(REPO, RL.LOCK_VARIABLE, "{}")
    assert forge.delete_variable(REPO, RL.LOCK_VARIABLE) is True
    flat = [" ".join(c) for c in calls]
    assert "pr merge 7 --repo example-org/app --disable-auto" in flat
    assert "pr merge 7 --repo example-org/app --auto --squash --match-head-commit h7" in flat
    assert any(c.startswith("pr comment 7 --repo example-org/app --body-file") for c in flat)
    assert any("-X PATCH" in c and "actions/variables/AXIOM_RELEASE_LOCK" in c for c in flat), "POST failed, so PATCH"
    assert not any("--force" in c or "--admin" in c for c in flat)


# -- the lander workflow honours the lock (#1787 review note, 2026-11-11) -----------
#
# During the 0.70.32 freeze the lander WORKFLOW merged a pull request one minute
# after the bump merged. The workflow's token is not guaranteed to read the
# variables API, and a failed read meant "no lock". The workflow now hands the
# variable to the step, and a variable handed in is the one that is believed.


def test_a_lock_variable_in_the_environment_is_believed_without_asking_the_api(monkeypatch):
    def runner(args: list[str]) -> str:
        raise AssertionError(f"the API must not be asked: {args}")

    monkeypatch.setenv(RL.LOCK_VARIABLE, '{"held": true}')
    assert RL.GhLockForge(REPO, runner=runner).get_variable(REPO, RL.LOCK_VARIABLE) == '{"held": true}'


def test_an_empty_lock_variable_in_the_environment_means_not_held_and_the_api_is_not_asked(monkeypatch):
    def runner(args: list[str]) -> str:
        raise AssertionError(f"the API must not be asked: {args}")

    monkeypatch.setenv(RL.LOCK_VARIABLE, "")
    assert RL.GhLockForge(REPO, runner=runner).get_variable(REPO, RL.LOCK_VARIABLE) is None


def test_without_the_environment_variable_the_api_is_asked(monkeypatch):
    """Negative control: a person's machine still reads the repo marker."""
    monkeypatch.delenv(RL.LOCK_VARIABLE, raising=False)
    asked: list[list[str]] = []

    def runner(args: list[str]) -> str:
        asked.append(list(args))
        return json.dumps({"value": "from-the-api"})

    assert RL.GhLockForge(REPO, runner=runner).get_variable(REPO, RL.LOCK_VARIABLE) == "from-the-api"
    assert asked


def test_writing_or_clearing_the_marker_updates_what_this_process_believes(monkeypatch):
    monkeypatch.setenv(RL.LOCK_VARIABLE, "old")
    forge = RL.GhLockForge(REPO, runner=lambda args: "")
    forge.set_variable(REPO, RL.LOCK_VARIABLE, "new")
    assert forge.get_variable(REPO, RL.LOCK_VARIABLE) == "new"
    assert forge.delete_variable(REPO, RL.LOCK_VARIABLE) is True
    assert forge.get_variable(REPO, RL.LOCK_VARIABLE) is None


def test_the_lander_workflow_hands_the_lock_variable_to_the_step_that_acts():
    workflow = Path(__file__).resolve().parents[6] / ".github" / "workflows" / "rivet-lander.yml"
    text = workflow.read_text(encoding="utf-8")
    # The step that lands: `release land ... --act` (the watch job's --act lands nothing).
    lands = [i for i in range(len(text)) if text.startswith("release land", i) and "--act" in text[i : i + 200]]
    assert len(lands) == 1
    step = text[text.rindex("env:", 0, lands[0]) : lands[0]]
    assert f"{RL.LOCK_VARIABLE}: ${{{{ vars.{RL.LOCK_VARIABLE} }}}}" in step


def test_release_status_returns_the_lock_line_and_prints_nothing_itself(tmp_path: Path, capsys, monkeypatch):
    """The skill is on the MCP surface, where stdout is the protocol channel."""
    from axiom.extensions.builtins.release import skills as S

    monkeypatch.setattr(S, "_status_legacy", lambda params, ctx: S.SkillResult(ok=True))
    ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None)
    result = S.status({"state_dir": str(tmp_path)}, ctx)
    assert result.ok
    assert result.value["release_lock"] == "Release lock: none held here"
    assert capsys.readouterr().out == ""
