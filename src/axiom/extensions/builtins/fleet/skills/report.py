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
import os
import platform
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

PUSH_URL_ENV = "AXIOM_FLEET_PUSH_URL"
PUSH_TOKEN_ENV = "AXIOM_FLEET_PUSH_TOKEN"
NODE_ID_ENV = "AXIOM_FLEET_NODE_ID"

DEFAULT_CADENCES = {"heartbeat": 900, "service_health": 900, "versions": 86_400}


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


COLLECTORS: dict[str, Callable[[], dict]] = {
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
        return SkillResult(
            ok=False,
            value={"enrolled": True, "sent": 0, "error": str(exc), "notes": notes},
            actions_taken=[f"push to {url} failed: {exc}", *notes],
        )

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


__all__ = ["run", "COLLECTORS"]
