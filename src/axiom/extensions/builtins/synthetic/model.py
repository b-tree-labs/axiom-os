# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Journey definitions and results.

Three outcomes, never two: ``pass`` (every step proven), ``fail`` (a step
observed the wrong thing), ``unproven`` (a step could not be run: missing
credential, driver not installed). Unproven is not a pass, so a gate that
requires ``pass`` cannot be satisfied by a check that did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pass", "fail", "unproven"]


@dataclass(frozen=True)
class Expect:
    status: int | None = None
    status_in: tuple[int, ...] = ()
    content_type: str | None = None  # prefix match, e.g. "text/html"
    header_present: tuple[str, ...] = ()
    header_absent: tuple[str, ...] = ()
    header_matches: dict[str, str] = field(default_factory=dict)  # name -> regex
    location_matches: str | None = None  # regex against the Location header
    cache_policy: str | None = None  # see cachepolicy.POLICIES
    body_contains: tuple[str, ...] = ()
    body_not_contains: tuple[str, ...] = ()
    assets_not_html: bool = False  # same-origin scripts/styles must not come back as HTML
    max_latency_ms: int | None = None


@dataclass(frozen=True)
class Step:
    name: str
    path: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    auth: Literal["none", "session"] = "none"
    expect: Expect = field(default_factory=Expect)


@dataclass(frozen=True)
class Journey:
    name: str
    base_url: str
    steps: tuple[Step, ...]
    session_cookie_env: str | None = None  # env var holding "name=value"; never the value itself
    timeout_s: float = 10.0
    verify_tls: bool = True


@dataclass
class Evidence:
    status: int | None = None
    elapsed_ms: int | None = None
    headers: dict[str, str] = field(default_factory=dict)  # allowlisted; no cookie values
    note: str | None = None


@dataclass
class StepResult:
    name: str
    status: Status
    detail: str
    evidence: Evidence = field(default_factory=Evidence)


@dataclass
class JourneyResult:
    journey: str
    base_url: str
    status: Status
    steps: list[StepResult]
    started_at: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "journey": self.journey,
            "base_url": self.base_url,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "steps": [
                {
                    "name": s.name,
                    "status": s.status,
                    "detail": s.detail,
                    "evidence": {
                        "status": s.evidence.status,
                        "elapsed_ms": s.evidence.elapsed_ms,
                        "headers": s.evidence.headers,
                        "note": s.evidence.note,
                    },
                }
                for s in self.steps
            ],
        }


def roll_up(statuses: list[Status]) -> Status:
    if "fail" in statuses:
        return "fail"
    if "unproven" in statuses or not statuses:
        return "unproven"
    return "pass"
