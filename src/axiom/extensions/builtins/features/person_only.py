# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A flag only a person may change (``set_by = "person"``, #1811).

Every write to such a flag (an override, a rollout, the kill switch) asks :func:`refusal` first.
Only two surfaces may change it: the command line, where the person answers ``yes`` at the
prompt (no prompt, no change), and the web, whose route requires a sign-in within
:data:`FRESH_SECONDS` before it calls the skill. An MCP tool, an agent tool or an in-process
caller is refused with the reason. A scheduled change a person made is applied later as theirs.
"""

from __future__ import annotations

from typing import Any

#: How recent a web sign-in must be to change a person-only flag. One constant on purpose: a
#: log's co-sign freshness is declared per entry type (``cosign.assurance.fresh_within``) and
#: there is no platform default to share.
FRESH_SECONDS = 300


def is_person_only(key: str) -> bool:
    from .flag_registry import registry

    flag = registry().get(key)
    return flag is not None and flag.set_by == "person"


def refusal(key: str, change: str, ctx: Any) -> str | None:
    """Why this caller may not make ``change`` to ``key``, or None when it may."""
    if not is_person_only(key):
        return None
    surface = getattr(ctx, "surface", None)
    if surface == "web":
        return None  # the route checked the sign-in's age before calling
    if surface == "cli":
        prompt = getattr(ctx, "user_prompt", None)
        if prompt is None:
            return f"flag {key!r} is changed only by a person at the prompt; nobody answered"
        answer = str(prompt(f"{change}: flag {key!r} is changed only by a person. Type yes to go on: "))
        if answer.strip().lower() != "yes":
            return f"flag {key!r} was not changed: the person did not answer yes"
        return None
    return (
        f"flag {key!r} is changed only by a person, at the command line or on the web; "
        f"not from {surface or 'software'}"
    )


def stale_sign_in(key: str, claims: dict[str, Any], *, now: float | None = None) -> str | None:
    """For the web route: why this session may not change a person-only ``key`` (a sign-in older
    than :data:`FRESH_SECONDS`, or one that says nothing of when), or None."""
    import time

    if not is_person_only(key):
        return None
    at = claims.get("auth_time")
    age = None if not isinstance(at, int | float) else (now or time.time()) - at
    if age is None or age > FRESH_SECONDS:
        return (
            f"flag {key!r} is changed only by a person who signed in within "
            f"{FRESH_SECONDS // 60} minutes: sign in again"
        )
    return None


__all__ = ["FRESH_SECONDS", "is_person_only", "refusal", "stale_sign_in"]
