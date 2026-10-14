# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The Human<>Agent comms scenario, end-to-end on an in-memory channel:
TRIAGE posts an OOM incident with a proposed reversible fix → a human quizzes
the agent → approves → TIDY applies and reports. Vendor-neutral (no Slack).
"""

from __future__ import annotations

from axiom.extensions.builtins.diagnostics.incident_comms import IncidentConversation
from axiom.extensions.builtins.diagnostics.safety import (
    SEVERITY_CRITICAL,
    Finding,
)
from axiom.extensions.builtins.notifications.channels.interactive import (
    InMemoryInteractiveChannel,
)


def _oom_finding():
    return Finding(
        check_name="diagnostics.workload_crashloop",
        severity=SEVERITY_CRITICAL,
        title="Pod crash-looping: langfuse/langfuse-clickhouse-shard0-0 (OOMKilled)",
        detail="CrashLoopBackOff after 7059 restarts; last reason OOMKilled.",
        remediation="Staged a reversible memory-limit bump to 16 GiB for review.",
        metadata={
            "pod": "langfuse/langfuse-clickhouse-shard0-0",
            "reason": "OOMKilled",
            "restarts": 7059,
            "oom": True,
            "remediation_plan": {
                "namespace": "langfuse",
                "old_limit_bytes": 1536 * 1024**2,
                "new_limit_bytes": 16 * 1024**3,
                "reversible": True,
            },
        },
    )


def test_opening_posts_an_actionable_brief_and_an_approval():
    ch = InMemoryInteractiveChannel()
    conv = IncidentConversation(ch, responder=lambda q, ctx: "answer", remediator=lambda p: {"ok": True})
    conv.open(_oom_finding())

    blob = "\n".join(ch.texts())
    # the brief answers the reviewer's obvious questions up front
    assert "langfuse/langfuse-clickhouse-shard0-0" in blob
    assert "OOMKilled" in blob
    assert "7059" in blob
    assert "16" in blob and "reversible" in blob.lower()
    # an approval request was posted (Approve/Deny affordance)
    assert any(p.kind == "approval" for p in ch.posts)
    assert conv.status == "awaiting_approval"


def test_human_question_is_answered_by_the_agent_in_thread():
    ch = InMemoryInteractiveChannel()
    asked = {}

    def responder(question, ctx):
        asked["q"] = question
        return f"Current limit is {ctx['remediation_plan']['old_limit_bytes'] // 1024**2} MiB."

    conv = IncidentConversation(ch, responder=responder, remediator=lambda p: {"ok": True})
    conv.open(_oom_finding())
    ch.inject_message("what is the current limit?")

    assert asked["q"] == "what is the current limit?"
    assert any("1536 MiB" in t for t in ch.texts())


def test_approval_triggers_remediation_and_posts_outcome():
    ch = InMemoryInteractiveChannel()
    applied = {}

    def remediator(plan):
        applied["plan"] = plan
        return {"ok": True, "verified": True, "new_limit_gib": 16}

    conv = IncidentConversation(ch, responder=lambda q, ctx: "", remediator=remediator)
    conv.open(_oom_finding())
    ch.inject_action("approve", actor="@ben")

    assert applied["plan"]["new_limit_bytes"] == 16 * 1024**3
    assert conv.status == "applied"
    assert any("@ben" in t and "approv" in t.lower() for t in ch.texts())
    assert any("verified" in t.lower() or "16" in t for t in ch.texts())


def test_denial_does_not_remediate():
    ch = InMemoryInteractiveChannel()
    called = {"n": 0}

    def remediator(plan):
        called["n"] += 1
        return {}

    conv = IncidentConversation(ch, responder=lambda q, ctx: "", remediator=remediator)
    conv.open(_oom_finding())
    ch.inject_action("deny", actor="@ben")

    assert called["n"] == 0
    assert conv.status == "denied"


def test_post_resolution_actions_are_ignored():
    ch = InMemoryInteractiveChannel()
    called = {"n": 0}
    conv = IncidentConversation(ch, responder=lambda q, ctx: "", remediator=lambda p: called.__setitem__("n", called["n"] + 1))
    conv.open(_oom_finding())
    ch.inject_action("approve")
    ch.inject_action("approve")  # double-click / late click must not re-apply
    assert called["n"] == 1
