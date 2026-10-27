# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who turns a diagnosis into a diff.

The mechanical provider asks the existing ``ci_repair`` skill
(``plan_repair``). Another backend — a local agent, a container — is a
second implementation of ``FixProvider`` registered on
``axiom.release.fix_provider``. This module does not push.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from axiom.extensions.builtins.release.ci_flow import plan_repair
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.policy.agent_action_guard import human_route

BLOCKERS = (
    "failing_test",
    "lint",
    "config",
    "stale_branch",
    "flaky",
    "dependency_conflict",
    "merge_conflict",
    "batching_gate",
    "clear",
)


@dataclass(frozen=True)
class Diagnosis:
    kind: str
    evidence: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Fix:
    """A proposed diff. ``force`` is refused by every executor."""

    summary: str
    diff: str = ""
    commands: tuple[str, ...] = ()
    kind: str = ""
    needs_human: bool = False
    reason: str = ""
    force: bool = False
    pending: bool = False
    writer: str = ""
    branch: str = ""
    target_ref: str = ""
    paths: tuple[str, ...] = ()

    @property
    def diff_sha(self) -> str:
        import hashlib

        return hashlib.sha256(self.diff.encode()).hexdigest()


class FixProvider(Protocol):
    name: str

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        """What is blocking this pull request or the pipeline."""

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        """The fix to propose. It does not apply the fix."""


class NullFixProvider:
    """No fixer for this language. Every blocker is a person's decision."""

    name = "null"

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        if pull.checks == "passing" and pull.mergeable is not False:
            return Diagnosis("clear", "checks passing", pull.files)
        return Diagnosis("failing_test", pull.failure_log or pull.title, pull.files)

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        del pull
        if diagnosis.kind == "clear":
            return Fix(summary="land", kind="land")
        return Fix(summary=diagnosis.kind, needs_human=True, reason="no-fix-provider")


class MechanicalFixProvider:
    """Diagnose from the check and the log. Draft with ``plan_repair``."""

    name = "mechanical"

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        text = f"{pull.failure_log}\n{pull.title}".lower()
        paths = pull.files
        if pull.mergeable is False or "merge conflict" in text:
            return Diagnosis("merge_conflict", pull.failure_log or "not mergeable", paths)
        if "batch" in text and ("required" in text or "gate" in text):
            return Diagnosis("batching_gate", pull.failure_log or pull.title, paths)
        if "depend" in text and "conflict" in text:
            return Diagnosis("dependency_conflict", pull.failure_log, paths)
        if "flak" in text:
            return Diagnosis("flaky", pull.failure_log, paths)
        if "workflow" in text or text.strip().endswith(".yml"):
            return Diagnosis("config", pull.failure_log or pull.title, paths)
        if "behind" in text or "stale branch" in text:
            return Diagnosis("stale_branch", pull.failure_log or pull.title, paths)
        if "ruff" in text or "lint" in text:
            return Diagnosis("lint", pull.failure_log or pull.title, paths)
        if pull.checks == "passing":
            return Diagnosis("clear", "checks passing", paths)
        return Diagnosis("failing_test", pull.failure_log or pull.title, paths)

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        if human_route(diagnosis.paths or pull.files):
            return Fix(summary=diagnosis.kind, needs_human=True, reason="human_route")
        if diagnosis.kind == "clear":
            return Fix(summary="land", kind="land")
        if diagnosis.kind in {"merge_conflict", "dependency_conflict", "batching_gate"}:
            return Fix(summary=diagnosis.kind, needs_human=True, reason=diagnosis.kind)
        if diagnosis.kind == "flaky":
            return Fix(summary="rerun the flaky job", kind="rerun", commands=("rerun",))
        if diagnosis.kind == "stale_branch":
            return Fix(
                summary="merge the base into the head",
                kind="stale_branch",
                diff="merge base into head\n",
                commands=("git merge HEAD",),
            )
        planned = plan_repair(diagnosis.evidence, attempts=pull.ci_attempts)
        if planned.get("pattern") in {"try-cap", "unknown"} or planned.get("escalate"):
            return Fix(
                summary=str(planned.get("detail") or diagnosis.kind),
                needs_human=True,
                reason=str(planned.get("pattern") or "escalate"),
            )
        commands = tuple(str(item) for item in planned.get("commands") or ())
        diff = "\n".join(commands) + ("\n" if commands else "")
        return Fix(
            summary=str(planned.get("detail") or diagnosis.kind),
            diff=diff or str(planned.get("detail") or ""),
            commands=commands,
            kind=diagnosis.kind,
        )


def resolve_fix_provider(name: str = "mechanical") -> FixProvider:
    """Built-ins first, then ``axiom.release.fix_provider``."""
    if name in {"", "mechanical"}:
        return MechanicalFixProvider()
    if name == "null":
        return NullFixProvider()
    if name == "handoff":
        from axiom.extensions.builtins.release.handoff import WebhookHandoffFixProvider

        return WebhookHandoffFixProvider()
    try:
        from importlib.metadata import entry_points

        for item in entry_points(group="axiom.release.fix_provider"):
            if item.name == name:
                loaded = item.load()
                return loaded()
    except Exception:
        pass
    raise LookupError(f"no fix provider named {name!r}")
