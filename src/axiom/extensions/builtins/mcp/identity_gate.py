# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MCP identity gate (ADR-114 §1) — enforce ``allowed_principals`` at dispatch.

The *who* control, distinct from the *whether* control (GUARD + site rules,
ADR-114 §2). Before a tool handler runs, the caller principal must match the
tool's ``allowed_principals`` Matrix-style patterns; otherwise the call is
refused (never dispatched).

Caller resolution: the transport supplies the caller. stdio runs as the local
owner (:func:`axiom.infra.principal.local_handle`); an authenticated transport
(HTTP bearer, ADR-038 §9) stamps the subject into ``AXIOM_MCP_CLIENT_PRINCIPAL``.
Absent identity on any transport falls back to the local owner — which the
owner-scoped defaults still admit, and a non-owner pattern still refuses.

Fail-closed: an unknown tool, or one with no patterns, is treated as owner-only.
"""
from __future__ import annotations

import os

# Default patterns for a tool that declares none — "any principal in the local
# context" (the owner on a single-user node), matching the manifest tool default
# (spec-builtin-mcp-server.md §8). A tool tightens with `@<owner>:local` or a
# specific `@name:context`, or widens with an explicit list.
DEFAULT_PATTERNS: tuple[str, ...] = ("@*:local",)


def caller_principal() -> str:
    """The acting caller's ``@name:context`` handle for this MCP session."""
    stamped = os.environ.get("AXIOM_MCP_CLIENT_PRINCIPAL", "").strip()
    if stamped:
        return stamped
    from axiom.infra.principal import local_handle

    return local_handle()


def _split(handle: str) -> tuple[str, str]:
    """``@name:context`` -> (name, context); tolerant of a missing ``@``/``:``."""
    h = handle[1:] if handle.startswith("@") else handle
    name, _, context = h.partition(":")
    return name, context


def _segment_matches(pattern: str, value: str) -> bool:
    # Matrix-style: a segment is either an exact match or the whole-segment "*".
    return pattern == "*" or pattern == value


def principal_admitted(caller: str, patterns: tuple[str, ...]) -> bool:
    """True iff ``caller`` matches at least one pattern. Empty patterns → deny."""
    if not patterns:
        return False
    cn, cc = _split(caller)
    for pat in patterns:
        pn, pc = _split(pat)
        if _segment_matches(pn, cn) and _segment_matches(pc, cc):
            return True
    return False


def refusal_reason(tool_name: str, caller: str, patterns: tuple[str, ...]) -> str | None:
    """``None`` when the caller may invoke ``tool_name``; a refusal string otherwise.

    Fail-closed: no patterns for a tool → owner-only.
    """
    effective = patterns or (caller_owner_default(),)
    if principal_admitted(caller, effective):
        return None
    return (
        f"caller {caller!r} is not permitted to call {tool_name!r} "
        f"(allowed_principals={list(effective)})"
    )


def caller_owner_default() -> str:
    """The owner handle used as the fail-closed default when a tool has no patterns."""
    from axiom.infra.principal import local_handle

    return local_handle()
