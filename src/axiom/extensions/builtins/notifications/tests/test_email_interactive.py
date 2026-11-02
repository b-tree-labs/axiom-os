# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""B4 — Email InteractiveChannel + the 4th-transport generalization check:
the DT gate runs unchanged over email."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.email.interactive import (
    EmailInteractiveChannel,
    parse_email_inbound,
)
from axiom.extensions.builtins.notifications.channels.interactive import (
    ApprovalOutcome,
    ChannelMessage,
    InteractiveChannel,
)
from axiom.extensions.builtins.notifications.gateway.inbound import InboundReceiver
from axiom.extensions.builtins.notifications.verification_gate import (
    DTVerificationGate,
    Prediction,
)


def _channel(sent):
    return EmailInteractiveChannel(
        to_address="op@example.org", from_address="axi@example.org",
        send=lambda to, subject, body, thread_id: sent.append((to, subject, body, thread_id)),
    )


@pytest.mark.parametrize("body,kind,action", [
    ("YES", ApprovalOutcome, "confirm"),
    ("No, not yet", ApprovalOutcome, "reject"),
    ("2.4 units", ChannelMessage, None),
])
def test_parse_email_inbound_first_line(body, kind, action):
    parsed = parse_email_inbound({"body": body, "from": "op@example.org"})
    assert isinstance(parsed, kind)
    if action:
        assert parsed.action_id == action


def test_parse_strips_quoted_reply():
    body = "2.2\n\nOn Tue, Jun 17, Axi wrote:\n> Twin predicts 2.1 units"
    parsed = parse_email_inbound({"body": body, "from": "op@example.org"})
    assert isinstance(parsed, ChannelMessage) and parsed.text == "2.2"


def test_threads_via_in_reply_to():
    parsed = parse_email_inbound({"body": "hi", "from": "op@example.org", "in_reply_to": "<msg-1>"})
    assert parsed.thread_id == "<msg-1>"


def test_satisfies_interface_and_sends():
    sent = []
    ch = _channel(sent)
    assert isinstance(ch, InteractiveChannel)
    ch.post("hello", author="My Axi")
    assert sent[0][0] == "op@example.org" and "My Axi" in sent[0][1] and sent[0][2] == "hello"


def test_dispatch_routes_inbound():
    sent = []
    ch = _channel(sent)
    seen = []
    ch.on_action(seen.append)
    ch.dispatch({"body": "YES", "from": "op@example.org"})
    assert seen and seen[0].action_id == "confirm"


def test_inbound_receiver_routes_email():
    sent = []
    ch = _channel(sent)
    got = []
    ch.on_message(got.append)
    rx = InboundReceiver()
    rx.register("/email/inbound", ch)
    assert rx.handle("/email/inbound", {"body": "3.1", "from": "op@example.org"}) is True
    assert got[0].text == "3.1"


# --- GENERALIZATION PROOF: the gate runs over email ---------------------------

def test_gate_confirm_over_email():
    sent = []
    ch = _channel(sent)
    out = []
    gate = DTVerificationGate(ch, on_verified=out.append)
    gate.open(Prediction(title="dose", predicted_value=5.0, unit="mGy", tolerance=0.5))
    ch.dispatch({"body": "YES", "from": "op@example.org"})
    assert gate.status == "verified" and out[0].confirmed


def test_gate_measure_over_email():
    sent = []
    ch = _channel(sent)
    out = []
    gate = DTVerificationGate(ch, on_verified=out.append)
    gate.open(Prediction(title="activity", predicted_value=2.1, unit="units", tolerance=0.3))
    ch.dispatch({"body": "2.2", "from": "op@example.org"})
    assert out[0].measured == 2.2 and out[0].in_tolerance is True
