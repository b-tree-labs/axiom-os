# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.repo_state — repo-state awareness in plan derivation.

Per ADR-034 §9 (Phase 1: "Repo-state awareness in plan derivation") and
``working/plan-agent-modes-analysis.md`` §6 (parity gap "Repo-aware planning").

A plan that ignores git state is worse than no plan. ``capture_repo_state``
shells out to git via an injectable runner (so tests stub) and returns a
frozen ``RepoState`` snapshot. ``RepoStateHooks`` is an AskHooks-shaped class
that contributes the rendered summary into the ``domain_context`` layer of
PromptComposer so derived plans reflect current repo reality.
"""

from __future__ import annotations

from collections.abc import Sequence
from subprocess import CompletedProcess

from axiom.agents.pipeline.derive import PlanDerivationHooks
from axiom.agents.pipeline.repo_state import (
    CommitSummary,
    RepoState,
    RepoStateHooks,
    capture_repo_state,
    composite_hooks,
)
from axiom.infra.prompt_composer import PromptComposer
from axiom.memory.ask import AskRequest

# ---------------------------------------------------------------------------
# Fake runner — records calls + returns canned outputs keyed on the leading
# subcommand (e.g., "rev-parse", "status", "log").
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Pretends to be subprocess.run; returns canned CompletedProcess objects."""

    def __init__(self, responses: dict[str, CompletedProcess]):
        self.responses = responses
        self.calls: list[tuple[Sequence[str], str]] = []

    def __call__(self, args: Sequence[str], cwd: str) -> CompletedProcess:
        self.calls.append((tuple(args), cwd))
        # Match on the most-specific git subcommand we recognize.
        # args is like ("git", "rev-parse", "--show-toplevel"); pick a key.
        key = self._key_for(args)
        if key not in self.responses:
            raise KeyError(
                f"FakeRunner missing response for key={key!r}; have {list(self.responses)}"
            )
        return self.responses[key]

    @staticmethod
    def _key_for(args: Sequence[str]) -> str:
        # Use the second + third tokens to disambiguate (e.g. "rev-parse:--show-toplevel").
        if len(args) < 2:
            return ""
        if len(args) >= 3 and args[1] == "rev-parse":
            return f"rev-parse:{args[2]}"
        return args[1]


def _ok(stdout: str) -> CompletedProcess:
    return CompletedProcess(args=(), returncode=0, stdout=stdout, stderr="")


def _err(returncode: int = 128, stderr: str = "fatal: not a git repository") -> CompletedProcess:
    return CompletedProcess(args=(), returncode=returncode, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# capture_repo_state — clean repo
# ---------------------------------------------------------------------------


class TestCaptureRepoStateCleanRepo:
    def test_clean_repo_branch_and_head(self):
        runner = _FakeRunner({
            "rev-parse:--show-toplevel": _ok("/Users/example/repo\n"),
            "rev-parse:--abbrev-ref": _ok("main\n"),
            "rev-parse:HEAD": _ok("abcdef1234567890abcdef1234567890abcdef12\n"),
            "status": _ok(""),  # clean working tree (no porcelain output)
            "log": _ok("abcdef1 first commit|Alice\n"),
        })
        state = capture_repo_state(cwd="/anywhere", recent_n=1, runner=runner)
        assert state.repo_root == "/Users/example/repo"
        assert state.current_branch == "main"
        assert state.head_sha == "abcdef1234567890abcdef1234567890abcdef12"
        assert state.is_dirty is False
        assert state.untracked_files == ()
        assert state.modified_files == ()
        assert state.staged_files == ()
        assert len(state.recent_commits) == 1
        assert state.recent_commits[0].sha == "abcdef1"
        assert state.recent_commits[0].subject == "first commit"
        assert state.recent_commits[0].author == "Alice"


# ---------------------------------------------------------------------------
# capture_repo_state — dirty repo
# ---------------------------------------------------------------------------


class TestCaptureRepoStateDirtyRepo:
    def test_dirty_repo_classifies_files(self):
        # `git status --porcelain` emits two-char status codes:
        #   ?? path        → untracked
        #   " M path"      → modified (working tree)
        #   "M  path"      → staged (index)
        #   "MM path"      → both staged and modified
        porcelain = (
            "?? new_file.py\n"
            " M modified_file.py\n"
            "M  staged_file.py\n"
            "MM both_file.py\n"
        )
        runner = _FakeRunner({
            "rev-parse:--show-toplevel": _ok("/repo\n"),
            "rev-parse:--abbrev-ref": _ok("feature/branch\n"),
            "rev-parse:HEAD": _ok("deadbeef\n"),
            "status": _ok(porcelain),
            "log": _ok(""),
        })
        state = capture_repo_state(cwd="/repo", runner=runner)
        assert state.is_dirty is True
        assert "new_file.py" in state.untracked_files
        assert "modified_file.py" in state.modified_files
        assert "staged_file.py" in state.staged_files
        # "both_file.py" is both staged and modified — appears in both.
        assert "both_file.py" in state.staged_files
        assert "both_file.py" in state.modified_files

    def test_branch_with_slash(self):
        runner = _FakeRunner({
            "rev-parse:--show-toplevel": _ok("/repo\n"),
            "rev-parse:--abbrev-ref": _ok("feature/some-thing\n"),
            "rev-parse:HEAD": _ok("dead\n"),
            "status": _ok(""),
            "log": _ok(""),
        })
        state = capture_repo_state(cwd="/repo", runner=runner)
        assert state.current_branch == "feature/some-thing"


# ---------------------------------------------------------------------------
# capture_repo_state — recent commits
# ---------------------------------------------------------------------------


class TestCaptureRepoStateRecentCommits:
    def test_parses_multiple_commits(self):
        log_out = (
            "abc1234 add planning module|Alice\n"
            "def5678 fix bug in retrieval|Bob\n"
            "0001111 docs: clarify ADR|Alice\n"
        )
        runner = _FakeRunner({
            "rev-parse:--show-toplevel": _ok("/r\n"),
            "rev-parse:--abbrev-ref": _ok("main\n"),
            "rev-parse:HEAD": _ok("abc1234\n"),
            "status": _ok(""),
            "log": _ok(log_out),
        })
        state = capture_repo_state(cwd="/r", recent_n=3, runner=runner)
        assert len(state.recent_commits) == 3
        assert state.recent_commits[0] == CommitSummary(
            sha="abc1234", subject="add planning module", author="Alice",
        )
        assert state.recent_commits[1].subject == "fix bug in retrieval"
        assert state.recent_commits[2].author == "Alice"

    def test_recent_n_passed_to_git_log(self):
        runner = _FakeRunner({
            "rev-parse:--show-toplevel": _ok("/r\n"),
            "rev-parse:--abbrev-ref": _ok("main\n"),
            "rev-parse:HEAD": _ok("a\n"),
            "status": _ok(""),
            "log": _ok(""),
        })
        capture_repo_state(cwd="/r", recent_n=7, runner=runner)
        # Find the 'log' invocation; verify -n7 is in the args.
        log_calls = [c for c in runner.calls if c[0][1] == "log"]
        assert len(log_calls) == 1
        assert any("7" in arg for arg in log_calls[0][0])

    def test_subject_with_pipe_character_preserved(self):
        # Subjects may contain the pipe; only the first two pipes split.
        log_out = "abc1234 a subject | with pipes|Alice\n"
        runner = _FakeRunner({
            "rev-parse:--show-toplevel": _ok("/r\n"),
            "rev-parse:--abbrev-ref": _ok("main\n"),
            "rev-parse:HEAD": _ok("abc\n"),
            "status": _ok(""),
            "log": _ok(log_out),
        })
        state = capture_repo_state(cwd="/r", runner=runner)
        # We split sha (first whitespace) and last "|" for author.
        assert state.recent_commits[0].author == "Alice"
        assert "pipes" in state.recent_commits[0].subject


# ---------------------------------------------------------------------------
# capture_repo_state — outside a git repo
# ---------------------------------------------------------------------------


class TestCaptureRepoStateOutsideGit:
    def test_outside_repo_returns_all_empty(self):
        runner = _FakeRunner({
            "rev-parse:--show-toplevel": _err(returncode=128),
        })
        state = capture_repo_state(cwd="/tmp", runner=runner)
        assert state.repo_root is None
        assert state.current_branch is None
        assert state.head_sha is None
        assert state.is_dirty is False
        assert state.untracked_files == ()
        assert state.modified_files == ()
        assert state.staged_files == ()
        assert state.recent_commits == ()

    def test_outside_repo_short_circuits_other_calls(self):
        runner = _FakeRunner({
            "rev-parse:--show-toplevel": _err(returncode=128),
        })
        capture_repo_state(cwd="/tmp", runner=runner)
        # Should not have invoked status / log / branch lookup beyond the
        # initial short-circuit (FakeRunner would raise KeyError otherwise).
        # We still allow rev-parse:--show-toplevel — that's the probe.
        assert all(
            call[0][:3] == ("git", "rev-parse", "--show-toplevel")
            for call in runner.calls
        )


# ---------------------------------------------------------------------------
# capture_repo_state — runner exception is tolerated
# ---------------------------------------------------------------------------


class TestCaptureRepoStateRunnerErrors:
    def test_runner_raising_oserror_returns_empty(self):
        def boom(args, cwd):
            raise FileNotFoundError("git not on PATH")

        state = capture_repo_state(cwd="/anywhere", runner=boom)
        assert state.repo_root is None
        assert state.current_branch is None


# ---------------------------------------------------------------------------
# RepoState.to_prompt_summary
# ---------------------------------------------------------------------------


class TestRepoStateToPromptSummary:
    def test_summary_contains_branch_and_clean_flag(self):
        state = RepoState(
            repo_root="/r",
            current_branch="main",
            is_dirty=False,
            untracked_files=(),
            modified_files=(),
            staged_files=(),
            recent_commits=(
                CommitSummary(sha="abc1234", subject="first commit", author="Alice"),
            ),
            head_sha="abc1234abc1234",
        )
        text = state.to_prompt_summary()
        assert "main" in text
        assert "clean" in text.lower()
        assert "first commit" in text
        assert "abc1234" in text

    def test_summary_contains_dirty_marker(self):
        state = RepoState(
            repo_root="/r",
            current_branch="feature",
            is_dirty=True,
            untracked_files=("a.py",),
            modified_files=("b.py",),
            staged_files=("c.py",),
            recent_commits=(),
            head_sha="abc",
        )
        text = state.to_prompt_summary()
        assert "feature" in text
        assert "dirty" in text.lower()
        # File counts should appear (1 untracked, 1 modified, 1 staged)
        assert "1" in text

    def test_summary_outside_repo_states_so(self):
        state = RepoState(
            repo_root=None,
            current_branch=None,
            is_dirty=False,
            untracked_files=(),
            modified_files=(),
            staged_files=(),
            recent_commits=(),
            head_sha=None,
        )
        text = state.to_prompt_summary()
        # Must communicate the absence; specific phrasing flexible.
        assert "not a git repo" in text.lower() or "no git" in text.lower()


# ---------------------------------------------------------------------------
# RepoStateHooks.contribute_layers
# ---------------------------------------------------------------------------


def _ask_request() -> AskRequest:
    return AskRequest(
        question="goal text",
        principal_id="@p:c",
        scope_id="test-scope",
        mode="plan_derivation",
    )


class TestRepoStateHooks:
    def test_contributes_to_domain_context_layer(self):
        state = RepoState(
            repo_root="/r",
            current_branch="main",
            is_dirty=False,
            untracked_files=(),
            modified_files=(),
            staged_files=(),
            recent_commits=(
                CommitSummary(sha="abc1234", subject="first commit", author="Alice"),
            ),
            head_sha="abc1234",
        )
        hooks = RepoStateHooks(state)
        composer = PromptComposer()
        hooks.contribute_layers(_ask_request(), composer)

        debug = composer.debug()
        # Should be exactly one contribution; in domain_context.
        repo_contribs = [c for c in debug if c.layer == "domain_context"]
        assert len(repo_contribs) == 1
        contrib = repo_contribs[0]
        assert "main" in contrib.content
        assert "first commit" in contrib.content
        # Contribution must be tagged so other observers can find it.
        assert contrib.source.startswith("repo_state")

    def test_outside_repo_still_contributes_a_short_note(self):
        state = RepoState(
            repo_root=None,
            current_branch=None,
            is_dirty=False,
            untracked_files=(),
            modified_files=(),
            staged_files=(),
            recent_commits=(),
            head_sha=None,
        )
        hooks = RepoStateHooks(state)
        composer = PromptComposer()
        hooks.contribute_layers(_ask_request(), composer)
        debug = composer.debug()
        repo_contribs = [c for c in debug if c.layer == "domain_context"]
        assert len(repo_contribs) == 1
        # We render the "not a git repo" note so the LLM doesn't assume a repo.
        assert "git" in repo_contribs[0].content.lower()


# ---------------------------------------------------------------------------
# composite_hooks — compose multiple AskHooks-shaped objects
# ---------------------------------------------------------------------------


class TestCompositeHooks:
    def test_runs_each_underlying_contribute_layers(self):
        state = RepoState(
            repo_root="/r",
            current_branch="dev-branch",
            is_dirty=False,
            untracked_files=(),
            modified_files=(),
            staged_files=(),
            recent_commits=(
                CommitSummary(sha="abc1234", subject="seed commit", author="A"),
            ),
            head_sha="abc1234",
        )

        plan_hooks = PlanDerivationHooks()
        repo_hooks = RepoStateHooks(state)
        combined = composite_hooks(plan_hooks, repo_hooks)

        composer = PromptComposer()
        combined.contribute_layers(_ask_request(), composer)
        rendered = composer.render_text()

        # Plan-derivation contribution
        assert "plan" in rendered.lower()
        assert "json" in rendered.lower()
        # Repo-state contribution
        assert "dev-branch" in rendered
        assert "seed commit" in rendered

    def test_filter_citations_chains_through_each(self):
        # Compose two simple hooks that each decorate citations.
        class _Tag1:
            def filter_citations(self, request, citations):
                return citations + ["tag1"]

        class _Tag2:
            def filter_citations(self, request, citations):
                return citations + ["tag2"]

        combined = composite_hooks(_Tag1(), _Tag2())
        result = combined.filter_citations(_ask_request(), ["start"])
        assert result == ["start", "tag1", "tag2"]

    def test_pre_llm_short_circuits_on_first_non_none(self):
        sentinel = object()

        class _ReturnsEarly:
            def pre_llm(self, request, composer, citations):
                return sentinel

        class _Boom:
            def pre_llm(self, request, composer, citations):
                raise AssertionError("should not be called after early return")

        combined = composite_hooks(_ReturnsEarly(), _Boom())
        out = combined.pre_llm(_ask_request(), PromptComposer(), [])
        assert out is sentinel

    def test_post_llm_chains_through_each(self):
        class _Append1:
            def post_llm(self, request, raw_response, citations):
                return (raw_response or "") + " [a]"

        class _Append2:
            def post_llm(self, request, raw_response, citations):
                return (raw_response or "") + " [b]"

        combined = composite_hooks(_Append1(), _Append2())
        out = combined.post_llm(_ask_request(), "hello", [])
        assert out == "hello [a] [b]"

    def test_empty_composite_is_noop(self):
        # A composite of zero hooks is a valid object with safe defaults.
        combined = composite_hooks()
        composer = PromptComposer()
        combined.contribute_layers(_ask_request(), composer)
        assert composer.debug() == []
        assert combined.filter_citations(_ask_request(), ["x"]) == ["x"]
        assert combined.pre_llm(_ask_request(), composer, []) is None
        assert combined.post_llm(_ask_request(), "raw", []) is None
