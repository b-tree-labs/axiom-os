# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI for RIVET — release and CI/CD management.

Bound to the `axi rivet` noun (see this extension's manifest). The
heartbeat subcommand is what launchd / systemd fires every 5 minutes
when RIVET is registered as a daemon agent.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime

from axiom.infra.paths import get_user_state_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi rivet",
        description="RIVET — build, test, tag, and publish releases",
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("status", help="Show build and release status")
    sub.add_parser("mode", help="Show developer vs operator mode")
    sub.add_parser("patterns", help="Show known CI failure patterns")
    sub.add_parser("check", help="Run pre-push prevention checks")
    sub.add_parser(
        "heartbeat",
        help="Proactive tick — check pipelines, match patterns, log signals",
    )

    plan_p = sub.add_parser("plan", help="Show release plan")
    plan_p.add_argument("--format", choices=["human", "json"], default="human")

    sync_p = sub.add_parser(
        "sync",
        help="Fetch + fast-forward local default branches from their remotes",
    )
    sync_p.add_argument(
        "--plan",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Show what would fast-forward without modifying any branch",
    )
    sync_p.add_argument(
        "--root",
        default="",
        help="Workspace root to scan (default: $AXI_WORKSPACE_ROOT or cwd)",
    )
    sync_p.add_argument("--format", choices=["human", "json"], default="human")

    watch_p = sub.add_parser("watch", help="Track a remote routine through to its PR")
    watch_sub = watch_p.add_subparsers(dest="watch_kind")
    routine_p = watch_sub.add_parser("routine", help="Track a CCR cloud routine")
    routine_p.add_argument("trigger_id", help="RemoteTrigger id (e.g. trig_xxx)")
    routine_p.add_argument(
        "--branch", required=True, help="Branch the routine will push"
    )
    routine_p.add_argument(
        "--repo", default="b-tree-labs/axiom-os", help="GitHub repo OWNER/NAME"
    )
    routine_p.add_argument("--note", default="", help="Free-text note")

    sub.add_parser("watched", help="List tracked routines")

    unwatch_p = sub.add_parser("unwatch", help="Stop tracking a routine")
    unwatch_p.add_argument("trigger_id")

    close_p = sub.add_parser(
        "close-stale",
        help="Close stale 🔴-CI-failed issues whose underlying state is now green",
    )
    close_group = close_p.add_mutually_exclusive_group(required=True)
    close_group.add_argument(
        "--pr", type=int, metavar="N",
        help="Close stale issues for PR #N (only if PR is currently passing)",
    )
    close_group.add_argument(
        "--all-prs", action="store_true",
        help="Close stale issues for every PR whose current state is safe "
             "(open+passing, merged + main passing, or closed-without-merge)",
    )
    close_group.add_argument(
        "--all-main", action="store_true",
        help="Close stale issues for main (only if main is currently passing)",
    )
    close_group.add_argument(
        "--all-tags", action="store_true",
        help="Close stale issues for release tags reachable from main",
    )
    close_p.add_argument(
        "--dry-run", action="store_true",
        help="List what would close without actually closing",
    )

    pause_p = sub.add_parser(
        "pause",
        help="Halt RIVET's autonomous destructive ops (sentinel-file kill-switch)",
    )
    pause_p.add_argument(
        "--scope", default="auto-close",
        choices=["auto-close", "all"],
        help="Scope to halt (default: auto-close). 'all' halts every "
             "destructive op for RIVET.",
    )
    pause_p.add_argument(
        "--reason", default="",
        help="Free-text note recorded with the sentinel (e.g., why you paused)",
    )

    resume_p = sub.add_parser(
        "resume",
        help="Lift a pause sentinel set by `axi rivet pause`",
    )
    resume_p.add_argument(
        "--scope", default="auto-close",
        choices=["auto-close", "all"],
        help="Scope to resume (default: auto-close). Must match what was paused.",
    )

    sub.add_parser(
        "paused",
        help="Show currently-paused RIVET op scopes",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.action:
        parser.print_help()
        return 1

    handlers = {
        "status": _cmd_status,
        "mode": _cmd_mode,
        "patterns": _cmd_patterns,
        "check": _cmd_check,
        "plan": _cmd_plan,
        "sync": _cmd_sync,
        "heartbeat": _cmd_heartbeat,
        "watch": _cmd_watch,
        "watched": _cmd_watched,
        "unwatch": _cmd_unwatch,
        "close-stale": _cmd_close_stale,
        "pause": _cmd_pause,
        "resume": _cmd_resume,
        "paused": _cmd_paused,
    }
    return handlers[args.action](args)


# Maps operator-friendly scope aliases to framework op_class strings.
# `--scope all` writes a `pause.all.json` that halts every op for RIVET.
_PAUSE_SCOPE_MAP = {
    "auto-close": "github.issue.close",
    "all": "all",
}


def _cmd_pause(args: argparse.Namespace) -> int:
    """`axi rivet pause` — write sentinel halt."""
    from axiom.policy.agent_action_guard import pause_action

    scope = _PAUSE_SCOPE_MAP[args.scope]
    path = pause_action(
        state_dir=get_user_state_dir(),
        agent="rivet",
        scope=scope,
        by=os.environ.get("USER", "unknown"),
        reason=args.reason or "(no reason given)",
    )
    print(f"RIVET paused: --scope {args.scope}")
    print(f"  sentinel: {path}")
    print(f"  reason:   {args.reason or '(none)'}")
    print(f"  resume with: axi rivet resume --scope {args.scope}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    """`axi rivet resume` — lift a pause sentinel."""
    from axiom.policy.agent_action_guard import resume_action

    scope = _PAUSE_SCOPE_MAP[args.scope]
    removed = resume_action(
        state_dir=get_user_state_dir(),
        agent="rivet",
        scope=scope,
    )
    if removed:
        print(f"RIVET resumed: --scope {args.scope} (sentinel removed)")
    else:
        print(f"RIVET --scope {args.scope}: nothing was paused.")
    return 0


def _cmd_paused(args: argparse.Namespace) -> int:
    """`axi rivet paused` — show active pause sentinels."""
    from axiom.policy.agent_action_guard import list_paused

    paused = list_paused(state_dir=get_user_state_dir(), agent="rivet")
    if not paused:
        print("RIVET: no active pause sentinels.")
        return 0
    # Reverse-map op_class back to friendly scope name.
    inverse = {v: k for k, v in _PAUSE_SCOPE_MAP.items()}
    print(f"RIVET: {len(paused)} active pause sentinel(s):")
    for entry in paused:
        scope = entry.get("scope", "")
        friendly = inverse.get(scope, scope)
        print(
            f"  --scope {friendly}"
            f"  paused_at={entry.get('paused_at')}"
            f"  by={entry.get('paused_by')}"
            f"  reason={entry.get('reason')}"
        )
    return 0


def _auto_sweep_post_merge_stale() -> None:
    """Heartbeat-fired auto-sweep of stale 🔴-CI-failed issues whose
    underlying PR is now in a safe state.

    The recovery-flip auto-closer (`auto_close_on_recovery`) catches
    the live PR → passing transition. But stale 🔴s from PRs whose
    iterations failed mid-cycle (lint typo fixed in the next commit)
    sit open until something triggers a sweep. This makes that
    trigger automatic: on every heartbeat, if main is currently
    passing, run `sweep_stale(all_prs=True)`.

    Safety: `sweep_stale` itself routes closes through
    `axiom.policy.agent_action_guard`, so the volume cap (default 10
    per tick), sentinel pause, and env-disable apply transparently.
    Never raises — heartbeat must complete even if the sweep errors.
    """
    try:
        from .pr_check_auto_closer import current_main_state, sweep_stale
        if current_main_state() != "passing":
            return
        report = sweep_stale(all_prs=True)
        if report.closed:
            print(
                f"[auto-sweep] closed {len(report.closed)} stale 🔴 issue(s) "
                f"for safe PRs"
            )
    except Exception:
        pass  # heartbeat MUST NOT fail because of sweep glitches


def _cmd_close_stale(args: argparse.Namespace) -> int:
    """`axi rivet close-stale` — manual sweep of stale 🔴 CI-failed issues."""
    from .pr_check_auto_closer import sweep_stale

    if args.pr is not None:
        report = sweep_stale(pr_number=args.pr, dry_run=args.dry_run)
        target = f"PR #{args.pr}"
    elif args.all_prs:
        report = sweep_stale(all_prs=True, dry_run=args.dry_run)
        target = "all PRs"
    elif args.all_tags:
        report = sweep_stale(all_tags=True, dry_run=args.dry_run)
        target = "all tags"
    else:
        report = sweep_stale(all_main=True, dry_run=args.dry_run)
        target = "main"

    if report.skipped_reason:
        reason_map = {
            "pr_not_passing": (
                f"{target}: refusing — current CI state is not passing. "
                "Close those issues manually after fixing CI, or pass "
                "--dry-run for a preview."
            ),
            "main_not_passing": (
                f"{target}: refusing — main's latest CI run is not passing."
            ),
            "no_target": "no target specified — pass --pr N or --all-main",
        }
        print(reason_map.get(report.skipped_reason, report.skipped_reason))
        return 1

    if not report.closed:
        print(f"{target}: no stale 🔴-CI-failed issues found.")
        return 0

    verb = "would close (dry-run)" if args.dry_run else "closed"
    print(f"{target}: {verb} {len(report.closed)} issue(s):")
    for issue in report.closed:
        print(f"  #{issue.number}: {issue.title}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from .ci_monitor import get_build_status

    status = get_build_status()

    print("RIVET Build Status")
    print("=" * 50)
    print(f"Mode: {status['mode']['mode']}")
    print()

    for p in status["pipelines"]:
        icon = (
            "pass"
            if p["status"] == "success"
            else "FAIL"
            if p["status"] in ("failed", "failure")
            else "..."
        )
        print(f"  [{icon}] {p['repo']:<15} {p['ref']:<10} {p['status']}")

    if not status["pipelines"]:
        print("  No pipelines detected. Configure gh CLI and/or GITLAB_TOKEN.")

    print()
    if status["all_green"]:
        print("All green. Ready to release.")
    else:
        print("Build issues detected. Run `axi release check` for diagnostics.")

    return 0


def _cmd_mode(args: argparse.Namespace) -> int:
    from .mode import detect_mode

    mode = detect_mode()
    d = mode.to_dict()

    print(f"Mode: {d['mode']}")
    print("\nAxiom (axi-platform):")
    print(f"  Version:  {d['axiom']['version']}")
    print(f"  Source:   {d['axiom']['source']}")
    print(f"  Editable: {d['axiom']['editable']}")
    print("\nDomain consumer:")
    print(f"  Version:  {d['consumer']['version']}")
    print(f"  Source:   {d['consumer']['source']}")
    print(f"  Editable: {d['consumer']['editable']}")

    return 0


def _cmd_patterns(args: argparse.Namespace) -> int:
    from .failure_patterns import FailurePatternDB

    db = FailurePatternDB()
    patterns = db.load()

    print(f"Known CI Failure Patterns ({len(patterns)}):\n")
    for p in patterns:
        print(f"  [{p.source}] {p.name}")
        print(f"    Diagnosis: {p.diagnosis}")
        print(f"    Fix: {p.fix}")
        if p.occurrences:
            print(f"    Seen: {p.occurrences} time(s), last: {p.last_seen[:10]}")
        print()

    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from .failure_patterns import FailurePatternDB

    db = FailurePatternDB()
    checks = db.get_prevention_checks()

    print(f"Running {len(checks)} prevention checks...\n")

    import subprocess

    failed = 0
    for cmd in checks:
        try:
            result = subprocess.run(
                cmd,
                shell=True,  # noqa: S602
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                print(f"  [warn] {cmd}")
                print(f"         {result.stdout.strip()[:100]}")
                failed += 1
            else:
                print(f"  [ok]   {cmd}")
        except Exception as e:
            print(f"  [warn] {cmd}: {e}")

    print(f"\n{len(checks) - failed}/{len(checks)} checks passed.")
    return 0 if failed == 0 else 1


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    """Proactive CI sweep — what launchd fires every 5 minutes.

    1. Poll all known CI pipelines.
    2. For each red pipeline, try the learned-pattern DB.
    3. Persist a structured signal entry to ~/.axi/agents/rivet/heartbeat.jsonl.
    4. Surface unmatched failures with a `next_route` of "bonsai" so the
       Bonsai-first / Claude-fallback router (per RIVET training protocol)
       can pick them up out-of-band.

    Exit code: 0 if green, 2 if any pipeline is red. Non-zero exits make
    launchd / `axi agents logs` light up loudly.
    """
    from .ci_monitor import check_pipelines
    from .failure_patterns import FailurePatternDB
    from .pr_check_responder import handle_flips
    from .pr_check_watcher import watch_user_prs
    from .routine_monitor import poll_routines

    pipelines = check_pipelines()
    db = FailurePatternDB()
    routine_transitions = poll_routines(get_user_state_dir())
    # PR-scoped CI watch — fills the gap that let PR-level billing /
    # runner failures fly under RIVET's top-level-run-only radar.
    pr_flips = watch_user_prs(state_dir=get_user_state_dir())

    # Local-main sync — fetch every workspace repo and fast-forward the
    # clean, non-diverged default branches (the operator-chosen default
    # autonomy). Non-destructive: diverged / dirty / ahead branches are
    # surfaced in the signal below, never touched. Resilient by contract —
    # a sync glitch (offline remote, locked repo) must never sink the
    # heartbeat, so it degrades to an empty result.
    try:
        from .local_sync import sync_workspace

        sync_results = sync_workspace(apply=True)
    except Exception:
        sync_results = []

    # Cross-repo trunk watch — the push-into-radar for repos the operator
    # explicitly enrolls in ~/.axi/agents/rivet/watched-repos.toml. Closed
    # the multi-day silent-red incident in late May 2026 where a
    # downstream consumer repo's main stayed red unobserved. Resilient by
    # contract: any error degrades to no findings, never sinks heartbeat.
    cross_repo_findings: list[dict] = []
    try:
        from .cross_repo_pr_watch import (
            cross_repo_pr_watch as _xrpw,
            default_config_path,
            load_watched_repos,
        )

        _cfg = default_config_path()
        _targets = load_watched_repos(_cfg)
        if _targets:
            _findings, _ = _xrpw(_targets, state_dir=get_user_state_dir())
            cross_repo_findings = [
                {
                    "repo": f.repo,
                    "ref": f.ref,
                    "severity": f.severity,
                    "detail": f.detail,
                    "url": f.run_url,
                }
                for f in _findings
            ]
    except Exception:
        pass

    matched: list[dict] = []
    unmatched: list[dict] = []
    for p in pipelines:
        if p.status not in ("failed", "failure"):
            continue
        hits = db.match_failure(p.failure_reason or "") if p.failure_reason else []
        if hits:
            matched.append(
                {
                    "repo": p.repo,
                    "ref": p.ref,
                    "url": p.url,
                    "failure_reason": p.failure_reason,
                    "pattern": hits[0].name,
                    "diagnosis": hits[0].diagnosis,
                    "fix": hits[0].fix,
                }
            )
        else:
            entry_dict = {
                "repo": p.repo,
                "ref": p.ref,
                "url": p.url,
                "failure_reason": p.failure_reason,
                "next_route": "bonsai",
            }
            # Attach an LLM-generated narrative for unmatched failures so
            # operators see a candidate diagnosis alongside the raw entry.
            try:
                from .failure_narrative import narrative_for_failure

                narr = narrative_for_failure(entry_dict)
                if narr is not None:
                    entry_dict["narrative"] = narr
            except Exception:
                pass
            unmatched.append(entry_dict)

    all_green = all(p.status == "success" for p in pipelines) if pipelines else True

    entry = {
        "agent": "rivet",
        "ts": datetime.now(UTC).isoformat(),
        "all_green": all_green,
        "pipelines": [p.to_dict() for p in pipelines],
        "matched_failures": matched,
        "unmatched_failures": unmatched,
        "routine_transitions": routine_transitions,
        "pr_check_flips": [
            {
                "pr_number": f.pr_number,
                "title": f.title,
                "url": f.url,
                "head_branch": f.head_branch,
                "from_state": f.from_state,
                "to_state": f.to_state,
                "classification": f.classification,
                "failing_jobs": [
                    {"name": r.name, "description": r.description, "link": r.link}
                    for r in f.failing_rows
                ],
            }
            for f in pr_flips
        ],
    }

    # Route flips through the responder: it sends notifications via the
    # publishing layer (stdout + macOS notification center via pync when
    # available) and writes per-PR failure-report markdown under
    # ~/.axi/agents/rivet/reports/ for code-classified failures.
    try:
        from axiom.extensions.builtins.publishing.providers.notification.terminal import (
            TerminalNotificationProvider,
        )
        sink = TerminalNotificationProvider()
    except Exception:
        # Publishing not available — fall back to a stdout-only sink that
        # implements the same `send` shape. Keeps the heartbeat resilient
        # to publisher-side regressions.
        class _StdoutSink:
            def send(self, recipients, subject, body, urgency="normal"):
                indicator = {"high": "!", "normal": "-", "low": "."}.get(urgency, "-")
                print(f"[{indicator}] {subject}")
                for line in body.splitlines():
                    print(f"    {line}")
                return True
        sink = _StdoutSink()
    reports = handle_flips(pr_flips, state_dir=get_user_state_dir(), sink=sink)
    entry["failure_reports"] = [
        {"pr_number": r.pr_number, "path": str(r.path)} for r in reports
    ]

    # Record the local-sync outcome on the signal and print a terse summary:
    # what advanced, and what needs a human (diverged / dirty / failed).
    entry["local_sync"] = [r.to_dict() for r in sync_results]
    entry["cross_repo_findings"] = cross_repo_findings
    _attention = {"diverged", "behind_dirty", "fetch_failed", "ff_failed", "error"}
    forwarded = [r for r in sync_results if r.action == "fast_forwarded"]
    surfaced = [r for r in sync_results if r.action in _attention]
    if forwarded:
        print(
            f"[sync] fast-forwarded {len(forwarded)} default branch(es) "
            "from their remotes"
        )
    for r in surfaced:
        host = f" ({r.host})" if r.host else ""
        print(f"[sync] {r.repo}{host}: {r.action} — needs attention")

    log_dir = get_user_state_dir() / "agents" / "rivet"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "heartbeat.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    # Auto-sweep stale 🔴-CI-failed issues whose PR is now safe. Catches
    # the mid-PR-iteration noise the recovery-flip auto-closer misses
    # (lint fail → fix in next commit; the 🔴 from the failed commit
    # sits open until something triggers a sweep). Guarded by main-CI
    # state probe + agent_action_guard rate limit; safe by construction.
    _auto_sweep_post_merge_stale()

    # Any PR check that just flipped to failing also escalates exit to 2
    # so `axi agents logs` / launchd surface the heartbeat as "noticed
    # something." Recoveries (failing → passing) don't.
    pr_failing_flip = any(f.to_state == "failing" for f in pr_flips)
    if all_green and not pr_failing_flip:
        return 0
    return 2


def _cmd_watch(args: argparse.Namespace) -> int:
    if getattr(args, "watch_kind", None) != "routine":
        print("usage: axi rivet watch routine <trigger_id> --branch <name>")
        return 1
    from .routine_monitor import track

    state_dir = get_user_state_dir()
    r = track(
        state_dir,
        trigger_id=args.trigger_id,
        branch=args.branch,
        repo=args.repo,
        note=args.note,
    )
    print(f"watching routine {r.trigger_id} → {r.repo} branch {r.branch}")
    if r.note:
        print(f"  note: {r.note}")
    return 0


def _cmd_watched(args: argparse.Namespace) -> int:
    from .routine_monitor import load_tracked

    routines = load_tracked(get_user_state_dir())
    if not routines:
        print("No routines being watched.")
        return 0
    print(f"Watched routines ({len(routines)}):\n")
    for r in routines:
        pr = f"PR #{r.pr_number}" if r.pr_number else "—"
        print(f"  [{r.state:<11}] {r.trigger_id}  {r.branch}  ({pr})")
        if r.note:
            print(f"               note: {r.note}")
    return 0


def _cmd_unwatch(args: argparse.Namespace) -> int:
    from .routine_monitor import untrack

    if untrack(get_user_state_dir(), args.trigger_id):
        print(f"unwatched {args.trigger_id}")
        return 0
    print(f"no tracked routine with id {args.trigger_id}")
    return 1


# Sync actions that mean "a human should look": local work diverges from
# the remote, or the fetch/fast-forward could not complete.
_SYNC_ATTENTION = {"diverged", "behind_dirty", "fetch_failed", "ff_failed", "error"}

# One-line, human-readable gloss per action for the `sync` summary.
_SYNC_GLOSS = {
    "up_to_date": "up to date",
    "fast_forwarded": "fast-forwarded",
    "behind": "behind (would fast-forward)",
    "behind_dirty": "behind, but working tree is dirty — left untouched",
    "diverged": "DIVERGED from remote — resolve manually (rebase/merge/PR)",
    "ahead": "ahead of remote (unpushed local commits)",
    "no_remote": "no 'origin' remote configured",
    "no_default_branch": "could not resolve a remote default branch",
    "fetch_failed": "fetch failed (offline or needs credentials)",
    "ff_failed": "fast-forward failed",
    "missing_local": "no local default branch (would create a tracking branch)",
    "created": "created local tracking branch",
    "error": "error during sync",
}


def _cmd_sync(args: argparse.Namespace) -> int:
    """`axi rivet sync` — fetch + fast-forward local default branches.

    Non-destructive: only clean, strictly-behind default branches are
    fast-forwarded. Diverged / dirty / ahead branches are surfaced and
    left for the operator. ``--plan`` (alias ``--dry-run``) assesses
    without modifying anything. Exit 2 when any repo needs attention.
    """
    from .local_sync import sync_workspace

    apply = not args.dry_run
    results = sync_workspace(args.root or None, apply=apply)

    if getattr(args, "format", "human") == "json":
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        if not results:
            print(
                "No git repos found to sync. Set $AXI_WORKSPACE_ROOT or run "
                "from your workspace directory."
            )
        else:
            verb = "Planning sync" if args.dry_run else "Syncing"
            print(f"{verb} for {len(results)} repo(s):\n")
            for r in sorted(results, key=lambda x: x.repo.lower()):
                gloss = _SYNC_GLOSS.get(r.action, r.action)
                counts = ""
                if r.ahead or r.behind:
                    counts = f"  [+{r.ahead}/-{r.behind}]"
                host = f"  {{{r.host}}}" if r.host else ""
                print(f"  {r.repo:<28} {gloss}{counts}{host}")
                if r.detail and r.action in _SYNC_ATTENTION:
                    print(f"  {'':<28}   {r.detail}")

    return 2 if any(r.action in _SYNC_ATTENTION for r in results) else 0


def _cmd_plan(args: argparse.Namespace) -> int:
    from axiom.vega.federation.release_plan import ReleasePlanService

    svc = ReleasePlanService()
    milestones = svc.list_milestones()

    if getattr(args, "format", "human") == "json":
        print(json.dumps([m.to_dict() for m in milestones], indent=2))
    elif not milestones:
        print("No release milestones. Run `axi release plan --add <version>` to create one.")
    else:
        print("Release Plan:\n")
        for m in milestones:
            status_label = {"planned": "PLAN", "tagged": "TAG", "announced": "ANN"}.get(
                m.status, "?"
            )
            tests = sum(f.test_count for f in m.features)
            print(f'  [{status_label}] v{m.version} "{m.codename}"')
            print(f"    Target: {m.target_date}  Status: {m.status}  Tests: {tests}")
            for f in m.features:
                print(f"      - {f.name} ({f.test_count} tests)")
            print()

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
