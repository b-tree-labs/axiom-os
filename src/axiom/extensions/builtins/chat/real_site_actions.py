# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Test mode asks before an action that only makes sense on a real site.

A machine running on invented sample data (``AXIOM_SAMPLE_DATA``) answers from numbers that are not
a site's. Reading them is harmless and marked on every reply. *Acting* on them is not: sending an
operator an alert, pushing a reading to a node, binding to a site or changing a setpoint means
something only against a real site, and a demo that does it for real is the failure worth
preventing. So in test mode such a tool always asks, even when the person once chose "always allow"
or an allowlist would approve it, and the warning says why. The question is never answered by a
standing choice; "always allow" is taken as "this once" because a standing yes made on a real
site must not carry into test mode.

What counts is declared, not guessed from a verb: a tool says ``real_site_only`` on its ``ToolDef``,
or lives in one of :data:`REAL_SITE_NAMESPACES`. Reads never ask: they are what test mode is for.
"""

from __future__ import annotations

from typing import Any

#: Capability namespaces whose writes act on a real site, a real operator or real equipment.
REAL_SITE_NAMESPACES = frozenset(
    {
        "alert",
        "alerts",
        "notify",
        "notifications",
        "control",
        "daq",
        "ingest",
        "forward",
        "promote",
        "transmit",
        "deploy",
        "binding",
        "site",
        "sites",
        "fleet",
        "federation",
    }
)

#: Printed just before the approval prompt, so the prompt that follows has its reason.
WARNING = (
    "TEST MODE: this machine answers from invented sample data, not a real site. "
    '{tool} only makes sense on a real site. Choose a/r below; "always allow" is not kept in test mode.'
)


def in_test_mode() -> bool:
    from axiom.infra.serving_mode import sample_data_mode

    return sample_data_mode()


def _namespace(tool_name: str) -> str:
    return tool_name.split("__", 1)[0].split(".", 1)[0].lower()


def acts_on_a_real_site(tool_name: str, tools: dict[str, Any]) -> bool:
    """True for a tool that changes something and only makes sense against a real site."""
    tool = tools.get(tool_name)
    if tool is None:
        return False
    from axiom.infra.orchestrator.actions import ActionCategory

    if getattr(tool, "category", None) == ActionCategory.READ:
        return False
    if getattr(tool, "real_site_only", False):
        return True
    return _namespace(tool_name) in REAL_SITE_NAMESPACES


def must_ask_in_test_mode(tool_name: str, tools: dict[str, Any]) -> bool:
    """Whether this call must be put to the person now, whatever they allowed before."""
    return in_test_mode() and acts_on_a_real_site(tool_name, tools)


def warning_for(tool_name: str) -> str:
    return WARNING.format(tool=tool_name)


__all__ = [
    "REAL_SITE_NAMESPACES",
    "acts_on_a_real_site",
    "must_ask_in_test_mode",
    "in_test_mode",
    "warning_for",
]
