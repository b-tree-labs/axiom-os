# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``ci lint``: workflow shapes that spend minutes for nothing.

Warnings, never blockers. Each finding names the file, the rule and the fix.

Rules
-----
``pr-concurrency``
    A pull-request workflow with no ``concurrency`` that cancels in progress.
    Every push then runs the whole workflow again beside the old run.
``fast-stage``
    A long job (timeout at least ``long_job_minutes``, or observed that long)
    that does not wait on a short job. A lint or smoke failure should stop
    the run before the long job starts.
``docs-filter``
    A push or pull-request workflow with a long job, no ``paths``/
    ``paths-ignore`` and no change-detection job. A docs-only change runs the
    full suite. A tag-only push is not checked.
``nightly-matrix``
    A daily schedule that runs a matrix. Most compatibility matrices can run
    weekly.
``strict-without-queue``
    Branch protection requires branches to be up to date and there is no
    merge queue. Each merge makes every open pull request update and re-run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

_PR = ("pull_request", "pull_request_target")
_FILTER_HINTS = ("paths-filter", "changed-files", "changed_files")


@dataclass(frozen=True)
class LintFinding:
    file: str
    rule: str
    message: str
    fix: str
    job: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self) -> str:
        where = f"{self.file}:{self.job}" if self.job else self.file
        return f"[{self.rule}] {where}: {self.message} Fix: {self.fix}"


def _triggers(doc: dict) -> dict[str, Any]:
    # YAML 1.1 reads a bare ``on`` key as True.
    raw = doc.get("on", doc.get(True))
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {str(item): {} for item in raw}
    if isinstance(raw, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}
    return {}


def _cancels(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    value = block.get("cancel-in-progress")
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and ("${{" in value or value.strip().lower() == "true")


def _jobs(doc: dict) -> dict[str, dict]:
    jobs = doc.get("jobs") or {}
    return {str(k): v for k, v in jobs.items() if isinstance(v, dict)} if isinstance(jobs, dict) else {}


def _timeout(job: dict) -> int | None:
    value = job.get("timeout-minutes")
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _needs(job: dict) -> list[str]:
    raw = job.get("needs")
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _matrix_size(job: dict) -> int | None:
    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return 1
    matrix = strategy.get("matrix")
    if matrix is None:
        return 1
    if not isinstance(matrix, dict):
        return None
    size = 1
    seen = False
    for key, value in matrix.items():
        if key in {"include", "exclude"}:
            continue
        if not isinstance(value, list):
            return None
        size *= max(len(value), 1)
        seen = True
    include = matrix.get("include")
    if isinstance(include, list):
        size = size + len(include) if seen else len(include)
    return size


def _daily(cron: str) -> bool:
    fields = cron.split()
    return len(fields) == 5 and fields[2] == "*" and fields[4] == "*"


def lint_workflow(
    name: str,
    text: str,
    *,
    long_job_minutes: int = 20,
    job_minutes: dict[str, float] | None = None,
) -> list[LintFinding]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [LintFinding(name, "parse", f"not valid YAML ({exc.__class__.__name__}).", "Fix the file.")]
    if not isinstance(doc, dict):
        return []
    findings: list[LintFinding] = []
    triggers = _triggers(doc)
    jobs = _jobs(doc)
    observed = job_minutes or {}

    if any(t in triggers for t in _PR):
        top = _cancels(doc.get("concurrency"))
        every_job = bool(jobs) and all(_cancels(job.get("concurrency")) for job in jobs.values())
        if not (top or every_job):
            findings.append(
                LintFinding(
                    name,
                    "pr-concurrency",
                    "pull-request runs are not cancelled when a newer push arrives.",
                    "Add `concurrency: {group: <workflow>-${{ github.ref }}, cancel-in-progress: true}`.",
                )
            )

    def is_long(job_id: str, job: dict) -> bool:
        timeout = _timeout(job)
        seen = observed.get(job_id) or observed.get(str(job.get("name") or ""))
        return (timeout is not None and timeout >= long_job_minutes) or (seen is not None and seen >= long_job_minutes)

    code_push = "push" in triggers and not (
        triggers["push"].get("tags") and not triggers["push"].get("branches")
    )
    if any(t in triggers for t in _PR) or code_push:
        for job_id, job in jobs.items():
            if not is_long(job_id, job):
                continue
            needs = _needs(job)
            short = [n for n in needs if n in jobs and not is_long(n, jobs[n])]
            if not short:
                findings.append(
                    LintFinding(
                        name,
                        "fast-stage",
                        "a long job starts without waiting on a fast check.",
                        "Add a lint or smoke job and `needs:` it, so a quick failure stops the run.",
                        job=job_id,
                    )
                )

        filtered = any("paths" in cfg or "paths-ignore" in cfg for t, cfg in triggers.items() if t in (*_PR, "push"))
        if not any(is_long(job_id, job) for job_id, job in jobs.items()):
            filtered = True
        if not filtered:
            for job_id, job in jobs.items():
                label = f"{job_id} {job.get('name') or ''}".lower()
                if "change" in label or "paths" in label:
                    filtered = True
                for step in job.get("steps") or []:
                    if isinstance(step, dict) and any(h in str(step.get("uses") or "") for h in _FILTER_HINTS):
                        filtered = True
        if not filtered:
            findings.append(
                LintFinding(
                    name,
                    "docs-filter",
                    "a docs-only change runs every job.",
                    "Add `paths-ignore` for docs, or a change-detection job the long jobs gate on.",
                )
            )

    schedule = triggers.get("schedule")
    crons = []
    raw_schedule = doc.get("on", doc.get(True))
    if isinstance(raw_schedule, dict) and isinstance(raw_schedule.get("schedule"), list):
        crons = [str(item.get("cron") or "") for item in raw_schedule["schedule"] if isinstance(item, dict)]
    if schedule is not None and any(_daily(c) for c in crons):
        for job_id, job in jobs.items():
            size = _matrix_size(job)
            if size is not None and size > 1:
                findings.append(
                    LintFinding(
                        name,
                        "nightly-matrix",
                        f"a {size}-leg matrix runs every day.",
                        "Run it weekly (set the day-of-week field) unless a daily signal is needed.",
                        job=job_id,
                    )
                )
    return findings


def lint_protection(protection: dict | None, rules: list[dict] | None, *, branch: str = "main") -> list[LintFinding]:
    if not isinstance(protection, dict):
        return []
    checks = protection.get("required_status_checks") or {}
    strict = isinstance(checks, dict) and bool(checks.get("strict"))
    queued = any(str(rule.get("type") or "") == "merge_queue" for rule in rules or [] if isinstance(rule, dict))
    if strict and not queued:
        return [
            LintFinding(
                f"branch protection ({branch})",
                "strict-without-queue",
                "branches must be up to date and there is no merge queue; every merge re-runs every open pull request.",
                "Enable a merge queue, or turn off the up-to-date requirement.",
            )
        ]
    return []


def lint_dir(
    directory: Path,
    *,
    long_job_minutes: int = 20,
    job_minutes: dict[str, float] | None = None,
) -> list[LintFinding]:
    root = Path(directory)
    if not root.is_dir():
        return []
    findings: list[LintFinding] = []
    for path in sorted([*root.glob("*.yml"), *root.glob("*.yaml")]):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        findings.extend(
            lint_workflow(path.name, text, long_job_minutes=long_job_minutes, job_minutes=job_minutes)
        )
    return findings
