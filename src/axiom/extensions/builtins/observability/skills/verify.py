# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``observe.verify`` — pre-flight and post-install probes.

Two phases:

- ``preflight`` — helm + kubectl on PATH, active kubectl context,
  target namespace exists.
- ``postinstall`` — ``/api/public/health`` returns 200, and a scratch
  trace round-trip lands successfully via the Langfuse HTTP API.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _http_get(url: str, timeout: float = 5.0):
    """Indirection so tests can monkeypatch without importing requests."""
    import urllib.request

    class _Resp:
        def __init__(self, code: int, text: str) -> None:
            self.status_code = code
            self.text = text

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return _Resp(r.status, r.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return _Resp(0, str(exc))


def _round_trip_trace(host: str, public_key: str, secret_key: str) -> bool:
    """Land a scratch trace via the Langfuse provider; return True on success."""
    try:
        from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider
        p = LangfuseTraceProvider(public_key=public_key, secret_key=secret_key, host=host)
        tid = p.start_trace("observe.verify.scratch", source="observe.verify")
        p.log_generation(tid, model="probe", prompt="ping", output="pong")
        p.flush()
        return True
    except Exception:  # noqa: BLE001
        return False


def _preflight(params: dict[str, Any]) -> SkillResult:
    actions: list[str] = []
    errors: list[str] = []
    for b in ("helm", "kubectl"):
        if shutil.which(b) is None:
            errors.append(f"{b} not on PATH")
    if errors:
        return SkillResult(ok=False, errors=errors, actions_taken=actions)

    r = subprocess.run(
        ["kubectl", "config", "current-context"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return SkillResult(ok=False, errors=["no active kubectl context"],
                           actions_taken=actions)
    actions.append(f"context: {r.stdout.strip()}")
    return SkillResult(ok=True, value={"phase": "preflight"}, actions_taken=actions)


def _postinstall(params: dict[str, Any]) -> SkillResult:
    actions: list[str] = []
    host = params.get("host")
    if not host:
        return SkillResult(ok=False, errors=["host parameter required"])

    health_url = host.rstrip("/") + "/api/public/health"
    resp = _http_get(health_url)
    actions.append(f"GET {health_url} → {resp.status_code}")
    if resp.status_code != 200:
        return SkillResult(
            ok=False,
            errors=[f"langfuse health probe returned {resp.status_code}"],
            actions_taken=actions,
        )

    public_key = params.get("public_key")
    secret_key = params.get("secret_key")
    if public_key and secret_key:
        ok = _round_trip_trace(host, public_key, secret_key)
        actions.append(f"scratch trace round-trip: {'ok' if ok else 'failed'}")
        if not ok:
            return SkillResult(ok=False, errors=["round-trip trace failed"],
                               actions_taken=actions)
    else:
        actions.append("keys not provided — skipping round-trip probe")

    return SkillResult(ok=True, value={"phase": "postinstall", "host": host},
                       actions_taken=actions)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    phase = params.get("phase", "preflight")
    if phase == "preflight":
        return _preflight(params)
    if phase == "postinstall":
        return _postinstall(params)
    return SkillResult(ok=False, errors=[f"unknown phase: {phase!r}"])
