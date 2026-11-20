# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Repo-state awareness for plan derivation — ADR-034 §9 / analysis §6.

A plan that ignores git state is worse than no plan. This module supplies:

- ``RepoState`` — a frozen snapshot of branch + working-tree + recent history.
- ``capture_repo_state`` — shells out to git via an injectable runner so tests
  stub deterministically; tolerates non-repo cwd by returning empty state.
- ``RepoStateHooks`` — AskHooks-shaped class that contributes the rendered
  summary into the ``domain_context`` layer of PromptComposer.
- ``composite_hooks`` — small helper composing multiple AskHooks-shaped
  objects (e.g., ``PlanDerivationHooks`` + ``RepoStateHooks``) so extension
  authors don't have to subclass.

No I/O at module-import time; ``capture_repo_state`` is the only path that
shells out, and its runner is injectable.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import Any

# ---------------------------------------------------------------------------
# Data model — frozen snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitSummary:
    """One recent commit's salient fields."""

    sha: str  # short sha (~7 chars; whatever git log emitted)
    subject: str
    author: str


@dataclass(frozen=True)
class RepoState:
    """Snapshot of the repo working tree + recent history.

    All fields are ``None`` / empty when the cwd is not inside a git repo.
    """

    repo_root: str | None
    current_branch: str | None
    is_dirty: bool
    untracked_files: tuple[str, ...]
    modified_files: tuple[str, ...]
    staged_files: tuple[str, ...]
    recent_commits: tuple[CommitSummary, ...]
    head_sha: str | None

    # -- rendering ----------------------------------------------------------

    def to_prompt_summary(self) -> str:
        """Concise multiline text block for the LLM domain_context layer."""
        if self.repo_root is None:
            return (
                "Repo state: not a git repo (planning without git context).\n"
                "Plan steps must not assume branches, commits, or diffs."
            )

        lines: list[str] = ["Current repo state:"]
        lines.append(f"- Repo root: {self.repo_root}")
        lines.append(f"- Branch: {self.current_branch or '(detached HEAD)'}")
        if self.head_sha:
            lines.append(f"- HEAD: {self.head_sha[:12]}")
        if self.is_dirty:
            lines.append(
                "- Working tree: dirty "
                f"({len(self.staged_files)} staged, "
                f"{len(self.modified_files)} modified, "
                f"{len(self.untracked_files)} untracked)"
            )
            if self.staged_files:
                lines.append(
                    "  staged: " + ", ".join(self.staged_files[:8])
                    + ("…" if len(self.staged_files) > 8 else "")
                )
            if self.modified_files:
                lines.append(
                    "  modified: " + ", ".join(self.modified_files[:8])
                    + ("…" if len(self.modified_files) > 8 else "")
                )
            if self.untracked_files:
                lines.append(
                    "  untracked: " + ", ".join(self.untracked_files[:8])
                    + ("…" if len(self.untracked_files) > 8 else "")
                )
        else:
            lines.append("- Working tree: clean")
        if self.recent_commits:
            lines.append(f"- Recent commits ({len(self.recent_commits)}):")
            for commit in self.recent_commits:
                lines.append(
                    f"  {commit.sha} {commit.subject} ({commit.author})"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner protocol + default
# ---------------------------------------------------------------------------


Runner = Callable[[Sequence[str], str], CompletedProcess]


def _default_runner(args: Sequence[str], cwd: str) -> CompletedProcess:
    """Default runner — invokes subprocess.run capturing stdout/stderr.

    Never raises on non-zero exit; callers inspect ``returncode``.
    Strips ``GIT_*`` env vars + sets ``GIT_CEILING_DIRECTORIES=cwd``
    via ``safe_git_env`` so git can't escape ``cwd`` to a parent repo.
    """
    from axiom.infra.git import safe_git_env
    return subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=safe_git_env(cwd),
    )


# ---------------------------------------------------------------------------
# capture_repo_state
# ---------------------------------------------------------------------------


_EMPTY = RepoState(
    repo_root=None,
    current_branch=None,
    is_dirty=False,
    untracked_files=(),
    modified_files=(),
    staged_files=(),
    recent_commits=(),
    head_sha=None,
)


def _safe_run(
    runner: Runner, args: Sequence[str], cwd: str,
) -> CompletedProcess | None:
    """Run, swallowing OSError (git not installed). Return None on OSError."""
    try:
        return runner(args, cwd)
    except (OSError, FileNotFoundError):
        return None


def _parse_porcelain(porcelain: str) -> tuple[
    tuple[str, ...], tuple[str, ...], tuple[str, ...]
]:
    """Parse `git status --porcelain` into (untracked, modified, staged).

    Porcelain format: two columns of status code, then space, then path.
      - "?? path"         → untracked
      - " M path"         → modified in working tree
      - "M  path"         → staged (in index)
      - "MM path"         → both staged and modified
      - "A  path"         → staged add
      - "AD path"         → staged add + deleted in worktree
    A file shows in *both* staged_files and modified_files when both columns
    are non-blank and non-"?".
    """
    untracked: list[str] = []
    modified: list[str] = []
    staged: list[str] = []
    for raw in porcelain.splitlines():
        if len(raw) < 3:
            continue
        x = raw[0]
        y = raw[1]
        path = raw[3:]
        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x.strip() and x != "?":
            staged.append(path)
        if y.strip() and y != "?":
            modified.append(path)
    return tuple(untracked), tuple(modified), tuple(staged)


def _parse_log(log_out: str) -> tuple[CommitSummary, ...]:
    """Parse `git log --pretty=format:%h %s|%an` lines into CommitSummary tuple.

    First whitespace splits sha from rest; LAST '|' splits author. This
    preserves pipe characters in subjects.
    """
    out: list[CommitSummary] = []
    for line in log_out.splitlines():
        if not line.strip():
            continue
        # sha + space + rest
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        sha, rest = parts
        # last "|" separates subject from author
        bar = rest.rfind("|")
        if bar < 0:
            subject, author = rest, ""
        else:
            subject = rest[:bar].strip()
            author = rest[bar + 1:].strip()
        out.append(CommitSummary(sha=sha, subject=subject, author=author))
    return tuple(out)


def capture_repo_state(
    cwd: str = ".",
    *,
    recent_n: int = 5,
    runner: Runner | None = None,
) -> RepoState:
    """Capture current repo state.

    ``runner`` is injectable for tests; defaults to ``subprocess.run``-backed.
    Returns ``RepoState`` with all None/empty fields if the cwd is not a git
    repo or if git is not available.
    """
    run = runner or _default_runner

    # 1. Probe: are we inside a git repo?
    probe = _safe_run(run, ("git", "rev-parse", "--show-toplevel"), cwd)
    if probe is None or probe.returncode != 0:
        return _EMPTY
    repo_root = probe.stdout.strip() or None

    # 2. Branch (may be empty / "HEAD" if detached).
    branch_proc = _safe_run(
        run, ("git", "rev-parse", "--abbrev-ref", "HEAD"), cwd,
    )
    current_branch: str | None = None
    if branch_proc is not None and branch_proc.returncode == 0:
        b = branch_proc.stdout.strip()
        # "HEAD" indicates detached state; surface as None for caller clarity.
        current_branch = None if b == "HEAD" else (b or None)

    # 3. HEAD sha (full).
    head_proc = _safe_run(run, ("git", "rev-parse", "HEAD"), cwd)
    head_sha: str | None = None
    if head_proc is not None and head_proc.returncode == 0:
        head_sha = head_proc.stdout.strip() or None

    # 4. Working tree status.
    status_proc = _safe_run(
        run, ("git", "status", "--porcelain"), cwd,
    )
    untracked: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    staged: tuple[str, ...] = ()
    if status_proc is not None and status_proc.returncode == 0:
        untracked, modified, staged = _parse_porcelain(status_proc.stdout)
    is_dirty = bool(untracked or modified or staged)

    # 5. Recent commits.
    log_proc = _safe_run(
        run,
        (
            "git", "log",
            f"-n{recent_n}",
            "--pretty=format:%h %s|%an",
        ),
        cwd,
    )
    recent: tuple[CommitSummary, ...] = ()
    if log_proc is not None and log_proc.returncode == 0:
        recent = _parse_log(log_proc.stdout)

    return RepoState(
        repo_root=repo_root,
        current_branch=current_branch,
        is_dirty=is_dirty,
        untracked_files=untracked,
        modified_files=modified,
        staged_files=staged,
        recent_commits=recent,
        head_sha=head_sha,
    )


# ---------------------------------------------------------------------------
# RepoStateHooks — AskHooks-shaped contribution into domain_context
# ---------------------------------------------------------------------------


class RepoStateHooks:
    """AskHooks-shaped class that contributes a repo-state summary.

    Composes with ``PlanDerivationHooks`` (use ``composite_hooks(...)``).
    """

    def __init__(self, repo_state: RepoState) -> None:
        self._state = repo_state

    def contribute_layers(self, request, composer) -> None:  # type: ignore[no-untyped-def]
        composer.add(
            layer="domain_context",
            name="repo_state",
            content=self._state.to_prompt_summary(),
            source="repo_state_hooks",
        )

    # The remaining AskHooks methods are no-ops; defined for protocol parity.
    def filter_citations(self, request, citations):  # type: ignore[no-untyped-def]
        return citations

    def pre_llm(self, request, composer, citations):  # type: ignore[no-untyped-def]
        return None

    def post_llm(self, request, raw_response, citations):  # type: ignore[no-untyped-def]
        return None


# ---------------------------------------------------------------------------
# composite_hooks — combine multiple AskHooks-shaped objects
# ---------------------------------------------------------------------------


class _CompositeHooks:
    """Internal: composes a tuple of AskHooks-shaped objects.

    Semantics:
      - ``contribute_layers`` calls every underlying hook in order.
      - ``filter_citations`` chains: each hook receives the previous output.
      - ``pre_llm`` short-circuits on the first non-None return.
      - ``post_llm`` chains the response string through each hook in order.

    Each hook is duck-typed via getattr; missing methods fall back to identity.
    """

    def __init__(self, hooks: tuple[Any, ...]) -> None:
        self._hooks = hooks

    def contribute_layers(self, request, composer) -> None:  # type: ignore[no-untyped-def]
        for hook in self._hooks:
            fn = getattr(hook, "contribute_layers", None)
            if fn is not None:
                fn(request, composer)

    def filter_citations(self, request, citations):  # type: ignore[no-untyped-def]
        out = citations
        for hook in self._hooks:
            fn = getattr(hook, "filter_citations", None)
            if fn is not None:
                out = fn(request, out)
        return out

    def pre_llm(self, request, composer, citations):  # type: ignore[no-untyped-def]
        for hook in self._hooks:
            fn = getattr(hook, "pre_llm", None)
            if fn is None:
                continue
            result = fn(request, composer, citations)
            if result is not None:
                return result
        return None

    def post_llm(self, request, raw_response, citations):  # type: ignore[no-untyped-def]
        # Per AskHooks: post_llm returns Optional[str]; None means
        # "no transform — caller falls back to raw_response". We chain only
        # actual transformations so a composite of zero hooks (or all
        # passthroughs) returns None — matching AskPipeline's expectation.
        out: str | None = None
        current = raw_response
        for hook in self._hooks:
            fn = getattr(hook, "post_llm", None)
            if fn is None:
                continue
            result = fn(request, current, citations)
            if result is not None:
                out = result
                current = result
        return out


def composite_hooks(*hooks: Any) -> _CompositeHooks:
    """Compose multiple AskHooks-shaped objects into one.

    Use to combine plan-derivation hooks with repo-state hooks (and any
    extension-specific hooks) without forcing extension authors to subclass.

        combined = composite_hooks(
            PlanDerivationHooks(),
            RepoStateHooks(capture_repo_state(cwd=...)),
        )
        ask_pipeline.hooks = combined
    """
    return _CompositeHooks(tuple(hooks))
