# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""B3 — SMS as a bidirectional InteractiveChannel + inbound webhook seam, and
the generalization proof: the DT gate runs unchanged over SMS."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.interactive import (
    ApprovalOutcome,
    ChannelMessage,
    InteractiveChannel,
)
from axiom.extensions.builtins.notifications.channels.twilio_interactive import (
    TwilioInteractiveChannel,
    parse_twilio_inbound,
)
from axiom.extensions.builtins.notifications.gateway.inbound import InboundReceiver
from axiom.extensions.builtins.notifications.verification_gate import (
    DTVerificationGate,
    Prediction,
)

# --- pure inbound parsing -----------------------------------------------------

@pytest.mark.parametrize("body,kind,action", [
    ("YES", ApprovalOutcome, "confirm"),
    ("no", ApprovalOutcome, "reject"),
    ("2.4", ChannelMessage, None),
    ("what does the twin say?", ChannelMessage, None),
])
def test_parse_twilio_inbound(body, kind, action):
    parsed = parse_twilio_inbound({"Body": body, "From": "+15125550100"})
    assert isinstance(parsed, kind)
    if action:
        assert parsed.action_id == action


def test_blank_body_is_none():
    assert parse_twilio_inbound({"Body": "  ", "From": "+1"}) is None


# --- channel satisfies protocol + sends + dispatches --------------------------

def _channel(sent):
    return TwilioInteractiveChannel(to_number="+15125550100", send=lambda to, body: sent.append((to, body)))


def test_satisfies_interface_and_sends():
    sent = []
    ch = _channel(sent)
    assert isinstance(ch, InteractiveChannel)
    ch.post("hello", author="Ben's Axi")
    assert sent[0][0] == "+15125550100" and "Ben's Axi: hello" in sent[0][1]


def test_dispatch_routes_inbound_to_handlers():
    sent = []
    ch = _channel(sent)
    got = []
    ch.on_message(got.append)
    ch.dispatch({"Body": "hi there", "From": "+1999"})
    assert got and got[0].text == "hi there"


def test_inbound_receiver_routes_and_checks_signature():
    sent = []
    ch = _channel(sent)
    seen = []
    ch.on_action(seen.append)
    rx = InboundReceiver()
    rx.register("/twilio/sms", ch, verify=lambda p, sig, hdr: sig == "good")

    assert rx.handle("/unknown", {}) is False
    with pytest.raises(PermissionError):
        rx.handle("/twilio/sms", {"Body": "YES"}, signature="bad")
    assert rx.handle("/twilio/sms", {"Body": "YES", "From": "+1"}, signature="good") is True
    assert seen and seen[0].action_id == "confirm"


# --- GENERALIZATION PROOF: the B2 gate runs over SMS, unchanged ---------------

def _run_gate_over(channel, inject):
    """Drive the gate's confirm + measure flows over `channel`, where `inject`
    feeds an inbound reply. Returns the captured outcomes."""
    out = []
    gate = DTVerificationGate(channel, on_verified=out.append, agent="Ben's Axi")
    gate.open(Prediction(title="activity", predicted_value=2.1, unit="MBq", tolerance=0.3))
    inject("2.2")  # a bare number = measured value (no button needed)
    return gate, out


def test_gate_confirm_over_sms():
    sent = []
    ch = _channel(sent)
    out = []
    gate = DTVerificationGate(ch, on_verified=out.append)
    gate.open(Prediction(title="dose", predicted_value=5.0, unit="mGy", tolerance=0.5))
    ch.dispatch({"Body": "YES", "From": "+1"})  # YES → confirm
    assert gate.status == "verified" and out[0].confirmed and out[0].measured == 5.0


def test_gate_measure_over_sms_matches_in_memory_behavior():
    sent = []
    ch = _channel(sent)
    gate, out = _run_gate_over(ch, lambda b: ch.dispatch({"Body": b, "From": "+1"}))
    assert gate.status == "verified"
    assert out[0].measured == 2.2 and out[0].in_tolerance is True  # |2.2-2.1| <= 0.3
