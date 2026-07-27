# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Layer 3b of the RIVET PR-CI watcher — auto-close stale 🔴 CI-failed
issues on recovery.

The auto-issue-on-failure workflow opens an issue every time CI fails
on a PR's merge ref. Without a complementary auto-closer, those
issues pile up (15+ open at the time of writing). This module closes
matching issues when a recovery flip (failing → passing) lands.

**Safety defenses** (this is the first destructive op in the
watcher pipeline):

  - Title regex anchored on the specific PR's merge ref
  - Author must be the `github-actions` bot
  - State must be `open`
  - Each close is logged with the evidence that matched
  - Dry-run mode via ``RIVET_AUTO_CLOSE_DRY_RUN=1`` env var
  - Hard disable via ``RIVET_AUTO_CLOSE=0``

The match pattern is the exact title shape the auto-opener uses:
``🔴 CI failed on `refs/pull/<N>/merge` (<sha>)``. Issues opened for
main-branch failures (``... on `main` (<sha>)``) are handled by a
separate sweep verb (`axi release close stale --branch main`) — that
needs the main-CI-state monitor to confirm green before closing,
which we don't have plumbed yet.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from .pr_check_watcher import StateFlip

_BOT_LOGINS = ("app/github-actions", "github-actions[bot]", "github-actions")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaleIssue:
    """One issue identified as a stale auto-opened CI-failure.

    `matched_pr` is set when the title matched the PR-merge-ref pattern;
    `matched_tag` is set when it matched the release-tag pattern. They
    are mutually exclusive: zero for irrelevant, populated for the one
    that matched.
    """

    number: int
    title: str
    matched_pr: int = 0
    matched_tag: str = ""


# ---------------------------------------------------------------------------
# gh seam (replaceable in tests)
# ---------------------------------------------------------------------------


def _run_git(args: list[str]) -> int:
    """Invoke `git <args>` and return the exit code. Test seam.

    Returns 1 on missing-binary / timeout (the conservative "not safe"
    answer for an ancestry probe).
    """
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1
    return result.returncode


def _run_gh(args: list[str]) -> str:
    """Invoke `gh <args>` and return stdout. Empty string on failure.

    Test override: monkeypatch `pr_check_auto_closer._run_gh`.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


# ---------------------------------------------------------------------------
# Title-pattern match
# ---------------------------------------------------------------------------


def _match_pr_in_title(title: str) -> int | None:
    """Return the PR number embedded in an auto-issue title, or None.

    Match pattern: ``🔴 CI failed on `refs/pull/<N>/merge` (<sha>)``.
    Tolerates extra characters around the marker — the title shape is
    set by the workflow but might evolve.
    """
    m = re.search(r"refs/pull/(\d+)/merge", title)
    if not m:
        return None
    return int(m.group(1))


def _match_tag_in_title(title: str) -> str | None:
    """Return the release tag embedded in an auto-issue title, or None.

    Match pattern: ``🔴 CI failed on `<tag>` (<sha>)`` where `<tag>`
    looks like a release ref (`v0.14.0`, `v1.2.3-rc1`, etc.). Refuses
    PR-merge refs and `main` to avoid mis-classification.
    """
    m = re.search(r"CI failed on `([^`]+)`", title)
    if not m:
        return None
    ref = m.group(1)
    if ref == "main" or ref.startswith("refs/pull/"):
        return None
    # Release tag heuristic: starts with `v` followed by a digit. We
    # don't try to validate full semver — the safety probe (ancestry
    # against main) is the real gate.
    if not re.match(r"^v\d", ref):
        return None
    return ref


def _is_bot_author(author: dict | None) -> bool:
    if not author:
        return False
    if author.get("is_bot") is True:
        return True
    return author.get("login", "") in _BOT_LOGINS


# ---------------------------------------------------------------------------
# Find + close
# ---------------------------------------------------------------------------


def find_stale_main_issues() -> list[StaleIssue]:
    """List open issues authored by the github-actions bot whose title
    matches ``🔴 CI failed on `main` (<sha>)``."""
    out = _run_gh([
        "issue", "list",
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,author,state",
    ])
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    main_re = re.compile(r"CI failed on `main`")
    results: list[StaleIssue] = []
    for d in data:
        title = d.get("title", "")
        if not main_re.search(title):
            continue
        if not _is_bot_author(d.get("author")):
            continue
        if d.get("state", "").upper() != "OPEN":
            continue
        results.append(StaleIssue(
            number=int(d["number"]), title=title, matched_pr=0,
        ))
    return results


def current_pr_state(*, pr_number: int) -> str:
    """Return the overall state of PR `pr_number`: "passing" / "failing"
    / "pending" / "unknown".

    Goes through this module's own ``_run_gh`` seam so the sweep tests
    only need to mock one place.
    """
    out = _run_gh([
        "pr", "checks", str(pr_number),
        "--json", "name,bucket,state,description,link",
    ])
    if not out.strip():
        return "unknown"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return "unknown"
    if not data:
        return "unknown"
    any_fail = any(d.get("bucket") == "fail" for d in data)
    if any_fail:
        return "failing"
    any_pending = any(d.get("bucket") == "pending" for d in data)
    if any_pending:
        return "pending"
    return "passing"


def current_main_state() -> str:
    """Return the conclusion state of the most-recent GHA run on main."""
    out = _run_gh([
        "run", "list", "--branch", "main", "-L", "1",
        "--json", "headBranch,status,conclusion,url",
    ])
    if not out.strip():
        return "unknown"
    try:
        runs = json.loads(out)
    except json.JSONDecodeError:
        return "unknown"
    if not runs:
        return "unknown"
    run = runs[0]
    status = run.get("status", "")
    conclusion = run.get("conclusion", "")
    if status != "completed":
        return "pending"
    if conclusion == "success":
        return "passing"
    if conclusion in ("failure", "cancelled", "timed_out"):
        return "failing"
    return "unknown"


@dataclass(frozen=True)
class SweepReport:
    """Result of a `sweep_stale()` invocation."""

    closed: list[StaleIssue]
    skipped_reason: str = ""  # non-empty when we refused to close


def sweep_stale(
    *,
    pr_number: int | None = None,
    all_prs: bool = False,
    all_main: bool = False,
    all_tags: bool = False,
    dry_run: bool = False,
) -> SweepReport:
    """Manual stale-issue sweep entry, exposed via `axi release close stale`.

    Three target modes:
      - `pr_number=N` — close stale issues for one PR IFF that PR's
        current CI state is "passing"
      - `all_prs=True` — TODO future enhancement; iterate all open PRs
      - `all_main=True` — close stale main-branch issues IFF main's
        current CI state is "passing"

    `dry_run=True` lists what would close without actually closing.
    """
    if pr_number is not None and not all_main:
        state = current_pr_state(pr_number=pr_number)
        if state != "passing":
            return SweepReport(closed=[], skipped_reason="pr_not_passing")
        stale = find_stale_pr_issues(pr_number=pr_number)
        return _do_close(
            stale, dry_run=dry_run,
            comment=(
                f"Closed by `axi release close stale --pr {pr_number}` — "
                f"PR is currently passing."
            ),
        )

    if all_main:
        state = current_main_state()
        if state != "passing":
            return SweepReport(closed=[], skipped_reason="main_not_passing")
        stale = find_stale_main_issues()
        return _do_close(
            stale, dry_run=dry_run,
            comment="Closed by `axi release close stale --all-main` — "
                    "main CI is currently passing.",
        )

    if all_tags:
        candidates = find_stale_tag_issues()
        if not candidates:
            return SweepReport(closed=[])
        by_tag: dict[str, list[StaleIssue]] = {}
        for issue in candidates:
            by_tag.setdefault(issue.matched_tag, []).append(issue)

        all_closed: list[StaleIssue] = []
        for tag, issues in sorted(by_tag.items()):
            safe, _reason = _tag_safe_to_close_stale(tag=tag)
            if not safe:
                continue
            sub = _do_close(
                issues, dry_run=dry_run,
                comment=(
                    f"Closed by `axi release close stale --all-tags` — "
                    f"tag `{tag}` is reachable from main (release was "
                    f"integrated; codebase has moved on)."
                ),
            )
            all_closed.extend(sub.closed)
        return SweepReport(closed=all_closed)

    if all_prs:
        # Group every PR-ref'd stale issue by PR number, classify each
        # PR's safety, close issues for the safe ones.
        candidates = find_all_pr_ref_issues()
        if not candidates:
            return SweepReport(closed=[])
        by_pr: dict[int, list[StaleIssue]] = {}
        for issue in candidates:
            by_pr.setdefault(issue.matched_pr, []).append(issue)

        all_closed: list[StaleIssue] = []
        for pr_num, issues in sorted(by_pr.items()):
            safe, _reason = _pr_safe_to_close_stale(pr_number=pr_num)
            if not safe:
                # Skip silently — the CLI surface logs the per-PR
                # outcome via the returned report.
                continue
            sub = _do_close(
                issues, dry_run=dry_run,
                comment=(
                    f"Closed by `axi release close stale --all-prs` — "
                    f"PR #{pr_num} state is safe (passing / merged with main "
                    f"green / closed-without-merge)."
                ),
            )
            all_closed.extend(sub.closed)
        return SweepReport(closed=all_closed)

    return SweepReport(closed=[], skipped_reason="no_target")


def _do_close(
    stale: list[StaleIssue], *, dry_run: bool, comment: str,
    action_name: str = "manual_sweep",
) -> SweepReport:
    """Close `stale` under the policy-guard framework.

    The state-precondition checks (PR/main/tag currently safe) live in
    `sweep_stale` proper so the CLI can surface friendly
    skipped_reason strings. The framework here owns rate-limit, pause,
    env-disable, and dry-run.
    """
    if not stale:
        return SweepReport(closed=[])

    from axiom.infra.paths import get_user_state_dir
    from axiom.policy.agent_action_guard import (
        AgentAction, guarded_act,
    )

    action = AgentAction(
        agent="rivet", op_class="github.issue.close",
        name=action_name, candidates=stale,
    )
    decision = guarded_act(
        action,
        do_one=lambda issue: close_stale_issue(
            issue_number=issue.number, comment=comment,
        ),
        state_dir=get_user_state_dir(),
        env_aliases=_ENV_ALIASES,
        dry_run=dry_run,
    )
    if decision.reason == "dry_run":
        return SweepReport(closed=decision.would_proceed)
    if not decision.proceed:
        return SweepReport(
            closed=[],
            skipped_reason=decision.reason or "guard_refused",
        )
    return SweepReport(closed=decision.completed)


def find_stale_tag_issues() -> list[StaleIssue]:
    """List open 🔴 issues whose title matches a release-tag ref
    (`v<X.Y.Z>`) opened by the github-actions bot."""
    out = _run_gh([
        "issue", "list",
        "--state", "open",
        "--limit", "200",
        "--json", "number,title,author,state",
    ])
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    results: list[StaleIssue] = []
    for d in data:
        title = d.get("title", "")
        tag = _match_tag_in_title(title)
        if tag is None:
            continue
        if not _is_bot_author(d.get("author")):
            continue
        if d.get("state", "").upper() != "OPEN":
            continue
        results.append(StaleIssue(
            number=int(d["number"]), title=title, matched_tag=tag,
        ))
    return results


def _tag_safe_to_close_stale(*, tag: str) -> tuple[bool, str]:
    """Tag-ref'd staleness: the tag's commit must be reachable from
    `origin/main`. If yes, the release was integrated into main and
    the codebase has moved on — the original CI failure is stale.

    Refusal cases: tag doesn't exist locally; tag exists but isn't an
    ancestor of main (release attempt was abandoned or branched off).
    """
    rc = _run_git([
        "merge-base", "--is-ancestor",
        f"refs/tags/{tag}", "origin/main",
    ])
    if rc == 0:
        return True, ""
    return False, (
        f"tag {tag} is not reachable from origin/main (release may "
        f"have been abandoned, or local clone is missing the tag)"
    )


def find_all_pr_ref_issues() -> list[StaleIssue]:
    """All open 🔴 issues with a `refs/pull/<N>/merge` ref pattern,
    across every PR. Used by --all-prs to enumerate candidates."""
    out = _run_gh([
        "issue", "list",
        "--state", "open",
        "--limit", "200",
        "--json", "number,title,author,state",
    ])
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    results: list[StaleIssue] = []
    for d in data:
        title = d.get("title", "")
        matched = _match_pr_in_title(title)
        if matched is None:
            continue
        if not _is_bot_author(d.get("author")):
            continue
        if d.get("state", "").upper() != "OPEN":
            continue
        results.append(StaleIssue(
            number=int(d["number"]), title=title, matched_pr=matched,
        ))
    return results


def _pr_safe_to_close_stale(*, pr_number: int) -> tuple[bool, str]:
    """Decide whether stale issues for this PR can be safely closed.

    Returns (safe, reason). See test_pr_check_auto_closer for the
    decision matrix.
    """
    out = _run_gh([
        "pr", "view", str(pr_number),
        "--json", "state,mergedAt",
    ])
    if not out.strip():
        return False, f"could not query PR #{pr_number} state"
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        return False, f"could not parse PR #{pr_number} state"
    state = info.get("state", "").upper()
    if state == "OPEN":
        pr_state = current_pr_state(pr_number=pr_number)
        if pr_state == "passing":
            return True, ""
        return False, f"PR #{pr_number} still failing or pending"
    if state == "MERGED":
        main = current_main_state()
        if main == "passing":
            return True, ""
        return False, f"PR #{pr_number} merged but main is currently {main}"
    if state == "CLOSED":
        # Closed without merging → work abandoned → stale by definition.
        return True, ""
    return False, f"unknown PR state: {state}"


def find_stale_pr_issues(pr_number: int) -> list[StaleIssue]:
    """List open issues authored by the github-actions bot whose title
    matches the given PR's merge ref."""
    out = _run_gh([
        "issue", "list",
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,author,state",
    ])
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    results: list[StaleIssue] = []
    for d in data:
        title = d.get("title", "")
        matched = _match_pr_in_title(title)
        if matched != pr_number:
            continue
        if not _is_bot_author(d.get("author")):
            continue
        if d.get("state", "").upper() != "OPEN":
            continue
        results.append(StaleIssue(
            number=int(d["number"]), title=title, matched_pr=matched,
        ))
    return results


def swap_status_emoji_in_title(title: str) -> str:
    """Replace a leading 🔴 with ✅ so closed issues stop dominating
    list views with the alarming red dot. Idempotent — already-swapped
    titles pass through unchanged. Titles without the marker emoji are
    left alone."""
    if title.startswith("🔴"):
        return "✅" + title[len("🔴"):]
    return title


def close_stale_issue(*, issue_number: int, comment: str) -> bool:
    """Close one issue with a comment and swap the title emoji.

    Returns False on gh failure. The title-edit is best-effort: if the
    title fetch or edit fails, the close still counts (issue IS closed;
    the visual swap is a nice-to-have).
    """
    import json

    # 1. Fetch the current title (so we can compute the swap)
    view_out = _run_gh([
        "issue", "view", str(issue_number),
        "--json", "title",
    ])

    # 2. Close with the audit comment
    _run_gh([
        "issue", "close", str(issue_number),
        "--comment", comment,
    ])

    # 3. Best-effort title swap. Don't fail the close on edit issues.
    try:
        if view_out.strip():
            data = json.loads(view_out)
            current_title = data.get("title", "")
            new_title = swap_status_emoji_in_title(current_title)
            if new_title != current_title:
                _run_gh([
                    "issue", "edit", str(issue_number),
                    "--title", new_title,
                ])
    except (json.JSONDecodeError, Exception):
        pass  # close already happened; title swap is best-effort

    return True


# ---------------------------------------------------------------------------
# Recovery hook
# ---------------------------------------------------------------------------


# Back-compat env-var aliases for the framework guard. Operators have
# `RIVET_AUTO_CLOSE=0` / `RIVET_AUTO_CLOSE_DRY_RUN=1` documented
# already — keep them working alongside the framework's canonical
# `RIVET_GITHUB_ISSUE_CLOSE_DISABLE` / `_DRY_RUN`.
_ENV_ALIASES = {
    "RIVET_AUTO_CLOSE=0": "disable",
    "RIVET_AUTO_CLOSE_DRY_RUN=1": "dry_run",
}


def auto_close_on_recovery(flip: StateFlip) -> list[StaleIssue]:
    """Called by the responder on every flip. No-op unless this is a
    failing → passing recovery for a PR with matching stale issues.

    Returns the list of issues that were closed (or "would close" in
    dry-run). The caller surfaces them via the notification path.

    Routes through `axiom.policy.agent_action_guard.guarded_act` so it
    inherits the rate limit, sentinel-pause, env-disable, and dry-run
    guards uniformly with the manual sweep path.
    """
    if not (flip.to_state == "passing" and flip.from_state == "failing"):
        return []

    from axiom.infra.paths import get_user_state_dir
    from axiom.policy.agent_action_guard import (
        AgentAction, guarded_act, is_action_disabled,
    )

    # Fast path: short-circuit before enumerating candidates when the
    # agent is hard-disabled or paused. Saves a gh call when the
    # operator has explicitly silenced this surface.
    probe_action = AgentAction(
        agent="rivet", op_class="github.issue.close",
        name="auto_close_on_recovery",
    )
    if is_action_disabled(
        probe_action,
        state_dir=get_user_state_dir(),
        env_aliases=_ENV_ALIASES,
    ):
        return []

    stale = find_stale_pr_issues(pr_number=flip.pr_number)
    if not stale:
        return []

    comment = (
        f"Closed automatically by RIVET — PR #{flip.pr_number} "
        f"recovered (CI now passing). {flip.url}\n"
        f"_If this was closed in error, reopen and ping @benbooth._"
    )
    action = AgentAction(
        agent="rivet", op_class="github.issue.close",
        name="auto_close_on_recovery",
        candidates=stale,
        metadata={"flip_url": flip.url, "pr_number": flip.pr_number},
    )
    decision = guarded_act(
        action,
        do_one=lambda issue: close_stale_issue(
            issue_number=issue.number, comment=comment,
        ),
        state_dir=get_user_state_dir(),
        env_aliases=_ENV_ALIASES,
    )
    if decision.reason == "dry_run":
        return decision.would_proceed
    return decision.completed
