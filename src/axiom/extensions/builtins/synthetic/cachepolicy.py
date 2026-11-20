# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HTTP cache rules a browser applies to a response (RFC 9111), as predicates.

The incident this exists for: a page served with a Last-Modified date and no
Cache-Control is reused by a browser with no request at all (heuristic
freshness), so an access gate in front of it is never consulted. These
functions state that rule so a check can fail on it before a user does.
"""

from __future__ import annotations

from collections.abc import Mapping

_HEURISTIC_STATUSES = frozenset({200, 203, 204, 300, 301, 404, 405, 410, 414, 501})


def _get(headers: Mapping[str, str], name: str) -> str | None:
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


def parse_cache_control(value: str | None) -> dict[str, str | None]:
    """Directive map: lower-cased name to its argument (or None). Unknown tokens are kept."""
    out: dict[str, str | None] = {}
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, arg = part.partition("=")
        out[name.strip().lower()] = arg.strip().strip('"') or None
    return out


def _max_age(cc: dict[str, str | None]) -> int | None:
    raw = cc.get("max-age")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def revalidates(headers: Mapping[str, str]) -> bool:
    """True when a browser must ask the server before reusing the response."""
    cc = parse_cache_control(_get(headers, "cache-control"))
    if "no-store" in cc or "no-cache" in cc:
        return True
    age = _max_age(cc)
    return age is not None and age <= 0


def heuristically_cacheable(status: int, headers: Mapping[str, str]) -> bool:
    """True when a browser may reuse the response with no request, on a guess.

    Heuristic freshness applies only with a Last-Modified validator and no
    explicit freshness information (max-age, s-maxage, Expires) and no
    directive that forbids reuse without validation.
    """
    if status not in _HEURISTIC_STATUSES:
        return False
    if _get(headers, "last-modified") is None:
        return False
    cc = parse_cache_control(_get(headers, "cache-control"))
    if "no-store" in cc or "no-cache" in cc:
        return False
    if "max-age" in cc or "s-maxage" in cc or _get(headers, "expires") is not None:
        return False
    return True


def is_long_lived(headers: Mapping[str, str], *, minimum_seconds: int = 86400) -> bool:
    """True when the response states an explicit lifetime of at least minimum_seconds."""
    age = _max_age(parse_cache_control(_get(headers, "cache-control")))
    return age is not None and age >= minimum_seconds


POLICIES = ("revalidate", "no-heuristic", "long-lived")


def satisfies(policy: str, status: int, headers: Mapping[str, str]) -> tuple[bool, str]:
    """Check one named policy; return (ok, plain-language reason)."""
    if policy == "revalidate":
        ok = revalidates(headers)
        return ok, "must be revalidated before reuse" if ok else (
            "a browser may reuse this response without asking the server"
            + (
                " (heuristic freshness: Last-Modified and no Cache-Control)"
                if heuristically_cacheable(status, headers)
                else ""
            )
        )
    if policy == "no-heuristic":
        h = heuristically_cacheable(status, headers)
        return (not h), "explicit cache policy" if not h else (
            "Last-Modified with no Cache-Control or Expires: browsers will guess a lifetime"
        )
    if policy == "long-lived":
        ok = is_long_lived(headers)
        return ok, "explicit long lifetime" if ok else "no explicit long max-age"
    return False, f"unknown cache policy {policy!r}; expected one of {POLICIES}"
