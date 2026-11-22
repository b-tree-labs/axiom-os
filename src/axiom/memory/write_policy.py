# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dual write policies + shared-tier transformation (#39).

Per Collaborative Memory §4: fragments written to a shared tier go
through a transformation hook (redact / anonymize / paraphrase /
classify) before crossing the private→shared boundary. Private tier
always gets the original; shared tier gets the sanitized variant.

Transformations are pure functions (fragment → fragment). They can
be composed via `compose_transforms([t1, t2, ...])`.

Write scope can be computed from the policy coordinate (#38) via
scope_from_policy — this is how the policy layer decides where a
fragment goes.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from enum import Enum

from axiom.medallion.export import pseudonymize

from .fragment import MemoryFragment
from .policy import PolicyCoord, resolve

# ---------------------------------------------------------------------------
# Scope + transform protocol
# ---------------------------------------------------------------------------


class WriteScope(str, Enum):
    PRIVATE = "private"
    SHARED = "shared"


Transform = Callable[[MemoryFragment], MemoryFragment]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def write_fragment(
    fragment: MemoryFragment,
    scope: WriteScope,
    private_store: Callable[[MemoryFragment], None],
    shared_store: Callable[[MemoryFragment], None],
    transform: Transform | None = None,
) -> None:
    """Write a fragment to the appropriate tier(s).

    - Private scope: writes only to private_store.
    - Shared scope: writes original to private_store AND transformed
      (via `transform` if provided) to shared_store. The private
      tier keeps the authoritative record; the shared tier gets the
      sanitized view.
    """
    private_store(fragment)
    if scope is WriteScope.SHARED:
        shared = transform(fragment) if transform is not None else fragment
        shared_store(shared)


# ---------------------------------------------------------------------------
# Built-in transforms
# ---------------------------------------------------------------------------


def anonymize_principal(fragment: MemoryFragment) -> MemoryFragment:
    """Replace provenance.principal_id with a deterministic pseudonym.

    Preserves the ADR-035 accountability fields verbatim — anonymizing
    the actor for shared-tier projection MUST NOT strip the binding to
    the accountable human, or the federation chain breaks.
    """
    anon_id = pseudonymize(fragment.provenance.principal_id)
    new_prov = dataclasses.replace(fragment.provenance, principal_id=anon_id)
    return dataclasses.replace(fragment, provenance=new_prov, signature=None)


def redact_pattern(
    fragment: MemoryFragment,
    pattern: re.Pattern,
    replacement: str,
) -> MemoryFragment:
    """Apply a regex substitution to every string value in content."""
    new_content = _redact_in(fragment.content, pattern, replacement)
    return dataclasses.replace(fragment, content=new_content, signature=None)


def _redact_in(obj, pattern, replacement):
    """Recursively apply regex substitution in strings within content."""
    if isinstance(obj, str):
        return pattern.sub(replacement, obj)
    if isinstance(obj, dict):
        return {k: _redact_in(v, pattern, replacement) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_in(v, pattern, replacement) for v in obj]
    return obj


def compose_transforms(transforms: list[Transform]) -> Transform:
    """Compose a list of transforms into one (applied left-to-right)."""
    def _composed(f: MemoryFragment) -> MemoryFragment:
        for t in transforms:
            f = t(f)
        return f

    return _composed


# ---------------------------------------------------------------------------
# Policy-driven scope resolution
# ---------------------------------------------------------------------------


def scope_from_policy(
    coord: PolicyCoord,
    user: str,
    agent: str,
    at: str,
) -> WriteScope:
    """Resolve write scope from the policy coordinate.

    Policy rule key is `"write"`. Values: "shared" | "private" |
    anything else defaults to private (fail-safe).
    """
    effective = resolve(coord, user=user, agent=agent, at=at)
    write_rule = effective.get("write", "private")
    if write_rule == "shared":
        return WriteScope.SHARED
    return WriteScope.PRIVATE
