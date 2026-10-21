# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What Rivet does with a red check, and who reviews the change.

A mechanical failure has a known repair and a resubmit. Anything else is
named and left for a person. A review is never done by the model that wrote
the change. These functions do not call a model and do not push. A harness
that has the Axiom MCP tools runs the repair and the review itself.
"""

from __future__ import annotations

from typing import Any


def plan_repair(log: str, check: str = "") -> dict[str, Any]:
    """The repair for a failure Rivet has already seen, or an escalation.

    ``resubmit`` is never ``update-branch``. Updating a branch through the
    host's API skips the pre-push hook, which is how a repairable failure
    lands on GitHub again.
    """
    text = f"{check} {log}".lower()
    if "context files drifted" in text or "agents.md" in text:
        return _plan(
            "context-sync",
            "The generated context files drifted from AGENTS.md.",
            ["axi context sync"],
            "push",
        )
    if "ruff" in text:
        return _plan(
            "ruff-fix",
            "Ruff reported a finding it can fix.",
            ["ruff check src tests conftest.py --select E,F,W --ignore E501 --fix"],
            "push",
        )
    if "barely ran" in text or "too short to prove" in text:
        return _plan(
            "short-run",
            "The run was too short to prove the property, and nothing torn was reported.",
            [],
            "rerun",
        )
    if "unexpected root" in text:
        return _stop(
            "stray-root",
            "A directory at the repo root is not part of the tree. It is not deleted from here.",
        )
    if "forbidden term" in text or "name a deployment" in text or "names a customer" in text:
        return _stop(
            "customer-name",
            "The platform tree names a customer or a deployment. That is rewritten by a person, not by a fixer.",
        )
    return _stop("unknown", "No mechanical repair is known for this failure.")


def choose_reviewer(author_model: str, models: list[str]) -> dict[str, Any]:
    """A review model that is not the one that wrote the change."""
    author = (author_model or "").strip()
    roster = []
    for model in models:
        name = (model or "").strip()
        if name and name not in roster:
            roster.append(name)
    others = [name for name in roster if name != author]
    if not author:
        return {"ok": False, "reviewer": "", "reason": "name the model that wrote the change"}
    if not others:
        return {
            "ok": False,
            "reviewer": "",
            "reason": f"the review needs a model other than {author}",
        }
    return {"ok": True, "reviewer": others[0], "author_model": author, "reason": ""}


def _plan(pattern: str, detail: str, commands: list[str], resubmit: str) -> dict[str, Any]:
    return {
        "ok": True,
        "pattern": pattern,
        "detail": detail,
        "commands": commands,
        "resubmit": resubmit,
        "escalate": False,
    }


def _stop(pattern: str, detail: str) -> dict[str, Any]:
    return {
        "ok": True,
        "pattern": pattern,
        "detail": detail,
        "commands": [],
        "resubmit": "none",
        "escalate": pattern == "unknown",
    }


__all__ = ["choose_reviewer", "plan_repair"]
