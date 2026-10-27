# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CI-health thresholds, read from the ``[ci_health]`` table of the lander TOML.

Every number RIVET acts on lives here as data. Nothing in the code names an
owner, an organization, or a repository.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused


@dataclass(frozen=True)
class CIHealthConfig:
    #: Billing owner. Empty means the owner half of the first watched repo.
    org: str = ""
    #: Repositories whose runs are read. Empty means the lander's watch list.
    repos: tuple[str, ...] = ()
    #: Monthly Actions budget in dollars. 0 means read the forge's own budget.
    budget_dollars: float = 0.0
    #: "net" (what is billed after included minutes) or "gross".
    budget_basis: str = "net"
    warn_pct: float = 70.0
    stop_pct: float = 90.0
    #: Alert when the month's projection exceeds last month by this multiple.
    run_rate_multiple: float = 1.5
    #: Alert when one waste signal is more than this share of window minutes.
    waste_share: float = 0.25
    window_days: int = 14
    max_run_pages: int = 10
    chronic_red_runs: int = 3
    #: Workflow name or path substrings that are never cancelled.
    release_workflows: tuple[str, ...] = ("release", "publish", "tag", "deploy")
    #: A head branch that matches this is a tag, and is never cancelled.
    tag_pattern: str = r"^v?\d+(\.\d+)+([.-].*)?$"
    default_branch: str = "main"
    max_cancels: int = 20
    #: Workflows whose runs carry a selection job. Empty means the workflow
    #: with the most pull-request minutes in the window.
    selection_workflows: tuple[str, ...] = ()
    #: Jobs whose logs carry the selection marker.
    selection_jobs: tuple[str, ...] = ("unit", "test")
    #: How many recent selection-job logs one sweep reads.
    selection_log_sample: int = 10
    #: Reasons for a full suite that are intended, not a fallback.
    intended_full_reasons: tuple[str, ...] = ("full-context",)
    #: At warn, landing waits until this many pull requests are ready.
    warn_batch_min: int = 3
    #: ...or until the oldest ready one has waited this long.
    batch_max_wait_hours: float = 6.0
    #: Labels or head-branch prefixes that mark a release pull request.
    release_labels: tuple[str, ...] = ("release",)
    release_branch_prefixes: tuple[str, ...] = ("release/",)
    #: HERALD recipients. The inbox is always one of the channels.
    notify_recipients: tuple[str, ...] = ("@operator",)
    #: Lint: a job whose timeout is at least this many minutes is long.
    long_job_minutes: int = 20
    #: A budget state older than this is not trusted by ``land --act``.
    state_ttl_hours: float = 2.0
    notes: tuple[str, ...] = field(default_factory=tuple)


def _strings(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        raise LanderRefused(f"ci_health.{name} must be a list of strings")
    return tuple(str(item) for item in value)


def _number(table: dict, key: str, default: float, *, low: float = 0.0, high: float | None = None) -> float:
    raw = table.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise LanderRefused(f"ci_health.{key} must be a number")
    value = float(raw)
    if value < low or (high is not None and value > high):
        raise LanderRefused(f"ci_health.{key} is out of range")
    return value


def parse_ci_health(data: dict) -> CIHealthConfig:
    """Build a config from a parsed TOML document. Missing keys keep defaults."""
    table = data.get("ci_health") or {}
    if not isinstance(table, dict):
        raise LanderRefused("ci_health must be a table")
    base = CIHealthConfig()
    warn = _number(table, "warn_pct", base.warn_pct, high=100)
    stop = _number(table, "stop_pct", base.stop_pct, high=100)
    if warn >= stop:
        raise LanderRefused("ci_health.warn_pct must be below stop_pct")
    basis = str(table.get("budget_basis") or base.budget_basis)
    if basis not in {"net", "gross"}:
        raise LanderRefused("ci_health.budget_basis must be net or gross")
    return CIHealthConfig(
        org=str(table.get("org") or ""),
        repos=_strings(table.get("repos"), "repos"),
        budget_dollars=_number(table, "budget_dollars", base.budget_dollars),
        budget_basis=basis,
        warn_pct=warn,
        stop_pct=stop,
        run_rate_multiple=_number(table, "run_rate_multiple", base.run_rate_multiple),
        waste_share=_number(table, "waste_share", base.waste_share, high=1),
        window_days=int(_number(table, "window_days", base.window_days, low=1)),
        max_run_pages=int(_number(table, "max_run_pages", base.max_run_pages, low=1)),
        chronic_red_runs=int(_number(table, "chronic_red_runs", base.chronic_red_runs, low=2)),
        release_workflows=_strings(table.get("release_workflows"), "release_workflows") or base.release_workflows,
        tag_pattern=str(table.get("tag_pattern") or base.tag_pattern),
        default_branch=str(table.get("default_branch") or base.default_branch),
        max_cancels=int(_number(table, "max_cancels", base.max_cancels)),
        selection_workflows=_strings(table.get("selection_workflows"), "selection_workflows"),
        selection_jobs=_strings(table.get("selection_jobs"), "selection_jobs") or base.selection_jobs,
        selection_log_sample=int(_number(table, "selection_log_sample", base.selection_log_sample)),
        intended_full_reasons=_strings(table.get("intended_full_reasons"), "intended_full_reasons")
        or base.intended_full_reasons,
        warn_batch_min=int(_number(table, "warn_batch_min", base.warn_batch_min, low=1)),
        batch_max_wait_hours=_number(table, "batch_max_wait_hours", base.batch_max_wait_hours),
        release_labels=_strings(table.get("release_labels"), "release_labels") or base.release_labels,
        release_branch_prefixes=_strings(table.get("release_branch_prefixes"), "release_branch_prefixes")
        or base.release_branch_prefixes,
        notify_recipients=_strings(table.get("notify_recipients"), "notify_recipients") or base.notify_recipients,
        long_job_minutes=int(_number(table, "long_job_minutes", base.long_job_minutes, low=1)),
        state_ttl_hours=_number(table, "state_ttl_hours", base.state_ttl_hours),
    )


def load_ci_health(path: Path | None) -> CIHealthConfig:
    """Read ``[ci_health]`` from ``path``. No file keeps every default."""
    if path is None:
        candidate = Path.cwd() / ".axiom" / "release-lander.toml"
        path = candidate if candidate.is_file() else None
    if path is None:
        return CIHealthConfig()
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return CIHealthConfig()
    return parse_ci_health(data)
