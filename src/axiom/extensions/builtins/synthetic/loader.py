# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Load a journey from TOML. Unknown keys are errors: a typo must not become a check that never runs."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from . import cachepolicy
from .model import Expect, Journey, Step

_JOURNEY_KEYS = {"name", "base_url", "timeout_s", "verify_tls", "session"}
_STEP_KEYS = {"name", "path", "method", "headers", "auth", "expect"}
_EXPECT_KEYS = {
    "status",
    "status_in",
    "content_type",
    "header_present",
    "header_absent",
    "header_matches",
    "location_matches",
    "cache_policy",
    "body_contains",
    "body_not_contains",
    "assets_not_html",
    "max_latency_ms",
}


class JourneyError(ValueError):
    """The journey file is not usable; the message names the key."""


def _unknown(where: str, got: set[str], allowed: set[str]) -> None:
    extra = got - allowed
    if extra:
        raise JourneyError(f"{where}: unknown key(s) {sorted(extra)}; allowed {sorted(allowed)}")


def parse_journey(data: dict[str, Any]) -> Journey:
    j = data.get("journey")
    if not isinstance(j, dict):
        raise JourneyError("missing [journey] table")
    _unknown("[journey]", set(j), _JOURNEY_KEYS)
    for req in ("name", "base_url"):
        if not j.get(req):
            raise JourneyError(f"[journey] needs {req}")
    session = j.get("session") or {}
    _unknown("[journey.session]", set(session), {"cookie_env"})
    raw_steps = data.get("step") or []
    if not raw_steps:
        raise JourneyError("a journey needs at least one [[step]]")
    steps: list[Step] = []
    for i, s in enumerate(raw_steps, 1):
        where = f"[[step]] #{i}"
        _unknown(where, set(s), _STEP_KEYS)
        for req in ("name", "path"):
            if not s.get(req):
                raise JourneyError(f"{where} needs {req}")
        auth = s.get("auth", "none")
        if auth not in ("none", "session"):
            raise JourneyError(f"{where}: auth must be 'none' or 'session'")
        e = s.get("expect") or {}
        _unknown(f"{where} expect", set(e), _EXPECT_KEYS)
        if e.get("cache_policy") and e["cache_policy"] not in cachepolicy.POLICIES:
            raise JourneyError(f"{where}: cache_policy must be one of {cachepolicy.POLICIES}")
        expect = Expect(
            status=e.get("status"),
            status_in=tuple(e.get("status_in", ())),
            content_type=e.get("content_type"),
            header_present=tuple(h.lower() for h in e.get("header_present", ())),
            header_absent=tuple(h.lower() for h in e.get("header_absent", ())),
            header_matches={k.lower(): v for k, v in e.get("header_matches", {}).items()},
            location_matches=e.get("location_matches"),
            cache_policy=e.get("cache_policy"),
            body_contains=tuple(e.get("body_contains", ())),
            body_not_contains=tuple(e.get("body_not_contains", ())),
            assets_not_html=bool(e.get("assets_not_html", False)),
            max_latency_ms=e.get("max_latency_ms"),
        )
        steps.append(
            Step(
                name=s["name"],
                path=s["path"],
                method=s.get("method", "GET").upper(),
                headers=dict(s.get("headers", {})),
                auth=auth,
                expect=expect,
            )
        )
    if any(s.auth == "session" for s in steps) and not session.get("cookie_env"):
        raise JourneyError(
            "a step uses auth = 'session' but [journey.session] cookie_env is not set"
        )
    return Journey(
        name=j["name"],
        base_url=j["base_url"],
        steps=tuple(steps),
        session_cookie_env=session.get("cookie_env"),
        timeout_s=float(j.get("timeout_s", 10.0)),
        verify_tls=bool(j.get("verify_tls", True)),
    )


def load_journey(path: str | Path) -> Journey:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JourneyError(f"journey file not found: {p}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise JourneyError(f"{p}: not valid TOML: {exc}") from exc
    return parse_journey(data)
