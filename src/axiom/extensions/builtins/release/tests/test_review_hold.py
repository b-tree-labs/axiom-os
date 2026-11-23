# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A human review request or a changes-requested review blocks landing."""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.release.fixes import Diagnosis, Fix
from axiom.extensions.builtins.release.handoff import WebhookHandoffFixProvider
from axiom.extensions.builtins.release.lander.actor import GitEffects, landing_block, run_actor
from axiom.extensions.builtins.release.lander.config import parse_config
from axiom.extensions.builtins.release.lander.model import FailingCheck, OpenPull
from axiom.extensions.builtins.release.lander.report import build_report
from axiom.extensions.builtins.release.lander.reviews import (
    changes_requested_by,
    pending_human_reviewers,
    review_approvals,
    review_hold,
)
from axiom.extensions.builtins.release.tests.test_webhook_handoff import LabelEffects, MemoryBranches
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore

HEAD = "abc1234567890abc"


def _config(**extra: object) -> object:
    text = (
        'mode = "warn"\n'
        'language = "python"\n'
        "[fix_provider]\n"
        'name = "handoff"\n'
        "[fix_provider.webhook]\n"
        'url_env = "RELEASE_FIX_WEBHOOK_URL"\n'
        'key_env = "RELEASE_FIX_WEBHOOK_KEY"\n'
        "[raci]\n"
        'approvers = ["@ada:local"]\n'
        'auto_allowed = ["merge"]\n'
        "[claims]\n"
        'foreign_labels = ["janna:working"]\n'
    )
    config = parse_config(text)
    if extra:
        from dataclasses import replace

        config = replace(config, **extra)  # type: ignore[arg-type]
    return config


def _pull(**kwargs: object) -> OpenPull:
    base: dict[str, object] = dict(
        number=4,
        title="change",
        head_sha=HEAD,
        head_ref="topic/one",
        files=("src/a.py",),
        checks="passing",
        mergeable=True,
    )
    base.update(kwargs)
    return OpenPull(**base)  # type: ignore[arg-type]


class _Clear:
    def diagnose(self, pull: OpenPull) -> Diagnosis:
        if pull.checks == "passing" and pull.mergeable is not False:
            return Diagnosis("clear", "checks passing", pull.files)
        return Diagnosis("failing_test", "red", pull.files)

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        del pull
        if diagnosis.kind == "clear":
            return Fix(summary="land", kind="land")
        return Fix(summary=diagnosis.kind, needs_human=True, reason="no fixer configured")


def _act(tmp_path: Path, pulls: list[OpenPull], *, provider: object | None = None, environ: dict | None = None):
    return run_actor(
        pulls,
        repo="owner/name",
        config=_config(),  # type: ignore[arg-type]
        state_dir=tmp_path,
        effects=LabelEffects(),
        gate=ApprovalGate(InMemoryActionStore()),
        provider=provider or _Clear(),
        environ=environ if environ is not None else {},
    )


def test_a_team_or_a_bot_request_is_not_a_human_hold():
    pending = pending_human_reviewers(
        [
            {"__typename": "User", "login": "ada"},
            {"__typename": "Team", "name": "Maintainers", "slug": "maintainers"},
            {"__typename": "Bot", "login": "rivet[bot]"},
            {"__typename": "User", "login": "dependabot[bot]"},
        ]
    )
    assert pending == ("ada",)
    changes = changes_requested_by(
        [
            {"author": {"login": "ada"}, "state": "CHANGES_REQUESTED"},
            {"author": {"login": "ada"}, "state": "APPROVED"},
            {"author": {"login": "coderabbit[bot]"}, "state": "CHANGES_REQUESTED"},
        ]
    )
    assert changes == ()
    assert review_hold(pending, changes) == "waiting on ada"


def test_changes_requested_blocks_until_that_person_approves():
    assert changes_requested_by(
        [{"author": {"login": "ada", "__typename": "User"}, "state": "CHANGES_REQUESTED"}]
    ) == ("ada",)
    assert review_hold((), ("ada",)) == "changes requested by ada"
    assert review_hold(("bea",), ("ada",)) == "changes requested by ada; waiting on bea"


def test_a_pending_human_review_is_not_landed(tmp_path: Path):
    effects = LabelEffects()
    result = run_actor(
        [_pull(pending_human_reviewers=("ada",))],
        repo="owner/name",
        config=_config(),  # type: ignore[arg-type]
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=_Clear(),
        environ={},
    )
    assert result.outcomes[0].status == "waiting"
    assert result.outcomes[0].reason == "waiting on ada"
    assert effects.landed == []


def test_changes_requested_is_not_landed_and_a_clear_green_pull_is(tmp_path: Path):
    held = LabelEffects()
    blocked = run_actor(
        [_pull(changes_requested_by=("ada",))],
        repo="owner/name",
        config=_config(),  # type: ignore[arg-type]
        state_dir=tmp_path / "held",
        effects=held,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=_Clear(),
        environ={},
    )
    assert blocked.outcomes[0].reason == "changes requested by ada"
    assert held.landed == []

    landed_effects = LabelEffects()
    landed = run_actor(
        [_pull()],
        repo="owner/name",
        config=_config(),  # type: ignore[arg-type]
        state_dir=tmp_path / "open",
        effects=landed_effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=_Clear(),
        environ={},
    )
    assert landed.outcomes[0].status == "landed"
    assert landed_effects.landed[0][0] == 4
    assert landed_effects.landed[0][2] == "merge"
    from axiom.extensions.builtins.release.lander.actor import merge_command

    assert "--match-head-commit" in merge_command(4, "merge", landed_effects.landed[0][1])


def test_a_protected_path_is_not_auto_landed(tmp_path: Path):
    effects = LabelEffects()
    result = run_actor(
        [_pull(files=("src/pricing/list.py",))],
        repo="owner/name",
        config=_config(),  # type: ignore[arg-type]
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=_Clear(),
        environ={},
    )
    assert result.outcomes[0].status == "escalated"
    assert "pricing" in result.outcomes[0].reason
    assert effects.landed == []
    assert landing_block(_pull(files=("pyproject.toml",)), _config()).startswith("protected:")  # type: ignore[arg-type]
    assert "dependency" in landing_block(_pull(files=("pyproject.toml",)), _config())  # type: ignore[arg-type]
    assert "workflow" in landing_block(_pull(files=(".github/workflows/ci.yml",)), _config())  # type: ignore[arg-type]


def test_dependency_manifests_in_any_directory_are_held_for_a_person():
    """A dependency file is protected wherever it sits, not only at the root.

    Names that merely look related, and a source file, stay routine.
    """
    held = (
        "pyproject.toml",
        "libs/pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "services/dev-requirements.txt",
        "requirements/nested/test-requirements-ci.txt",
        "poetry.lock",
        "uv.lock",
        "Pipfile",
        "Pipfile.lock",
        "setup.py",
        "setup.cfg",
        "constraints.txt",
        "web/package.json",
        "web/package-lock.json",
        "web/npm-shrinkwrap.json",
        "web/yarn.lock",
        "web/pnpm-lock.yaml",
        "web/bun.lockb",
        "infra/Dockerfile",
        "infra/Dockerfile.dev",
        "images/axiom-runtime/Dockerfile",
        "deploy/docker-compose.yml",
        "deploy/docker-compose.prod.yaml",
        "deploy/compose.yml",
        "deploy/compose.override.yaml",
        "services/go.mod",
        "services/go.sum",
        "rust/Cargo.toml",
        "rust/Cargo.lock",
        "ruby/Gemfile",
        "ruby/Gemfile.lock",
    )
    config = _config()
    for path in held:
        reason = landing_block(_pull(files=(path,)), config)  # type: ignore[arg-type]
        assert reason.startswith("protected:"), path
        assert "dependency" in reason, path
    routine = (
        "src/a.py",
        "docs/requirements.md",
        "notes/my-requirements.txt.bak",
        "vendor/notDockerfile",
        "docs/recompose.yml",
        "src/package.js",
        "go.mod.bak",
    )
    for path in routine:
        assert landing_block(_pull(files=(path,)), config) == "", path  # type: ignore[arg-type]


def test_a_dependabot_pull_that_only_touches_dependency_files_is_held(tmp_path: Path):
    effects = LabelEffects()
    result = run_actor(
        [
            _pull(
                head_ref="dependabot/npm_and_yarn/frontend-abc123def4",
                files=("src/web/package.json", "src/web/package-lock.json"),
                checks="passing",
            )
        ],
        repo="owner/name",
        config=_config(),  # type: ignore[arg-type]
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=_Clear(),
        environ={},
    )
    assert result.outcomes[0].status == "escalated"
    assert result.outcomes[0].reason.startswith("protected:")
    assert "dependency" in result.outcomes[0].reason
    assert effects.landed == []


def test_a_missing_fixer_does_not_stop_a_green_landing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RELEASE_FIX_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("RELEASE_FIX_WEBHOOK_KEY", raising=False)
    provider = WebhookHandoffFixProvider(branches=MemoryBranches())
    effects = LabelEffects()
    result = run_actor(
        [_pull(number=5, checks="failing", failure_log="exotic failure"), _pull()],
        repo="owner/name",
        config=_config(),  # type: ignore[arg-type]
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=provider,
        environ={},
    )
    assert result.outcomes[0].status == "escalated"
    assert result.outcomes[0].reason == "no fixer configured"
    assert result.outcomes[1].status == "landed"
    assert effects.landed[0][0] == 4
    assert effects.landed[0][2] == "merge"
    report = build_report(
        [_pull(pending_human_reviewers=("ada",)), _pull(number=9, files=("src/pricing/list.py",))],
        repo="owner/name",
        config=_config(),  # type: ignore[arg-type]
    )
    rendered = " ".join(" ".join(item.reasons) for item in report.needs_human)
    assert "waiting on ada" in rendered
    assert "pricing" in rendered
    assert "no fixer configured" in report.notes


def test_the_authors_own_approval_counts_only_for_self_landers():
    reviews = [
        {"author": {"login": "ada"}, "state": "CHANGES_REQUESTED", "commit": {"oid": HEAD}},
        {"author": {"login": "ada"}, "state": "APPROVED", "commit": {"oid": HEAD}},
        {"author": {"login": "bea"}, "state": "APPROVED", "commit": {"oid": HEAD}},
    ]
    assert review_approvals(reviews, pr_author="@Ada") == (("bea", HEAD),)
    assert changes_requested_by(reviews, pr_author="ada") == ("ada",)
    assert review_approvals(reviews, pr_author="ada", self_landers=("Ada",)) == (
        ("ada", HEAD),
        ("bea", HEAD),
    )
    assert changes_requested_by(reviews, pr_author="ada", self_landers=("@ada",)) == ()
    from axiom.extensions.builtins.release.lander.reviews import review_fields

    pending, changes = review_fields(
        {"author": "ada", "reviewRequests": [{"__typename": "User", "login": "ada"}], "reviews": reviews},
    )
    assert pending == ("ada",)
    assert changes == ("ada",)
    pending, changes = review_fields(
        {"author": "ada", "reviewRequests": [{"__typename": "User", "login": "ada"}], "reviews": reviews},
        self_landers=("benjaminbooth", "ada"),
    )
    assert pending == ()
    assert changes == ()


def test_ineligibility_is_reported_before_a_fixer_and_a_draft_is_not_posted(tmp_path: Path):
    posts: list[dict] = []

    def poster(url: str, body: dict, *, header: str, key: str) -> None:
        posts.append({"url": url, "body": body, "header": header, "key": key})

    provider = WebhookHandoffFixProvider(branches=MemoryBranches(), poster=poster)
    env = {
        "RELEASE_FIX_WEBHOOK_URL": "https://hooks.example/fix",
        "RELEASE_FIX_WEBHOOK_KEY": "sekrit",
    }

    def once(pull: OpenPull, directory: Path):
        return run_actor(
            [pull],
            repo="owner/name",
            config=_config(),  # type: ignore[arg-type]
            state_dir=directory,
            effects=LabelEffects(),
            gate=ApprovalGate(InMemoryActionStore()),
            provider=provider,
            environ=env,
        )

    draft = once(_pull(draft=True, checks="failing", failure_log="exotic failure"), tmp_path / "draft")
    assert draft.outcomes[0].reason == "draft"
    held = once(
        _pull(checks="failing", failure_log="exotic failure", pending_human_reviewers=("ada",)),
        tmp_path / "held",
    )
    assert held.outcomes[0].reason == "waiting on ada"
    protected = once(
        _pull(checks="failing", failure_log="exotic failure", files=("pyproject.toml",)),
        tmp_path / "protected",
    )
    assert protected.outcomes[0].reason.startswith("protected:")
    assert "dependency" in protected.outcomes[0].reason
    assert posts == []

    sent = once(
        _pull(
            checks="failing",
            failure_log="exotic failure",
            failing_checks=(FailingCheck("unit", "https://ci.example/logs/1"),),
        ),
        tmp_path / "sent",
    )
    assert sent.outcomes[0].status == "waiting"
    assert len(posts) == 1
    assert posts[0]["url"] == "https://hooks.example/fix"
    assert posts[0]["header"] == "X-Fix-Key"
    assert posts[0]["key"] == "sekrit"
    body = posts[0]["body"]
    assert body["repo"] == "owner/name"
    assert body["pr"] == 4
    assert body["head_sha"] == HEAD
    assert body["branch"] == "rivet-fix/4-abc1234"
    assert [item["name"] for item in body["failing_checks"]] == ["unit"]


def test_rerun_reads_whether_the_sha_is_green():
    effects = GitEffects(repo="owner/name", token_env="GH_TOKEN")

    def passing(args: list[str]) -> str:
        del args
        return '{"headRefOid": "abc", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}'

    effects._run = passing  # type: ignore[method-assign]
    assert effects.rerun(4, "abc") == "passing"

    def failing(args: list[str]) -> str:
        del args
        return '{"headRefOid": "abc", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}'

    effects._run = failing  # type: ignore[method-assign]
    assert effects.rerun(4, "abc") == "failing"
