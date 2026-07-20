# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Availability gating for CLI commands (ADR-047).

A command declares the capabilities it needs — ``requires = ["git"]`` in
its AEOS manifest, or the core ``_SUBCOMMAND_REQUIRES`` map in
``axiom_cli``. This module turns those names into an availability verdict
the dispatcher uses to hide the command from help and refuse to run it with
a reason + remedy, instead of letting it crash on a missing dependency
mid-run.
"""

from __future__ import annotations

from collections.abc import Iterable

from axiom.infra import capabilities
from axiom.infra.capabilities import Availability, Capability


def unmet_requirements(
    requires: Iterable[str] | None,
) -> list[tuple[Capability, Availability]]:
    """Return ``(capability, availability)`` for each unmet requirement.

    ``requires`` is an iterable of capability names. Unknown names are
    skipped — forward-compatible: a manifest may name a capability that a
    newer Axiom registers. (``axi ext lint`` is the place to flag typos.)
    """
    known = [cap for name in (requires or ()) if (cap := capabilities.get(name))]
    return capabilities.missing(known)


def is_available(requires: Iterable[str] | None) -> bool:
    """True if every named requirement is met (or there are none)."""
    return not unmet_requirements(requires)


def format_unavailable(
    noun: str, unmet: list[tuple[Capability, Availability]]
) -> str:
    """A user-facing block explaining why ``noun`` can't run + how to fix it."""
    lines = [f"  '{noun}' is unavailable — missing:"]
    for cap, av in unmet:
        lines.append(f"    • {cap.description or cap.name}: {av.reason or 'unavailable'}")
        if av.remedy:
            lines.append(f"      → {av.remedy}")
    return "\n".join(lines)


__all__ = ["unmet_requirements", "is_available", "format_unavailable"]
