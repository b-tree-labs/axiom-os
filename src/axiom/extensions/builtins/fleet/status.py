# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Effect-checked status evaluation (ADR-119 D4, spec-fleet-console §4-§5).

The invariant: GREEN requires cited observed-effect evidence. A claim
without evidence is UNPROVEN — distinct from FAILED (the backup-validate
WARN/FAIL "unproven vs broken" split, generalized). Silence is failure:
anything past 3x its declared cadence is STALE, and staleness beats
content — there is no last-known-green.

Evaluation happens at read time from stored reports; there is no
background marker job, so the judgment cannot itself go stale.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from axiom.infra.time_utils import time_ago

STALE_MULTIPLIER = 3

REPORT_KINDS = (
    "heartbeat",
    "service_health",
    "backup",
    "backup_validate",
    "canary",
    "versions",
)


class Status(str, enum.Enum):
    GREEN = "green"
    UNPROVEN = "unproven"
    STALE = "stale"
    FAILED = "failed"
    UNKNOWN = "unknown"


# Worst wins. UNKNOWN outranks GREEN deliberately: absence of judgment is
# never rendered as health (the axi-status "UNKNOWN is not a state you
# resolve optimistically" lesson).
_SEVERITY = {
    Status.FAILED: 4,
    Status.STALE: 3,
    Status.UNPROVEN: 2,
    Status.UNKNOWN: 1,
    Status.GREEN: 0,
}


@dataclass(frozen=True)
class Evaluation:
    kind: str
    status: Status
    evidence: str
    #: How the judgement was reached, as data — the stored values, the
    #: rule and how to check it without this platform. None where the
    #: judgement has no arithmetic worth redoing. See receipts.derivation.
    derivation: dict | None = None


def rollup(statuses: Iterable[Status]) -> Status:
    worst: Status | None = None
    for s in statuses:
        if worst is None or _SEVERITY[s] > _SEVERITY[worst]:
            worst = s
    return worst if worst is not None else Status.UNKNOWN


def _parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _eval_heartbeat(payload: dict) -> tuple[Status, str]:
    # Freshness IS the effect; staleness was already ruled out upstream.
    return Status.GREEN, "Reported on time."


def _eval_service_health(payload: dict) -> tuple[Status, str]:
    services = payload.get("services") or []
    if not services:
        return Status.UNPROVEN, "no services in report"
    unhealthy = [s.get("name", "?") for s in services if s.get("status") == "unhealthy"]
    if unhealthy:
        return Status.FAILED, f"unhealthy: {', '.join(unhealthy)}"
    unproven = [
        s.get("name", "?")
        for s in services
        if s.get("status") in ("degraded", "unknown") or s.get("latency_ms") is None
    ]
    if unproven:
        return (
            Status.UNPROVEN,
            f"claimed without observed latency or degraded: {', '.join(unproven)}",
        )
    return Status.GREEN, f"{len(services)} services healthy with observed latency"


def _eval_backup(payload: dict, *, cadence_seconds: int, now: datetime) -> tuple[Status, str]:
    if payload.get("error"):
        return Status.FAILED, str(payload["error"])
    artifact = payload.get("artifact")
    size = payload.get("size_bytes")
    created = _parse_ts(payload.get("created_at", ""))
    if not artifact or not isinstance(size, (int, float)) or size <= 0 or created is None:
        return Status.UNPROVEN, "missing observed effect (artifact / size_bytes>0 / created_at)"
    if (now - created).total_seconds() > STALE_MULTIPLIER * cadence_seconds:
        return Status.STALE, f"dump created_at {created.isoformat()} exceeds 3x cadence"
    return (
        Status.GREEN,
        f"artifact {artifact}, size_bytes={int(size)}, created {created.isoformat()}",
    )


def _eval_backup_validate(payload: dict) -> tuple[Status, str]:
    checks = {c.get("name"): c.get("status") for c in payload.get("checks") or []}
    fails = [n for n, s in checks.items() if s == "FAIL"]
    if fails:
        return Status.FAILED, f"FAIL: {', '.join(fails)}"
    if "restore_live" not in checks:
        return Status.UNPROVEN, "restore_live check absent — restorability unproven"
    warns = [n for n, s in checks.items() if s == "WARN"]
    if warns:
        return Status.UNPROVEN, f"WARN (unproven, not broken): {', '.join(warns)}"
    if not checks or any(s != "PASS" for s in checks.values()):
        return Status.UNPROVEN, "checks incomplete"
    return Status.GREEN, f"all checks PASS including restore_live ({len(checks)} checks)"


def _eval_canary(payload: dict) -> tuple[Status, str]:
    status = payload.get("status")
    smoke = payload.get("smoke_results") or {}
    if status in ("red", "rollback"):
        return (
            Status.FAILED,
            f"canary {status}: {payload.get('failure_reason') or 'see smoke_results'}",
        )
    if status == "green" and smoke:
        return Status.GREEN, f"canary green with {len(smoke)} smoke results"
    if status == "green":
        return Status.UNPROVEN, "green claimed with no smoke results"
    return Status.UNPROVEN, f"unrecognized canary status {status!r}"


def _eval_versions(payload: dict) -> tuple[Status, str]:
    versions = payload.get("versions") or {}
    if not versions:
        return Status.UNPROVEN, "no versions reported"
    return Status.GREEN, f"{len(versions)} packages reported (drift computed separately)"


_BINDINGS: dict[str, Callable[..., tuple[Status, str]]] = {
    "heartbeat": _eval_heartbeat,
    "service_health": _eval_service_health,
    "backup_validate": _eval_backup_validate,
    "canary": _eval_canary,
    "versions": _eval_versions,
}


def every(seconds: int) -> str:
    """A cadence as a person says it: ``every 15 minutes``, ``every day``.

    The evaluator's evidence is read on a case page, so the numbers in it
    are the reader's numbers. ``past 3x cadence (900s)`` is three machine
    units in a row and tells a person nothing they can act on.
    """
    if seconds % 86_400 == 0:
        days = seconds // 86_400
        return "every day" if days == 1 else f"every {days} days"
    if seconds % 3_600 == 0:
        hours = seconds // 3_600
        return "every hour" if hours == 1 else f"every {hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "every minute" if minutes == 1 else f"every {minutes} minutes"
    return f"every {seconds} seconds"


def evaluate_report(
    *,
    kind: str,
    payload: dict,
    received_at: datetime,
    cadence_seconds: int,
    now: datetime,
    node_id: str = "",
) -> Evaluation:
    """Evaluate one stored report. Staleness beats content for every kind."""
    if kind not in REPORT_KINDS:
        raise ValueError(f"unknown report kind {kind!r}; known: {REPORT_KINDS}")

    age = (now - received_at).total_seconds()
    if age > STALE_MULTIPLIER * cadence_seconds:
        from axiom.extensions.builtins.receipts.derivation import stale_derivation

        sentence = (
            f"The last report arrived {time_ago(received_at, now=now, compact=False)}. "
            f"This node reports {every(cadence_seconds)}."
        )
        return Evaluation(
            kind=kind,
            status=Status.STALE,
            evidence=sentence,
            derivation=stale_derivation(
                claim_kind=kind,
                entity_id=node_id or "",
                claim=sentence,
                received_at=received_at.isoformat(),
                age_seconds=int(age),
                cadence_seconds=cadence_seconds,
                multiplier=STALE_MULTIPLIER,
            ).payload(),
        )

    if kind == "backup":
        status, evidence = _eval_backup(payload, cadence_seconds=cadence_seconds, now=now)
    else:
        status, evidence = _BINDINGS[kind](payload)
    return Evaluation(kind=kind, status=status, evidence=evidence)
