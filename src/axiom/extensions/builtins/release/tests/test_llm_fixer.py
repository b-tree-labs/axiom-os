# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tier-1 fixer (#2147): bounded tools, no escape, mechanical first, attempts capped."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.release.fixes import Diagnosis
from axiom.extensions.builtins.release.lander.model import FailingCheck, OpenPull
from axiom.extensions.builtins.release.llm_fixer import (
    LLMFixProvider,
    Workspace,
    load_settings,
    run_fix_loop,
)


@dataclass
class Call:
    tool_id: str
    name: str
    input: dict = field(default_factory=dict)


class Scripted:
    """A model that answers with a fixed list of tool calls, one turn at a time."""

    def __init__(self, turns: list[list[Call]]) -> None:
        self.turns = turns
        self.seen: list[dict] = []

    def complete_with_tools(self, messages, **kwargs):
        self.seen.append(kwargs)
        calls = self.turns.pop(0) if self.turns else []
        return SimpleNamespace(success=True, text="", tool_use=calls, error=None)


def _workspace(tmp_path: Path, **kwargs) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1 \n", encoding="utf-8")
    (tmp_path / "src" / "billing").mkdir()
    (tmp_path / "src" / "billing" / "rates.py").write_text("RATE = 3\n", encoding="utf-8")
    defaults = dict(
        commands={
            "check": "python -c \"import os,sys; sys.exit(1 if 'GH_TOKEN' in os.environ else 0)\""
        },
        protected=lambda rel: rel.startswith("src/billing/"),
        log_reader=lambda name: "E501 src/a.py:1 trailing whitespace",
        check_names=("lint",),
    )
    defaults.update(kwargs)
    return Workspace(tmp_path, **defaults)


def test_the_model_cannot_leave_the_repository(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(PermissionError):
        ws.read_file("../../etc/passwd")
    with pytest.raises(PermissionError):
        ws.write_file("/../outside.txt", "x")


def test_protected_paths_workflows_and_git_are_never_written(tmp_path):
    ws = _workspace(tmp_path)
    for path in ("src/billing/rates.py", ".github/workflows/ci.yml", ".git/config", ".env"):
        with pytest.raises(PermissionError):
            ws.write_file(path, "pwned")
    assert ws.written == set()


def test_commands_are_named_only_and_run_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    ws = _workspace(tmp_path)
    assert ws.run_command("rm -rf /").startswith("unknown command")
    assert ws.run_command("check").startswith("exit 0")  # GH_TOKEN was stripped


def test_a_log_is_read_only_for_a_failing_check(tmp_path):
    ws = _workspace(tmp_path)
    assert "trailing whitespace" in ws.read_check_log("lint")
    assert ws.read_check_log("deploy").startswith("unknown check")


def test_the_loop_edits_then_finishes(tmp_path):
    ws = _workspace(tmp_path)
    model = Scripted(
        [
            [Call("1", "read_check_log", {"check": "lint"})],
            [
                Call(
                    "2",
                    "replace_in_file",
                    {"path": "src/a.py", "old": "x = 1 \n", "new": "x = 1\n"},
                )
            ],
            [Call("3", "finish", {"fixed": True, "summary": "strip trailing whitespace"})],
        ]
    )
    result = run_fix_loop(model, ws, "brief", max_turns=5, routing_tier="export_controlled")
    assert result.fixed and result.turns == 3
    assert (tmp_path / "src" / "a.py").read_text() == "x = 1\n"
    assert ws.written == {"src/a.py"}
    assert all(
        kw["routing_tier"] == "export_controlled" and kw["task"] == "release_fix"
        for kw in model.seen
    )


def test_the_loop_stops_at_the_turn_budget(tmp_path):
    ws = _workspace(tmp_path)
    model = Scripted([[Call(str(i), "list_files", {})] for i in range(10)])
    result = run_fix_loop(model, ws, "brief", max_turns=3, routing_tier="public")
    assert not result.fixed and result.turns == 3


def test_a_blocked_write_is_reported_to_the_model_not_raised(tmp_path):
    ws = _workspace(tmp_path)
    model = Scripted(
        [
            [Call("1", "write_file", {"path": "src/billing/rates.py", "content": "RATE = 0\n"})],
            [Call("2", "finish", {"fixed": False, "summary": "needs a person"})],
        ]
    )
    result = run_fix_loop(model, ws, "brief", max_turns=5, routing_tier="public")
    assert not result.fixed
    assert (tmp_path / "src" / "billing" / "rates.py").read_text() == "RATE = 3\n"


def test_settings_default_to_disabled_and_clamp(tmp_path):
    assert load_settings(None).enabled is False
    path = tmp_path / "lander.toml"
    path.write_text(
        '[fixer]\nenabled = true\nmax_turns = 999\nrouting_tier = "bogus"\n[fixer.commands]\nlint = "ruff check"\n',
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.enabled and settings.max_turns == 40
    assert settings.routing_tier == "export_controlled"  # an unknown tier fails closed
    assert settings.commands == {"lint": "ruff check"}


def _pull(**kwargs) -> OpenPull:
    base = dict(
        number=12,
        title="t",
        head_sha="abcdef1234",
        head_ref="feat/x",
        files=("src/a.py",),
        checks="failing",
        failing_checks=(FailingCheck("lint", ""),),
    )
    base.update(kwargs)
    return OpenPull(**base)


def _provider(tmp_path: Path, *, remote_heads: int, enabled: bool = True) -> LLMFixProvider:
    cfg = tmp_path / "lander.toml"
    cfg.write_text(
        f"[fixer]\nenabled = {str(enabled).lower()}\nmax_attempts = 2\n", encoding="utf-8"
    )

    def git(args, cwd=None):
        if args[0] == "ls-remote":
            return "".join(f"sha\trefs/heads/rivet-fix/12-{i}\n" for i in range(remote_heads))
        raise AssertionError(f"unexpected git call {args}")

    provider = LLMFixProvider(completer=Scripted([]), git=git, gh=lambda args: "")
    provider.bind(
        config=SimpleNamespace(
            source=str(cfg), extra_path_rules=None, replaces_human_paths=False, repos=()
        )
    )
    return provider


def test_after_the_attempt_cap_the_blocker_goes_to_a_person(tmp_path):
    provider = _provider(tmp_path, remote_heads=2)
    fix = provider.draft(Diagnosis("failing_test", "boom", ("src/a.py",)), _pull())
    assert fix.needs_human and fix.reason.startswith("needs-attention")


def test_disabled_fixer_leaves_the_mechanical_decision(tmp_path):
    provider = _provider(tmp_path, remote_heads=0, enabled=False)
    fix = provider.draft(Diagnosis("failing_test", "boom", ("src/a.py",)), _pull())
    assert fix.needs_human and not fix.reason.startswith("needs-attention")


def test_conflicts_and_protected_pulls_never_reach_the_model(tmp_path):
    provider = _provider(tmp_path, remote_heads=0)
    conflict = provider.draft(Diagnosis("merge_conflict", "x", ("src/a.py",)), _pull())
    assert conflict.needs_human and conflict.reason == "merge_conflict"
    protected = provider.draft(
        Diagnosis("failing_test", "x", ("migrations/001.sql",)),
        _pull(files=("migrations/001.sql",)),
    )
    assert protected.needs_human


def test_the_llm_provider_is_a_builtin_name():
    from axiom.extensions.builtins.release.fixes import resolve_fix_provider

    assert resolve_fix_provider("llm").name == "llm"


def test_a_spend_cap_or_model_error_skips_without_using_an_attempt(tmp_path):
    """A 402 (monthly cap reached) or any model error leaves the blocker to a person and
    records no attempt, so a pull request keeps its tries for when the budget resets."""
    cfg = tmp_path / "lander.toml"
    cfg.write_text("[fixer]\nenabled = true\nmax_attempts = 2\n", encoding="utf-8")
    calls: list[list[str]] = []

    def git(args, cwd=None):
        calls.append(list(args))
        if args[0] == "ls-remote":
            return ""
        if args[0] == "worktree" and args[1] == "add":
            Path(args[-2]).mkdir(parents=True, exist_ok=True)
        return ""

    class Broke:
        def complete_with_tools(self, messages, **kwargs):
            return SimpleNamespace(
                success=False, text="", tool_use=[], error="402 credit limit reached"
            )

    provider = LLMFixProvider(completer=Broke(), git=git, gh=lambda args: "")
    provider.bind(
        config=SimpleNamespace(
            source=str(cfg), extra_path_rules=None, replaces_human_paths=False, repos=()
        )
    )
    fix = provider.draft(Diagnosis("lint", "E501", ("src/a.py",)), _pull())
    assert fix.needs_human
    assert "402" in fix.reason
    assert not any(c[0] == "push" for c in calls), calls


@pytest.mark.parametrize(
    "title",
    [
        "fix(ci): batching gate queues same-author PRs oldest-first",
        "chore: fix lint warnings",
        "ci: update the deploy workflow",
        "test: quarantine a flaky test",
        "fix: dependency conflict in lockfile",
        "chore: rebase the branch that fell behind",
    ],
)
def test_a_green_pull_request_is_clear_whatever_its_title_says(title):
    """2026-11-20: a green, mergeable PR titled 'batching gate ...' was diagnosed as a
    batching_gate blocker and never landed. The title is author text, not a symptom."""
    from axiom.extensions.builtins.release.fixes import MechanicalFixProvider

    pull = _pull(title=title, checks="passing", mergeable=True, failing_checks=(), failure_log="")
    assert MechanicalFixProvider().diagnose(pull).kind == "clear"


def test_a_failing_pull_requests_title_does_not_steer_the_diagnosis():
    from axiom.extensions.builtins.release.fixes import MechanicalFixProvider

    pull = _pull(
        title="fix lint",
        checks="failing",
        failure_log="AssertionError in test_rates",
        failing_checks=(FailingCheck("unit tests", ""),),
    )
    assert MechanicalFixProvider().diagnose(pull).kind == "failing_test"


def test_a_failing_batching_check_is_a_batching_blocker_not_a_test_for_the_model():
    """2026-11-20: soilmetrix #3015 failed only its 'PR batching check'. The PR carried no
    failure log, so the diagnosis defaulted to failing_test and the fixer spent both
    attempts on a blocker no code change can clear. The failing checks' names count."""
    from axiom.extensions.builtins.release.fixes import MechanicalFixProvider

    pull = _pull(checks="failing", failure_log="", failing_checks=(FailingCheck("PR batching check", ""),))
    assert MechanicalFixProvider().diagnose(pull).kind == "batching_gate"


def test_a_failing_lint_check_name_reads_as_lint():
    from axiom.extensions.builtins.release.fixes import MechanicalFixProvider

    pull = _pull(checks="failing", failure_log="", failing_checks=(FailingCheck("pre-commit (changed files)", ""),))
    assert MechanicalFixProvider().diagnose(pull).kind == "lint"


def test_the_model_is_never_called_without_a_failing_check(tmp_path):
    """2026-11-20: ci-runners runs no CI on pull requests, so a PR's only checks were
    RIVET's own. With those ignored nothing had failed, yet the fixer spent 16 model turns
    looking for something to fix. No failing check, no model call."""
    calls: list[list[str]] = []

    def git(args, cwd=None):
        calls.append(list(args))
        return ""

    class Watched:
        called = False

        def complete_with_tools(self, messages, **kwargs):
            Watched.called = True
            return SimpleNamespace(success=True, text="", tool_use=[], error=None)

    cfg = tmp_path / "lander.toml"
    cfg.write_text("[fixer]\nenabled = true\n", encoding="utf-8")
    provider = LLMFixProvider(completer=Watched(), git=git, gh=lambda args: "")
    provider.bind(config=SimpleNamespace(source=str(cfg), extra_path_rules=None, replaces_human_paths=False, repos=()))
    fix = provider.draft(Diagnosis("failing_test", "", ("src/a.py",)), _pull(checks="unknown", failing_checks=()))
    assert fix.needs_human
    assert not Watched.called
    assert not any(c[:1] in (["fetch"], ["worktree"], ["push"]) for c in calls), calls
