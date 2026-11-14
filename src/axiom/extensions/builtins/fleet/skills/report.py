# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``fleet.report`` — the node-side push (spec-fleet-console §7).

Gathers a local snapshot and POSTs it OUT to the configured console.
Absence of push config means the node has not opted in: the skill says so
and exits ok (a probe never warns). Collectors are best-effort — a broken
collector drops its kind with a note; it never blocks the others, because
a heartbeat that fails on a side-collector is how silent fleets happen.

Scheduling rides PULSE; this function is the schedule's action.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

PUSH_URL_ENV = "AXIOM_FLEET_PUSH_URL"
PUSH_TOKEN_ENV = "AXIOM_FLEET_PUSH_TOKEN"
NODE_ID_ENV = "AXIOM_FLEET_NODE_ID"

DEFAULT_CADENCES = {"heartbeat": 900, "service_health": 900, "versions": 86_400}

#: The push's own record, in the state directory: when it last succeeded, and how
#: many pushes in a row have failed since. A push refused every few minutes for a
#: week left no trace before this (#1777).
PUSH_RECORD = "fleet-push.json"
#: Failures in a row after which each further failure is logged at ERROR.
REPEATED_FAILURES = 3

_LOG = logging.getLogger("axi.fleet")


def _collect_heartbeat() -> dict:
    return {"at": datetime.now(UTC).isoformat()}


def _collect_service_health() -> dict:
    from axiom.extensions.builtins.status.cli import HealthChecker

    system = HealthChecker().check_all()
    return {
        "services": [
            {
                "name": svc.name,
                "status": svc.status.value if hasattr(svc.status, "value") else str(svc.status),
                "latency_ms": svc.latency_ms,
            }
            for svc in system.services
        ]
    }


def _collect_versions() -> dict:
    from importlib import metadata

    versions: dict[str, str] = {"python": platform.python_version()}
    for pkg in ("axiom-os-lm",):
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    return {"versions": versions}


def _collect_agents() -> dict:
    """This node's agent roster: who reports, how often they said they would,
    and who an operator chose not to run.

    Why this travels as a fleet report rather than being read off disk by
    whatever renders it: a reader on another machine has no disk to read. The
    roster is a fact about THIS node, and the only way it reaches a console
    somewhere else is the same way every other node fact does — pushed, under
    a credential whose site the collector does not choose.

    ``collectedAt`` is stamped here rather than inferred from arrival time,
    because the gap between the two is exactly what a stale push looks like
    and the evaluator needs to be able to see it.
    """
    from datetime import UTC, datetime

    from axiom.extensions.builtins.agents.liveness import agent_liveness

    payload: dict = {
        "agents": agent_liveness(),
        "collectedAt": datetime.now(UTC).isoformat(),
    }

    # Coverage rides in the same payload rather than as its own kind: it is
    # the denominator for the roster above — how much of this node could
    # report at all — and the two are only meaningful read together. Split
    # across two kinds they can arrive at different times and a console would
    # be pairing a roster with a coverage figure from another push.
    try:
        from axiom.extensions.builtins.hygiene.skills.capabilities import (
            bound_registry,
            capability_coverage,
        )

        payload["coverage"] = capability_coverage(bound_registry())
    except Exception:
        # Binding every extension can fail on a broken install. The roster is
        # still worth pushing, and an absent coverage key says the figure is
        # unknown where a zeroed one would read as an empty platform.
        pass
    return payload


COLLECTORS: dict[str, Callable[[], dict]] = {
    "agents": _collect_agents,
    "heartbeat": _collect_heartbeat,
    "service_health": _collect_service_health,
    "versions": _collect_versions,
}


def _default_transport(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — https push target from operator config
        return json.loads(resp.read().decode() or "{}")


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    url = params.get("push_url") or os.environ.get(PUSH_URL_ENV)
    token = params.get("push_token") or os.environ.get(PUSH_TOKEN_ENV)
    if not url or not token:
        return SkillResult(
            ok=True,
            value={"enrolled": False},
            actions_taken=["fleet push not configured; node is not enrolled — nothing sent"],
        )

    node_id = params.get("node_id") or os.environ.get(NODE_ID_ENV) or platform.node()
    transport = params.get("_transport") or _default_transport

    reports: list[dict] = []
    notes: list[str] = []
    for kind, collector in COLLECTORS.items():
        try:
            reports.append({"kind": kind, "payload": collector()})
        except Exception as exc:  # noqa: BLE001 — per-collector isolation is the contract
            notes.append(f"collector {kind} failed: {exc}")

    body = {
        "node_id": node_id,
        "cadences": params.get("cadences") or DEFAULT_CADENCES,
        "reports": reports,
    }
    try:
        response = transport(url, token, body)
    except Exception as exc:  # noqa: BLE001 — the push itself failing is the reportable outcome
        status, reason, fix = _explain_failure(exc)
        record = _record_outcome(ctx, ok=False, status=status, reason=reason)
        failures = record.get("consecutive_failures", 1)
        message = (f"fleet push to {url} failed"
                   + (f": HTTP {status}" if status else "") + f": {reason}. {fix}")
        log = _logger(ctx)
        if failures >= REPEATED_FAILURES:
            log.error("%s (%d pushes in a row have failed, since %s)", message, failures,
                      record.get("first_failed_at"))
        else:
            log.warning("%s", message)
        return SkillResult(
            ok=False,
            value={"enrolled": True, "sent": 0, "error": reason, "status": status,
                   "consecutive_failures": failures,
                   "first_failed_at": record.get("first_failed_at"), "notes": notes},
            errors=[message] + ([f"{failures} pushes in a row have failed, since "
                                 f"{record.get('first_failed_at')}"] if failures > 1 else []),
            actions_taken=[f"push to {url} failed: {reason}", *notes],
            unmet=[{"id": "fleet.push_accepted", "why": reason, "fix": fix}],
        )

    _record_outcome(ctx, ok=True)
    return SkillResult(
        ok=True,
        value={
            "enrolled": True,
            "sent": len(reports),
            "accepted": response.get("accepted"),
            "notes": notes,
        },
        actions_taken=[f"pushed {len(reports)} report(s) to {url}", *notes],
    )


def _explain_failure(exc: Exception) -> tuple[int | None, str, str]:
    """``(HTTP status or None, the console's reason, the fix)`` for a failed push.

    The reason is the console's own error envelope message when it sent one.
    Never includes the token.
    """
    import urllib.error

    if not isinstance(exc, urllib.error.HTTPError):
        return None, str(exc) or exc.__class__.__name__, (
            f"Check that {PUSH_URL_ENV} is the console's address and that it is reachable.")
    status = exc.code
    reason, code = exc.reason or "", ""
    try:
        error = json.loads(exc.read().decode() or "{}").get("error") or {}
        reason, code = str(error.get("message") or reason), str(error.get("code") or "")
    except Exception:  # noqa: BLE001 - a body that is not the envelope keeps the status line
        pass
    reason = str(reason) or f"HTTP {status}"
    if code == "invalid_credential" or status == 401:
        fix = (f"The console does not accept this node's key (unknown, revoked or missing): "
               f"issue a site-bound key on the console (axi gate issue api-key --principal "
               f"<handle> --site <site> --scope fleet:invoke) and set it as {PUSH_TOKEN_ENV}.")
    elif status == 403:
        fix = ("The key needs the fleet:invoke scope and a bound site: on the console, run "
               "axi gate rescope api-key <key-id> --add-scope fleet:invoke (add --site <site> "
               "if it has none).")
    else:
        fix = "See the console's log for this request."
    return status, reason, fix


def _logger(ctx: SkillContext | None) -> logging.Logger:
    return getattr(ctx, "logger", None) or _LOG


def _record_outcome(ctx: SkillContext | None, *, ok: bool, status: int | None = None,
                    reason: str = "") -> dict:
    """Update the push record (:data:`PUSH_RECORD`) and return it. Never raises."""
    try:
        state_dir = getattr(ctx, "state_dir", None)
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        path = Path(state_dir) / PUSH_RECORD
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            record = {}
        now = datetime.now(UTC).isoformat()
        if ok:
            record.update(consecutive_failures=0, first_failed_at=None, last_success_at=now)
        else:
            failures = int(record.get("consecutive_failures") or 0) + 1
            record.update(consecutive_failures=failures, last_failed_at=now,
                          last_status=status, last_reason=reason)
            if failures == 1 or not record.get("first_failed_at"):
                record["first_failed_at"] = now
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True))
        return record
    except Exception as exc:  # noqa: BLE001 - the record must never mask the push outcome
        _LOG.warning("could not update the fleet push record: %s", exc)
        return {"consecutive_failures": 0 if ok else 1}


__all__ = ["run", "COLLECTORS", "PUSH_RECORD", "REPEATED_FAILURES"]
