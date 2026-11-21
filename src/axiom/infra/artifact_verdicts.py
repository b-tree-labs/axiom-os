# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared RIVET → TIDY contract: is this Actions artifact still needed?

RIVET (the lander and CI-health sweep) knows when a run or artifact is
still needed — a PR in flight or in the merge queue, a failing run being
diagnosed or retried, a release or tag pending, an open fix attempt, a
bisect, a flaky-test investigation — or disposable — superseded by a
newer green run, the PR merged or closed, a re-run that succeeded.

The durable record lives in the agent state store
(``<state_dir>/agents/rivet/artifact-verdicts.json``), which both agents
already share. A ``rivet.artifact_verdict`` event is published when a
bus is available; a missed event is fine because TIDY reads the file.

TIDY consults this before deleting:

* ``keep`` — skip, even if the artifact is past its class retention.
* ``disposable`` — eligible early, still bounded by caps and the
  never-touch list.
* ``unknown``, an expired verdict, or an unreachable store — fall back
  to retention only, and never delete early.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from axiom.infra.state import LockedJsonFile

_log = logging.getLogger("axiom.artifact_verdicts")

VERDICTS = frozenset({"keep", "disposable", "unknown"})
EVENT_SUBJECT = "rivet.artifact_verdict"

KEEP_REASONS = frozenset({
    "pr_in_flight",
    "merge_queue",
    "failing_run_diagnosed",
    "retry_pending",
    "release_pending",
    "tag_pending",
    "open_fix_attempt",
    "bisect",
    "flaky_investigation",
})
DISPOSABLE_REASONS = frozenset({
    "superseded_green",
    "pr_merged",
    "pr_closed",
    "rerun_succeeded",
})

DEFAULT_KEEP_TTL = timedelta(days=7)
DEFAULT_DISPOSABLE_TTL = timedelta(days=2)

VerdictName = Literal["keep", "disposable", "unknown"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def verdict_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "artifact-verdicts.json"


def _key(verdict: "ArtifactVerdict") -> str:
    if verdict.artifact_id:
        return f"artifact:{verdict.repo}:{verdict.artifact_id}"
    if verdict.run_id:
        return f"run:{verdict.repo}:{verdict.run_id}"
    if verdict.head_sha:
        return f"sha:{verdict.repo}:{verdict.head_sha}"
    return f"misc:{verdict.repo}:{verdict.reason}:{verdict.updated_at.isoformat()}"


@dataclass(frozen=True)
class ArtifactVerdict:
    """One keep/disposable/unknown decision RIVET wrote for TIDY."""

    verdict: str
    reason: str
    repo: str = ""
    artifact_id: str = ""
    run_id: str = ""
    head_sha: str = ""
    expires_at: datetime | None = None
    updated_at: datetime | None = None
    source: str = "rivet"

    def __post_init__(self) -> None:
        name = (self.verdict or "unknown").lower()
        if name not in VERDICTS:
            object.__setattr__(self, "verdict", "unknown")
        else:
            object.__setattr__(self, "verdict", name)

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or _now())

    def effective(self, now: datetime | None = None) -> str:
        if self.expired(now):
            return "unknown"
        return self.verdict if self.verdict in VERDICTS else "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "repo": self.repo,
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "head_sha": self.head_sha,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactVerdict":
        return cls(
            verdict=str(data.get("verdict") or "unknown"),
            reason=str(data.get("reason") or ""),
            repo=str(data.get("repo") or ""),
            artifact_id=str(data.get("artifact_id") or ""),
            run_id=str(data.get("run_id") or data.get("workflow_run_id") or ""),
            head_sha=str(data.get("head_sha") or ""),
            expires_at=_parse_dt(data.get("expires_at")),
            updated_at=_parse_dt(data.get("updated_at")),
            source=str(data.get("source") or "rivet"),
        )

    @classmethod
    def make(
        cls,
        verdict: str,
        reason: str,
        *,
        repo: str,
        artifact_id: str = "",
        run_id: str | int = "",
        head_sha: str = "",
        expires_at: datetime | None = None,
        now: datetime | None = None,
        source: str = "rivet",
    ) -> "ArtifactVerdict":
        now = now or _now()
        if expires_at is None:
            ttl = DEFAULT_KEEP_TTL if verdict == "keep" else DEFAULT_DISPOSABLE_TTL
            expires_at = now + ttl
        return cls(
            verdict=verdict,
            reason=reason,
            repo=repo,
            artifact_id=str(artifact_id or ""),
            run_id=str(run_id or ""),
            head_sha=head_sha,
            expires_at=expires_at,
            updated_at=now,
            source=source,
        )


UNKNOWN = ArtifactVerdict(verdict="unknown", reason="")


@dataclass
class VerdictLoad:
    """Result of reading the store. ``unreachable`` means TIDY must not delete early."""

    items: list[ArtifactVerdict] = field(default_factory=list)
    unreachable: bool = False

    def lookup(
        self,
        *,
        artifact_id: str = "",
        run_id: str = "",
        repo: str = "",
        head_sha: str = "",
        now: datetime | None = None,
    ) -> ArtifactVerdict:
        if self.unreachable:
            return UNKNOWN
        now = now or _now()
        matches: list[ArtifactVerdict] = []
        for item in self.items:
            if repo and item.repo and item.repo != repo:
                continue
            if artifact_id and item.artifact_id and item.artifact_id == str(artifact_id):
                matches.append(item)
                continue
            if run_id and item.run_id and item.run_id == str(run_id):
                matches.append(item)
                continue
            if head_sha and item.head_sha and item.head_sha == head_sha:
                matches.append(item)
        live = [m for m in matches if m.effective(now) != "unknown"]
        if not live:
            return UNKNOWN
        keeps = [m for m in live if m.effective(now) == "keep"]
        pool = keeps or live
        pool.sort(key=lambda m: m.updated_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return pool[0]


def load_verdicts(state_dir: Path) -> VerdictLoad:
    """Read the store. A missing file is empty (all unknown). I/O or parse errors are unreachable."""
    path = verdict_path(state_dir)
    try:
        with LockedJsonFile(path, strict=True) as handle:
            raw = handle.read()
    except FileNotFoundError:
        return VerdictLoad()
    except Exception as exc:  # noqa: BLE001 — TIDY must not delete early
        _log.warning("artifact verdict store unreachable: %s", exc)
        return VerdictLoad(unreachable=True)
    if not raw:
        return VerdictLoad()
    rows = raw.get("verdicts") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return VerdictLoad(unreachable=True)
    items: list[ArtifactVerdict] = []
    for row in rows:
        if isinstance(row, dict):
            items.append(ArtifactVerdict.from_dict(row))
    return VerdictLoad(items=items)


def put_verdicts(
    state_dir: Path,
    verdicts: Iterable[ArtifactVerdict],
    *,
    now: datetime | None = None,
    publish: bool = True,
) -> list[ArtifactVerdict]:
    """Upsert verdicts. Expired entries are dropped. Returns the written set."""
    now = now or _now()
    incoming = list(verdicts)
    path = verdict_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with LockedJsonFile(path, exclusive=True, strict=True) as handle:
        raw = handle.read() or {}
        rows = raw.get("verdicts") if isinstance(raw, dict) else []
        if not isinstance(rows, list):
            rows = []
        by_key: dict[str, ArtifactVerdict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = ArtifactVerdict.from_dict(row)
            if item.expired(now):
                continue
            by_key[_key(item)] = item
        for item in incoming:
            by_key[_key(item)] = item
        written = list(by_key.values())
        handle.write({
            "updated_at": now.isoformat(),
            "verdicts": [item.to_dict() for item in written],
        })
    if publish:
        _publish(incoming)
    return written


def _publish(verdicts: Iterable[ArtifactVerdict]) -> None:
    try:
        from axiom.infra.bus import get_default_eventbus

        bus = get_default_eventbus()
        for item in verdicts:
            bus.publish(EVENT_SUBJECT, item.to_dict(), source="rivet")
    except Exception as exc:  # noqa: BLE001 — the file is the source of truth
        _log.info("could not publish %s: %s", EVENT_SUBJECT, exc)


__all__ = [
    "DEFAULT_DISPOSABLE_TTL",
    "DEFAULT_KEEP_TTL",
    "DISPOSABLE_REASONS",
    "EVENT_SUBJECT",
    "KEEP_REASONS",
    "UNKNOWN",
    "ArtifactVerdict",
    "VerdictLoad",
    "load_verdicts",
    "put_verdicts",
    "verdict_path",
]
