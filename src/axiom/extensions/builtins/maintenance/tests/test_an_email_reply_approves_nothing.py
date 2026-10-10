# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A reply to the update notice approves nothing (ADR-179 §4: a reply is never the proof).

The ``From`` of an email is whatever the sender typed, and "yes" says nothing
about what is being agreed to. So a reply, however worded and however threaded,
must not create, sign or approve anything; only a signed one-time link can.

Real processes as in the approval-link tests: the edge (relay, links), the
node's ``maintenance poll``, issued keys. The reply goes in through HERALD's
own inbound path: the webhook receiver and the email channel's ``dispatch``.
"""

from __future__ import annotations

import json
import sys

import pytest

from axiom.extensions.builtins.data_platform.ingest_sink.maintenance_box import MaintenanceBox
from axiom.extensions.builtins.notifications.approval_bridge import (
    bind_gate_to_channel,
    notify_pending,
)
from axiom.extensions.builtins.notifications.channels.email.interactive import (
    EmailInteractiveChannel,
)
from axiom.extensions.builtins.notifications.gateway.inbound import InboundReceiver
from axiom.governance import Classification
from axiom.infra.orchestrator.actions import Action
from axiom.infra.orchestrator.approval import ApprovalGate

from .test_remote_maintenance_end_to_end import SITE, _cli, _poll, _post_request, _results, _sign

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shares the POSIX end-to-end fixture")

LEAD = "lead@site-a.example"
REPLIES = (
    "yes",
    "YES, go ahead",
    "approve",
    "Approve: please install it",
    "ok",
    "y",
)


def _box_requests(world) -> list[dict]:
    return MaintenanceBox(world["edge_maint"]).requests(SITE)


def _audit_events(world) -> list[str]:
    path = world["node_state"] / "maintenance" / "audit.jsonl"
    return [json.loads(x)["event"] for x in path.read_text().splitlines()] if path.exists() else []


def test_a_reply_to_the_update_notice_creates_signs_and_approves_nothing(world):
    # The notice's own request, waiting at the node as it would after the ladder's first rung.
    change = _sign(world, action="restart_service", params={"service": "collector"})
    assert _post_request(world, change)[0] == 200
    _poll(world)
    rid = change["request"]["id"]
    assert _results(world)[rid]["status"] == "pending"
    before_box, before_audit = _box_requests(world), _audit_events(world)

    # The reply arrives through HERALD's inbound path, threaded to the notice,
    # "from" the very person the notice went to.
    channel = EmailInteractiveChannel(to_address=LEAD, from_address="updates@platform.example",
                                      send=lambda *a, **k: None)
    seen: list = []
    channel.on_action(seen.append)
    rx = InboundReceiver()
    rx.register("/email/inbound", channel)
    for reply in REPLIES:
        assert rx.handle("/email/inbound", {"from": LEAD, "body": f"{reply}\n\nOn Thu, updates wrote:\n> Approve: https://x",
                                            "in_reply_to": f"<notice-{rid}>"}) is True
    _poll(world)

    assert _box_requests(world) == before_box, "a reply put something in the site's mailbox"
    assert _audit_events(world)[len(before_audit):] == [], "the node did something because of a reply"
    assert [p["id"] for p in json.loads(_cli(world, "pending", "--json").stdout)["pending"]] == [rid]
    assert not world["marker"].exists(), "a change ran because of a reply"
    # Whatever the channel made of the replies, none of it names a decision on anything.
    assert all(":" not in o.action_id for o in seen)


def test_an_email_channel_cannot_be_bound_to_the_approval_gate(world, tmp_path):
    """The bridge that turns a channel's answer into an approval refuses a channel that cannot say who answered."""
    gate = ApprovalGate()
    channel = EmailInteractiveChannel(to_address=LEAD, send=lambda *a, **k: None)
    with pytest.raises(ValueError, match="cannot authenticate"):
        bind_gate_to_channel(gate, channel)
    with pytest.raises(ValueError, match="cannot authenticate"):
        notify_pending(gate.submit(Action(name="node.update", params={"version": "1.1.0"})), channel,
                       classification=Classification.INTERNAL, channel_ceiling=Classification.INTERNAL)


def test_even_a_reply_spelling_the_gate_s_own_option_id_is_not_an_approval(world):
    """The bridge's ids are ``approve:<id>``; typing one into a reply is just text."""
    gate = ApprovalGate()
    action = gate.submit(Action(name="node.update", params={"version": "1.1.0"}))
    channel = EmailInteractiveChannel(to_address=LEAD, send=lambda *a, **k: None)
    outcomes: list = []
    channel.on_action(outcomes.append)
    channel.dispatch({"from": LEAD, "body": f"approve:{action.action_id}"})
    assert outcomes == []
    assert gate.get(action.action_id).status.value == "pending"
