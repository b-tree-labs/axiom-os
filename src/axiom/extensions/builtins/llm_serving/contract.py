# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Serving-contract smoke — the cutover + CI gate (PIVOT-8 / #49).

The previous gate produced FALSE rollbacks: a too-short timeout plus a
content-only check flagged a slow-but-correct reasoning model as broken. This
smoke fixes both failure modes:

* **Reasoning-aware** — a non-empty ``content`` OR ``reasoning_content`` counts
  as grounded, so a reasoning model that puts its answer in ``reasoning_content``
  is never read as empty.
* **Latency-sized, slow≠broken** — the timeout is sized to real model latency.
  A correct answer that arrives slowly (above ``warn_latency_s`` but under the
  hard ``timeout_s``) is reported ``SLOW`` and still PASSES the gate; only a
  missing/empty/errored answer, or one exceeding the hard timeout, is
  ``BROKEN`` and triggers rollback.

:func:`run_contract_smoke` is pure: the HTTP get/post callables and a clock are
injected, so it is unit-testable with no network and deterministic latency, and
the CLI ``smoke`` verb wires the real ``urllib`` transport on top.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Injected transports: get(url, headers) -> (status, json); post(url, headers,
# json) -> (status, json). Kept dict-shaped (not requests/httpx objects) so the
# CLI can wire urllib without a third-party dep and tests can fake trivially.
HttpGet = Callable[..., tuple[int, dict]]
HttpPost = Callable[..., tuple[int, dict]]
Clock = Callable[[], float]


class SmokeVerdict(str, Enum):
    HEALTHY = "healthy"   # fast + correct
    SLOW = "slow"         # correct but above the warn latency — NOT a rollback
    BROKEN = "broken"     # missing/empty/errored/timed-out — rollback


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "pass" | "warn" | "fail"
    detail: str = ""


@dataclass(frozen=True)
class SmokeResult:
    verdict: SmokeVerdict
    checks: list[Check] = field(default_factory=list)
    latency_s: float = 0.0

    @property
    def passed(self) -> bool:
        """The gate passes on HEALTHY or SLOW; only BROKEN rolls back."""
        return self.verdict is not SmokeVerdict.BROKEN

    def summary(self) -> str:
        lines = [f"verdict: {self.verdict.value}  (chat latency {self.latency_s:.1f}s)"]
        for c in self.checks:
            mark = {"pass": "ok", "warn": "warn", "fail": "FAIL"}[c.status]
            lines.append(f"  [{mark}] {c.name}{': ' + c.detail if c.detail else ''}")
        return "\n".join(lines)


def _grounded_text(payload: dict) -> str:
    """Extract the answer text, accepting content OR reasoning_content."""
    try:
        msg = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = (msg.get("content") or "").strip()
    if content:
        return content
    return (msg.get("reasoning_content") or "").strip()


def run_contract_smoke(
    base_url: str,
    *,
    model: str,
    http_get: HttpGet,
    http_post: HttpPost,
    clock: Clock,
    timeout_s: float = 60.0,
    warn_latency_s: float = 20.0,
    api_key: str = "",
    prompt: str = "Reply with one short sentence confirming you are online.",
) -> SmokeResult:
    """Run the serving-contract smoke and return a structured verdict.

    Sequence: GET /v1/models (must list ≥1 model) → POST /v1/chat/completions
    (must return grounded content within the hard timeout). A models failure
    short-circuits — no point probing chat on a server that can't list models.
    """
    base = base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    checks: list[Check] = []

    # 1) /v1/models
    try:
        status, body = http_get(f"{base}/v1/models", headers=headers)
    except Exception as exc:  # noqa: BLE001 — a transport error IS the finding
        checks.append(Check("models", "fail", f"{type(exc).__name__}: {exc}"))
        return SmokeResult(SmokeVerdict.BROKEN, checks)
    n_models = len(body.get("data", [])) if status == 200 else 0
    if status != 200 or n_models == 0:
        checks.append(Check("models", "fail", f"HTTP {status}, {n_models} models"))
        return SmokeResult(SmokeVerdict.BROKEN, checks)
    checks.append(Check("models", "pass", f"{n_models} models"))

    # 2) /v1/chat/completions — measure latency
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
    }
    start = clock()
    try:
        status, body = http_post(
            f"{base}/v1/chat/completions", headers=headers, json=payload
        )
    except Exception as exc:  # noqa: BLE001
        latency = clock() - start
        checks.append(Check("chat", "fail", f"{type(exc).__name__}: {exc}"))
        return SmokeResult(SmokeVerdict.BROKEN, checks, latency)
    latency = clock() - start

    if status != 200:
        checks.append(Check("chat", "fail", f"HTTP {status}"))
        return SmokeResult(SmokeVerdict.BROKEN, checks, latency)

    text = _grounded_text(body)
    if not text:
        checks.append(Check("chat", "fail", "empty content and reasoning_content"))
        return SmokeResult(SmokeVerdict.BROKEN, checks, latency)

    # Hard timeout exceeded → broken (a real hang, not just slow).
    if latency > timeout_s:
        checks.append(Check("chat", "fail",
                            f"latency {latency:.1f}s > timeout {timeout_s:.0f}s"))
        return SmokeResult(SmokeVerdict.BROKEN, checks, latency)

    # Correct answer. Slow-but-correct is a WARN, never a rollback.
    if latency > warn_latency_s:
        checks.append(Check("chat", "warn",
                            f"grounded but slow ({latency:.1f}s > {warn_latency_s:.0f}s)"))
        return SmokeResult(SmokeVerdict.SLOW, checks, latency)

    checks.append(Check("chat", "pass", f"grounded in {latency:.1f}s"))
    return SmokeResult(SmokeVerdict.HEALTHY, checks, latency)


__all__ = ["Check", "SmokeResult", "SmokeVerdict", "run_contract_smoke"]
