# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Drift dashboard for TIDY — surfacing the *industrial common case*.

`worktrees.py` (S1-S4) only catches *evidently* stale work: dir gone, branch
deleted upstream, head merged into main, PR closed. By design, it refuses
to act on "looks old" alone.

That conservatism leaves a gap: the alive-but-drifting branch with no PR
opened. Across a dozen feature branches each 50+ commits behind main, this
gap becomes the dominant source of "merge to main chaos."

This module surfaces those branches with enough context that a human can
pick — per worktree — between {open draft PR, mark paused, archive, rebase
now}. Read-only by design; produces decision packets, never executes.

Design tenets
-------------
- **No bot actions** — every recommendation is a proposal for HITL review.
- **Prose context** — purpose, snapshot, recent commits, related ADRs/PRDs.
  Enough that the human doesn't have to dig before deciding.
- **Cheap to run** — pure git + `gh` queries; no LLM calls; seconds, not minutes.
- **Composable across repos** — pass a list of repo roots to walk a workspace.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from axiom.extensions.builtins.hygiene.worktrees import (
    DEFAULT_BRANCH_CANDIDATES,
    list_worktrees,
)

SEVERITY_ORDER: tuple[str, ...] = ("fresh", "moderate", "stale", "ancient")

# Thresholds — the (behind, age_days) pair determines the bucket.
# Tuned for "review your drift weekly" cadence; tweak as workspace grows.
_FRESH_BEHIND = 10
_FRESH_DAYS = 3
_MODERATE_BEHIND = 30
_MODERATE_DAYS = 14
_STALE_BEHIND = 100
_STALE_DAYS = 60

_ADR_RE = re.compile(r"\bADR-\d{3,4}\b")
_PRD_RE = re.compile(r"\bprd-[a-z0-9][a-z0-9-]+", re.IGNORECASE)

# Author/email patterns that almost certainly indicate test-fixture pollution
# rather than real human work. The 2026-05-04 incident polluted 18 commits on
# feat/twin-build-phase-0 because tests/cli/ext/test_publish.py:_init_git_repo
# wrote user.name=tester to a shared config; the surface that catches that
# class of failure is a hard-coded blocklist of obvious test identities.
_SUSPICIOUS_AUTHOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^tester$", re.IGNORECASE),
    re.compile(r"^test$", re.IGNORECASE),
    re.compile(r"^pytest", re.IGNORECASE),
    re.compile(r"^fixture$", re.IGNORECASE),
    re.compile(r"^anonymous$", re.IGNORECASE),
    re.compile(r"^unknown$", re.IGNORECASE),
)
_SUSPICIOUS_EMAIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^t@t\.test$", re.IGNORECASE),
    re.compile(r"^test@test\.", re.IGNORECASE),
    re.compile(r"^pytest@", re.IGNORECASE),
    re.compile(r"^fixture@", re.IGNORECASE),
    re.compile(r"@example\.(com|org|net)$", re.IGNORECASE),
)


def _is_suspicious_identity(name: str, email: str) -> bool:
    name = name.strip()
    email = email.strip()
    if any(p.match(name) for p in _SUSPICIOUS_AUTHOR_PATTERNS):
        return True
    if any(p.match(email) for p in _SUSPICIOUS_EMAIL_PATTERNS):
        return True
    return False


@dataclass(frozen=True)
class BranchPurpose:
    """Best-effort inference of *why* a branch exists."""

    branch_name: str
    inferred_topic: str
    related_adrs: list[str]
    related_prds: list[str]


@dataclass
class SuspiciousCommit:
    """A commit whose author or committer flags as test-fixture pollution."""

    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    subject: str


@dataclass
class WorktreeDrift:
    """Per-worktree drift snapshot with a HITL decision packet."""

    path: Path
    branch: str
    ahead: int
    behind: int
    unpushed: int
    dirty_files: int
    last_commit_age_days: int
    has_open_pr: bool | None
    pr_state: str | None
    purpose: BranchPurpose
    recent_commit_subjects: list[str]
    top_changed_paths: list[str]
    drift_severity: str
    suggested_action: str
    decision_packet: str
    suspicious_commits: list[SuspiciousCommit] = field(default_factory=list)


# ----- subprocess helpers ----------------------------------------------------


def _run(args: list[str], cwd: Path | None = None, timeout: int = 15) -> tuple[int, str]:
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=safe_git_env(cwd if cwd is not None else Path.cwd()),
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return result.returncode, result.stdout


def _resolve_default_branch(repo: Path) -> str:
    """First of DEFAULT_BRANCH_CANDIDATES that resolves to a real ref."""
    for candidate in DEFAULT_BRANCH_CANDIDATES:
        rc, _ = _run(["git", "rev-parse", "--verify", "--quiet", candidate], cwd=repo)
        if rc == 0:
            return candidate
    return "origin/main"  # fallback; may not exist


def _ahead_behind(wt: Path, default_branch: str) -> tuple[int, int]:
    rc, out = _run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...{default_branch}"],
        cwd=wt,
    )
    if rc != 0 or not out.strip():
        return (0, 0)
    parts = out.strip().split()
    if len(parts) != 2:
        return (0, 0)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (0, 0)


def _unpushed_count(wt: Path) -> int:
    """Commits on HEAD not on the upstream branch (or 0 if no upstream)."""
    rc, _ = _run(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=wt)
    if rc != 0:
        return 0
    rc2, out = _run(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=wt)
    if rc2 != 0:
        return 0
    try:
        return int(out.strip() or 0)
    except ValueError:
        return 0


def _dirty_count(wt: Path) -> int:
    rc, out = _run(["git", "status", "--porcelain"], cwd=wt)
    if rc != 0:
        return 0
    return sum(1 for line in out.splitlines() if line.strip())


def _last_commit_age_days(wt: Path) -> int:
    rc, out = _run(["git", "log", "-1", "--format=%ct"], cwd=wt)
    if rc != 0 or not out.strip():
        return 0
    try:
        ts = int(out.strip())
    except ValueError:
        return 0
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    return max(delta.days, 0)


def _recent_commit_subjects(wt: Path, n: int = 5) -> list[str]:
    rc, out = _run(["git", "log", f"-{n}", "--format=%s"], cwd=wt)
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


def _suspicious_commits(wt: Path, default_branch: str) -> list[SuspiciousCommit]:
    """Scan branch commits vs default for test-fixture-pollution identities.

    Scope: commits between the merge-base with default_branch and HEAD —
    i.e., what THIS branch contributes. Doesn't flag main's history; we
    can't fix what's already merged elsewhere.
    """
    rc, out = _run(
        [
            "git", "log",
            f"{default_branch}..HEAD",
            "--format=%H%x09%an%x09%ae%x09%cn%x09%ce%x09%s",
        ],
        cwd=wt,
        timeout=30,
    )
    if rc != 0:
        return []
    suspicious: list[SuspiciousCommit] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        sha, an, ae, cn, ce, subject = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        if _is_suspicious_identity(an, ae) or _is_suspicious_identity(cn, ce):
            suspicious.append(SuspiciousCommit(
                sha=sha[:10], author_name=an, author_email=ae,
                committer_name=cn, committer_email=ce, subject=subject,
            ))
    return suspicious


def _top_changed_paths(wt: Path, default_branch: str, n: int = 8) -> list[str]:
    """Top-N files (by line-change count) changed on this branch vs default."""
    rc, out = _run(
        ["git", "diff", "--numstat", f"{default_branch}...HEAD"], cwd=wt, timeout=30
    )
    if rc != 0:
        return []
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[2]
        try:
            churn = (int(added) if added != "-" else 0) + (
                int(removed) if removed != "-" else 0
            )
        except ValueError:
            churn = 0
        rows.append((churn, path))
    rows.sort(reverse=True)
    return [p for _, p in rows[:n]]


def _pr_state_for_branch(repo: Path, branch: str) -> tuple[bool | None, str | None]:
    """Return (has_open_pr, pr_state) — (None, None) if `gh` unavailable."""
    if not branch:
        return None, None
    rc, out = _run(
        [
            "gh", "pr", "list", "--head", branch, "--state", "all",
            "--json", "state", "--limit", "1",
        ],
        cwd=repo,
        timeout=15,
    )
    if rc != 0:
        return None, None
    try:
        prs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None, None
    if not prs:
        return False, None
    state = str(prs[0].get("state", "")).upper() or None
    return (state == "OPEN"), state


# ----- Purpose inference ----------------------------------------------------


def infer_purpose(*, repo_path: Path, worktree_path: Path, branch: str) -> BranchPurpose:
    """Best-effort purpose inference from commit messages + branch name."""

    rc, out = _run(["git", "log", "-30", "--format=%s%n%b"], cwd=worktree_path)
    log_text = out if rc == 0 else ""

    adrs = sorted({m for m in _ADR_RE.findall(log_text)})
    prds = sorted({m.lower() for m in _PRD_RE.findall(log_text)})

    # Topic: the branch slug is usually the work's name; commit subjects show up
    # in the "Recent commits" section so we don't double-use them as the title.
    if branch:
        slug = branch.split("/", 1)[-1]
        topic = slug.replace("-", " ").replace("_", " ")
    else:
        # Detached HEAD or unnamed: use first non-merge subject.
        topic = ""
        for line in log_text.splitlines():
            line = line.strip()
            if line and not line.startswith("Merge"):
                topic = line
                break

    return BranchPurpose(
        branch_name=branch,
        inferred_topic=topic,
        related_adrs=adrs,
        related_prds=prds,
    )


# ----- Severity + suggested action ------------------------------------------


def classify_severity(behind: int, last_commit_age_days: int) -> str:
    if behind <= _FRESH_BEHIND and last_commit_age_days <= _FRESH_DAYS:
        return "fresh"
    if behind <= _MODERATE_BEHIND and last_commit_age_days <= _MODERATE_DAYS:
        return "moderate"
    if behind <= _STALE_BEHIND and last_commit_age_days <= _STALE_DAYS:
        return "stale"
    return "ancient"


def suggest_action(r: WorktreeDrift) -> str:
    """Choose one of: continue / monitor-pr / open-pr-or-pause / open-pr-or-archive
    / archive / rebase-recommended / investigate-dirty / quarantine-suspicious-authors.

    Heuristics — tuned to NEVER auto-act; every action requires HITL.
    """

    # Pollution outcomes dominate everything else — if the branch has commits
    # by tester/pytest/etc., neither "open PR" nor "archive" is safe until the
    # history is cleaned. This is what catches the 2026-05-04 incident class.
    if r.suspicious_commits:
        return "quarantine-suspicious-authors"

    # PR-state outcomes dominate.
    if r.pr_state in ("MERGED", "CLOSED"):
        return "archive"
    if r.has_open_pr:
        return "monitor-pr"

    # No PR yet.
    if r.ahead == 0 and r.dirty_files == 0:
        # Nothing to ship + clean → likely already archive-worthy
        return "archive"
    if r.ahead == 0 and r.dirty_files > 0:
        return "investigate-dirty"

    # Has commits + no PR. Drift severity drives the prompt.
    if r.drift_severity == "fresh":
        return "continue"
    if r.drift_severity == "moderate":
        return "open-pr-or-pause"
    if r.drift_severity == "stale":
        if r.behind > _MODERATE_BEHIND:
            return "open-pr-or-pause"
        return "rebase-recommended"
    # ancient
    return "open-pr-or-archive"


# ----- Decision packet (prose) ----------------------------------------------


def render_decision_packet(r: WorktreeDrift) -> str:
    """Prose summary a human reads before deciding PR/pause/archive.

    Format is plain text (no markdown) so it works in CLI, chat, and email
    digests without per-surface adaptation.
    """
    lines = []
    lines.append(f"Branch: {r.branch}")
    lines.append(f"Path:   {r.path}")
    lines.append(f"State:  ahead={r.ahead}, behind={r.behind}, dirty={r.dirty_files}, "
                 f"unpushed={r.unpushed}, last_activity={r.last_commit_age_days}d ago")
    lines.append(f"Severity: {r.drift_severity}")

    pr_line = "PR: none yet"
    if r.pr_state:
        pr_line = f"PR: {r.pr_state}"
    elif r.has_open_pr is None:
        pr_line = "PR: (status unknown — gh unavailable)"
    lines.append(pr_line)

    lines.append("")
    lines.append(f"Purpose (inferred): {r.purpose.inferred_topic}")
    if r.purpose.related_adrs:
        lines.append(f"Related ADRs: {', '.join(r.purpose.related_adrs)}")
    if r.purpose.related_prds:
        lines.append(f"Related PRDs: {', '.join(r.purpose.related_prds)}")

    if r.recent_commit_subjects:
        lines.append("")
        lines.append("Recent commits:")
        for subj in r.recent_commit_subjects:
            lines.append(f"  - {subj}")

    if r.top_changed_paths:
        lines.append("")
        lines.append("Top-changed paths vs main:")
        for p in r.top_changed_paths:
            lines.append(f"  - {p}")

    if r.suspicious_commits:
        lines.append("")
        lines.append(f"⚠ SUSPICIOUS COMMITS ({len(r.suspicious_commits)}) — likely test-fixture pollution:")
        for sc in r.suspicious_commits:
            lines.append(
                f"  - {sc.sha} author={sc.author_name!r} <{sc.author_email}> — {sc.subject}"
            )

    lines.append("")
    lines.append(f"Suggested action: {r.suggested_action}")
    lines.append(_action_rationale(r))
    return "\n".join(lines)


def _action_rationale(r: WorktreeDrift) -> str:
    a = r.suggested_action
    if a == "continue":
        return "  Why: branch is fresh — keep working; revisit when behind ≥ 10 or 3+ days idle."
    if a == "monitor-pr":
        return f"  Why: PR is OPEN ({r.pr_state}); TIDY stands down."
    if a == "open-pr-or-pause":
        return ("  Why: meaningful work present, no PR opened, drift past the moderate "
                "threshold. Open a draft PR for visibility — or explicitly pause this "
                "branch so reviewers know it's parked.")
    if a == "open-pr-or-archive":
        return ("  Why: branch is ancient and still has no PR. Either land it now (open "
                "PR + rebase) or archive it — drifting further only increases the "
                "merge cost.")
    if a == "rebase-recommended":
        return ("  Why: stale but still recoverable. Rebase onto main first; this catches "
                "incompatibility breaks before reviewers hit them.")
    if a == "archive":
        if r.pr_state == "MERGED":
            return "  Why: PR already merged into main — nothing left to land."
        if r.pr_state == "CLOSED":
            return "  Why: PR was closed without merge — branch was abandoned upstream."
        return "  Why: no commits to land and clean working tree — safe to remove."
    if a == "investigate-dirty":
        return ("  Why: uncommitted local changes with no commits ahead. Probably forgotten "
                "work-in-progress; eyeball before pruning.")
    if a == "quarantine-suspicious-authors":
        return ("  Why: one or more commits on this branch are authored by test-fixture "
                "identities (tester / pytest / etc.). DO NOT push, PR, or merge until the "
                "history is rewritten. Likely cause: a test fixture wrote to a shared git "
                "config and bled author identity into real commits. See the recovery "
                "playbook in axiom/extensions/builtins/hygiene/docs/lessons-tester-pollution.md.")
    return ""


# ----- Top-level gather -----------------------------------------------------


def gather_drift(repo: Path) -> list[WorktreeDrift]:
    """Per-worktree drift report for `repo`. Excludes the main worktree."""

    rc, main_path_str = _run(["git", "rev-parse", "--show-toplevel"], cwd=repo)
    main_path = Path(main_path_str.strip()) if rc == 0 else repo.resolve()
    default_branch = _resolve_default_branch(repo)

    reports: list[WorktreeDrift] = []
    for wt in list_worktrees(repo):
        if not wt.path.exists():
            continue  # let worktrees.py's S1 handle missing dirs
        if wt.path.resolve() == main_path.resolve():
            continue
        if not wt.branch:
            continue  # detached HEAD: out of scope for drift

        ahead, behind = _ahead_behind(wt.path, default_branch)
        unpushed = _unpushed_count(wt.path)
        dirty = _dirty_count(wt.path)
        age = _last_commit_age_days(wt.path)
        has_open_pr, pr_state = _pr_state_for_branch(repo, wt.branch)
        purpose = infer_purpose(
            repo_path=repo, worktree_path=wt.path, branch=wt.branch
        )
        subjects = _recent_commit_subjects(wt.path)
        paths = _top_changed_paths(wt.path, default_branch)
        suspicious = _suspicious_commits(wt.path, default_branch)
        severity = classify_severity(behind, age)

        # Build with placeholder action+packet, then fill in.
        r = WorktreeDrift(
            path=wt.path,
            branch=wt.branch,
            ahead=ahead,
            behind=behind,
            unpushed=unpushed,
            dirty_files=dirty,
            last_commit_age_days=age,
            has_open_pr=has_open_pr,
            pr_state=pr_state,
            purpose=purpose,
            recent_commit_subjects=subjects,
            top_changed_paths=paths,
            drift_severity=severity,
            suggested_action="",
            decision_packet="",
            suspicious_commits=suspicious,
        )
        r.suggested_action = suggest_action(r)
        r.decision_packet = render_decision_packet(r)
        reports.append(r)

    return reports


def gather_drift_across_repos(repos: list[Path]) -> dict[Path, list[WorktreeDrift]]:
    """Convenience: walk multiple repos in one pass; useful for workspace digests."""
    return {repo: gather_drift(repo) for repo in repos}


# ----- Compact dashboard rendering ------------------------------------------


def render_dashboard(reports: list[WorktreeDrift]) -> str:
    """One-line-per-worktree summary table for skim-reading."""
    if not reports:
        return "(no drift records)"

    # Sort: ancient → stale → moderate → fresh, within each by behind desc.
    sev_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    ordered = sorted(
        reports, key=lambda r: (-sev_idx[r.drift_severity], -r.behind, r.branch)
    )

    rows = [
        f"{'BRANCH':<48} {'SEV':<8} {'A/B':<10} {'DIRTY':<6} "
        f"{'AGE':<7} {'PR':<8} {'SUSP':<5} {'ACTION':<32}"
    ]
    rows.append("-" * len(rows[0]))
    for r in ordered:
        pr = r.pr_state or ("none" if r.has_open_pr is False else "?")
        susp = str(len(r.suspicious_commits)) if r.suspicious_commits else "·"
        rows.append(
            f"{r.branch[:48]:<48} {r.drift_severity:<8} "
            f"{f'{r.ahead}/{r.behind}':<10} {r.dirty_files:<6} "
            f"{f'{r.last_commit_age_days}d':<7} {pr:<8} "
            f"{susp:<5} {r.suggested_action:<32}"
        )
    return "\n".join(rows)
