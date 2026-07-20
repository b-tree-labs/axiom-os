# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""EventBus handlers for the Doctor Agent pipeline.

Per spec-hooks.md §7 + §9, these handlers are wired by the platform's
`HookRegistry` from the extension's `[[extension.provides]] kind = "hook"`
manifest blocks; there is no boot-time `bus.subscribe()` ceremony.

Handlers:
- doctor_handler:  cli.* → diagnose + patch (filters to cli.arg_error)
- review_handler:  doctor.patch_ready → independent review
- commit_handler:  review.approved → git commit
- retry_handler:   review.rejected → doctor retries (once)
- aar_handler:     terminal events → After Action Report

Circuit breakers:
- Fingerprint cooldown (5 min)
- Global rate limit (3 patches/hour)
- Lockfile (prevent concurrent runs)
- Recursion detection (skip if traceback contains doctor/)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.bus import get_default_eventbus
from axiom.infra.time_utils import parse_iso

_RUNTIME_DIR = _REPO_ROOT / "runtime"
_DOCTOR_DIR = _RUNTIME_DIR / "doctor"
_LOG_PATH = _RUNTIME_DIR / "logs" / "cli_events.jsonl"
_LOCKFILE = _DOCTOR_DIR / ".lock"
_REPORTS_DIR = _DOCTOR_DIR / "reports"

# --- Circuit breaker constants ---

MAX_PATCHES_PER_HOUR = 3
COOLDOWN_SECONDS = 300  # 5 min between attempts on same fingerprint
LOCK_STALE_SECONDS = 600  # 10 min


# --- Circuit breakers ---

def _recently_processed(fingerprint: str, cooldown: int = COOLDOWN_SECONDS) -> bool:
    """Check if this fingerprint was processed by the doctor recently."""
    if not _LOG_PATH.exists():
        return False
    cutoff = time.time() - cooldown
    try:
        for line in _LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                topic = event.get("topic", "")
                if not topic.startswith("doctor."):
                    continue
                data = event.get("data", {})
                if data.get("fingerprint") != fingerprint:
                    continue
                ts = event.get("timestamp", "")
                if ts:
                    evt_time = parse_iso(ts).timestamp()
                    if evt_time > cutoff:
                        return True
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError:
        pass
    return False


def _hourly_patch_count() -> int:
    """Count doctor.patch_* events in the last hour."""
    if not _LOG_PATH.exists():
        return 0
    cutoff = time.time() - 3600
    count = 0
    try:
        for line in _LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                topic = event.get("topic", "")
                if not topic.startswith("doctor.patch_"):
                    continue
                ts = event.get("timestamp", "")
                if ts:
                    evt_time = parse_iso(ts).timestamp()
                    if evt_time > cutoff:
                        count += 1
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError:
        pass
    return count


def _acquire_lock() -> bool:
    """Try to acquire the doctor lockfile. Returns True if acquired."""
    _DOCTOR_DIR.mkdir(parents=True, exist_ok=True)
    if _LOCKFILE.exists():
        try:
            age = time.time() - _LOCKFILE.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
                return False  # Active lock
            # Stale lock — remove it
            _LOCKFILE.unlink(missing_ok=True)
        except OSError:
            return False

    try:
        _LOCKFILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def _release_lock() -> None:
    """Release the doctor lockfile."""
    _LOCKFILE.unlink(missing_ok=True)


def _rollback_from_backup(files_changed: list[str]) -> None:
    """Roll back edited files from backups."""
    from axiom.extensions.builtins.diagnostics.tools import rollback_file
    for f in files_changed:
        rollback_file(f)


# --- Handlers ---

def doctor_handler(topic: str, data: dict[str, Any]) -> None:
    """Subscribes to 'cli.*' — diagnoses and patches unrecovered errors."""
    # Only handle arg errors
    if topic != "cli.arg_error":
        return

    # Skip recovered errors — recovery strategy already fixed it
    if data.get("recovered"):
        return

    fingerprint = data.get("fingerprint", "")
    if not fingerprint:
        return

    # Recursion guard: skip if the error came from doctor code
    tb = data.get("traceback", "")
    if "axiom/extensions/builtins/diagnostics/" in tb:
        return

    # Cooldown: skip if recently processed
    if _recently_processed(fingerprint):
        return

    bus = get_default_eventbus()

    # Rate limit
    if _hourly_patch_count() >= MAX_PATCHES_PER_HOUR:
        bus.publish("doctor.rate_limited", {
            "fingerprint": fingerprint,
            "reason": f"Rate limit: {MAX_PATCHES_PER_HOUR} patches/hour exceeded",
            **data,
        }, source="diagnostics")
        return

    # Lockfile
    if not _acquire_lock():
        return

    try:
        from axiom.infra.gateway import Gateway
        gateway = Gateway()
        if not gateway.available:
            bus.publish("doctor.llm_unavailable", {
                "fingerprint": fingerprint,
                **data,
            }, source="diagnostics")
            return

        from axiom.extensions.builtins.diagnostics.agent import DoctorAgent
        agent = DoctorAgent(gateway=gateway, bus=bus)
        result = agent.diagnose_and_patch(data)

        if result.tests_passed and result.files_changed:
            bus.publish("doctor.patch_ready", {
                **result.to_dict(),
                "error_signal": data,
                "attempt": 1,
            }, source="diagnostics")
        elif result.status == "llm_unavailable":
            bus.publish("doctor.llm_unavailable", {
                "fingerprint": fingerprint,
                **result.to_dict(),
            }, source="diagnostics")
        else:
            bus.publish("doctor.patch_failed", {
                **result.to_dict(),
                "error_signal": data,
            }, source="diagnostics")
    finally:
        _release_lock()


def review_handler(topic: str, data: dict[str, Any]) -> None:
    """Subscribes to 'doctor.patch_ready' — independent review."""
    bus = get_default_eventbus()
    try:
        from axiom.infra.gateway import Gateway
        gateway = Gateway()
    except Exception:
        # No gateway — auto-approve (tests already passed)
        bus.publish("review.approved", data, source="reviewer")
        return

    if not gateway.available:
        # No LLM for review — auto-approve (tests already passed)
        bus.publish("review.approved", data, source="reviewer")
        return

    from axiom.extensions.builtins.diagnostics.reviewer import Reviewer
    reviewer = Reviewer(gateway=gateway)
    verdict = reviewer.evaluate(data)

    if verdict.approved:
        bus.publish("review.approved", {
            **data,
            "review": verdict.to_dict(),
        }, source="reviewer")
    else:
        bus.publish("review.rejected", {
            **data,
            "review": verdict.to_dict(),
        }, source="reviewer")


def commit_handler(topic: str, data: dict[str, Any]) -> None:
    """Subscribes to 'review.approved' — commits the fix (if not already committed)."""
    bus = get_default_eventbus()
    # The DoctorAgent already attempted git commit in diagnose_and_patch.
    # If it succeeded, we just emit completion. If not, try again.
    commit_sha = data.get("commit_sha", "")
    if commit_sha:
        # Already committed during diagnosis
        bus.publish("doctor.patch_complete", data, source="diagnostics")
        return

    # Try to commit now
    from axiom.extensions.builtins.diagnostics.tools import execute as exec_tool
    files = data.get("files_changed", [])
    fingerprint = data.get("fingerprint", "")
    if not files or not fingerprint:
        bus.publish("doctor.patch_complete", data, source="diagnostics")
        return

    error_signal = data.get("error_signal", {})
    result = exec_tool("git_commit_fix", {
        "fingerprint": fingerprint,
        "files": files,
        "message": (
            f"doctor: fix {error_signal.get('error_type', 'error')} "
            f"in {error_signal.get('command', 'unknown')} [{fingerprint}]"
        ),
    })

    bus.publish("doctor.patch_complete", {
        **data,
        **result,
    }, source="diagnostics")


def retry_handler(topic: str, data: dict[str, Any]) -> None:
    """Subscribes to 'review.rejected' — doctor gets one more attempt."""
    bus = get_default_eventbus()
    attempt = data.get("attempt", 1)
    if attempt >= 2:
        # Already retried — give up
        bus.publish("doctor.patch_failed", data, source="diagnostics")
        return

    # Roll back the previous edit
    _rollback_from_backup(data.get("files_changed", []))

    try:
        from axiom.infra.gateway import Gateway
        gateway = Gateway()
    except Exception:
        bus.publish("doctor.patch_failed", data, source="diagnostics")
        return

    if not gateway.available:
        bus.publish("doctor.patch_failed", data, source="diagnostics")
        return

    # Doctor retries with reviewer feedback
    from axiom.extensions.builtins.diagnostics.agent import DoctorAgent
    feedback = data.get("review", {}).get("feedback", "")
    agent = DoctorAgent(gateway=gateway, bus=bus)
    error_signal = data.get("error_signal", {})
    result = agent.retry_with_feedback(error_signal, feedback)

    if result.tests_passed and result.files_changed:
        bus.publish("doctor.patch_ready", {
            **result.to_dict(),
            "error_signal": error_signal,
            "attempt": 2,
        }, source="diagnostics")
    else:
        bus.publish("doctor.patch_failed", {
            **result.to_dict(),
            "error_signal": error_signal,
        }, source="diagnostics")


# --- After Action Report ---

def aar_handler(topic: str, data: dict[str, Any]) -> None:
    """Produces After Action Report for terminal doctor events."""
    outcome = _outcome_from_topic(topic)
    report = _build_aar(topic, data, outcome)
    fingerprint = data.get("fingerprint", "unknown")
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    # Write markdown file
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _REPORTS_DIR / f"{fingerprint}_{ts}.md"
    try:
        report_path.write_text(report, encoding="utf-8")
    except OSError:
        pass

    summary = _one_line_summary(topic, data, outcome)

    # Emit to bus (logged to JSONL)
    bus = get_default_eventbus()
    bus.publish("doctor.aar", {
        "fingerprint": fingerprint,
        "outcome": outcome,
        "report_path": str(report_path),
        "summary": summary,
    }, source="diagnostics")

    # Print summary if interactive
    if sys.stdout.isatty():
        print(f"\n  Doctor: {summary}", file=sys.stderr)
        print(f"  Report: {report_path}\n", file=sys.stderr)


def _outcome_from_topic(topic: str) -> str:
    mapping = {
        "doctor.patch_complete": "PATCHED",
        "doctor.patch_failed": "FAILED",
        "doctor.rate_limited": "RATE_LIMITED",
        "doctor.llm_unavailable": "LLM_UNAVAILABLE",
    }
    return mapping.get(topic, "UNKNOWN")


def _one_line_summary(topic: str, data: dict[str, Any], outcome: str) -> str:
    fingerprint = data.get("fingerprint", "?")
    files = data.get("files_changed", [])
    tests = "tests passed" if data.get("tests_passed") else "tests failed"

    if outcome == "PATCHED":
        return f"[{fingerprint}] PATCHED {len(files)} file(s), {tests}"
    elif outcome == "FAILED":
        diagnosis = data.get("diagnosis", "")[:80]
        return f"[{fingerprint}] FAILED — {diagnosis or 'no diagnosis'}"
    elif outcome == "RATE_LIMITED":
        return f"[{fingerprint}] SKIPPED (rate limit)"
    elif outcome == "LLM_UNAVAILABLE":
        return f"[{fingerprint}] SKIPPED (LLM unavailable)"
    return f"[{fingerprint}] {outcome}"


def _build_aar(topic: str, data: dict[str, Any], outcome: str) -> str:
    """Build a markdown After Action Report."""
    fingerprint = data.get("fingerprint", "unknown")
    ts = datetime.now(UTC).isoformat()
    error_signal = data.get("error_signal", data)

    lines = [
        "# Doctor After Action Report",
        f"**Fingerprint:** `{fingerprint}`",
        f"**Timestamp:** {ts}",
        f"**Outcome:** {outcome}",
        "",
        "## Error Signal",
        f"- **Command:** `{' '.join(error_signal.get('argv', []))}`",
        f"- **Error:** {error_signal.get('error_type', '?')}: {error_signal.get('error_message', '?')}",
        f"- **Recovered by strategy:** {'Yes' if error_signal.get('recovered') else 'No'}",
    ]

    # Diagnosis
    diagnosis = data.get("diagnosis", "")
    if diagnosis:
        lines.extend(["", "## Diagnosis", "", diagnosis])

    # Changes
    files_changed = data.get("files_changed", [])
    patch_diff = data.get("patch_diff", "")
    if files_changed:
        lines.extend(["", "## Changes Made", ""])
        for f in files_changed:
            lines.append(f"- `{f}`")
        if patch_diff:
            lines.extend(["", "```diff", patch_diff, "```"])

    # Test results
    tests_passed = data.get("tests_passed")
    tests_output = data.get("tests_output", "")
    if tests_passed is not None:
        lines.extend([
            "", "## Test Results",
            f"- **Passed:** {'Yes' if tests_passed else 'No'}",
        ])
        if tests_output:
            output = tests_output[:2000]
            lines.extend(["", "```", output, "```"])

    # Review
    review = data.get("review", {})
    if review:
        lines.extend([
            "", "## Reviewer Verdict",
            f"- **Approved:** {'Yes' if review.get('approved') else 'No'}",
            f"- **Feedback:** {review.get('feedback', 'N/A')}",
        ])
        concerns = review.get("security_concerns", [])
        if concerns:
            lines.append(f"- **Security concerns:** {', '.join(concerns)}")
        issues = review.get("convention_issues", [])
        if issues:
            lines.append(f"- **Convention issues:** {', '.join(issues)}")

    # Git
    branch = data.get("branch_name", "") or data.get("branch", "")
    commit_sha = data.get("commit_sha", "")
    if branch or commit_sha:
        lines.extend([
            "", "## Git",
            f"- **Branch:** `{branch or 'N/A'}`",
            f"- **Commit:** `{commit_sha or 'N/A'}`",
        ])

    lines.extend(["", "---", "*Generated by Neut Doctor Agent*", ""])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connection health handler
# ---------------------------------------------------------------------------

def connection_health_handler(topic: str, data: dict[str, Any]) -> None:
    """Handle connection health events — attempt remediation for failures.

    Topics:
    - connections.unhealthy — Service down. Try ensure_available().
    - connections.throttled — Rate limited. Log for usage tracking.
    - connections.degraded — Warn but don't act.
    - connections.healthy — No action needed.
    """
    conn_name = data.get("connection", "")
    status = data.get("status", "")
    message = data.get("message", "")

    if status == "healthy":
        return  # Nothing to do

    if status == "throttled":
        # Just log — the gateway already handles backoff
        import logging
        logging.getLogger(__name__).info(
            "Connection %s throttled: %s", conn_name, message,
        )
        return

    if status == "unhealthy":
        # Try to auto-fix by ensuring the service is running
        try:
            from axiom.infra.connections import ensure_available
            result = ensure_available(conn_name)
            if result:
                import logging
                logging.getLogger(__name__).info(
                    "TRIAGE auto-recovered connection: %s", conn_name,
                )
            else:
                import logging
                logging.getLogger(__name__).warning(
                    "TRIAGE could not recover connection: %s — %s",
                    conn_name, message,
                )
        except Exception:
            pass
