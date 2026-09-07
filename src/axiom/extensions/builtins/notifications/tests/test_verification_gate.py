# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""B2 — DT verification gate: the channel-agnostic prediction→verify touchpoint.

Driven on InMemoryInteractiveChannel here; B3/B4 re-run this suite over SMS /
Email to prove the choreography is vendor-neutral (no special branches)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.interactive import InMemoryInteractiveChannel
from axiom.extensions.builtins.notifications.verification_gate import (
    DTVerificationGate,
    Prediction,
)


@pytest.fixture
def channel():
    return InMemoryInteractiveChannel()


def _gate(channel, sink, **kw):
    g = DTVerificationGate(channel, on_verified=sink, **kw)
    g.open(Prediction(title="end-of-run measurement", predicted_value=2.1,
                      unit="units", tolerance=0.3, detail="sample-042 in slot TPNT"))
    return g


def test_confirm_records_predicted_as_measured(channel):
    out = []
    g = _gate(channel, out.append)
    channel.inject_action("confirm", actor="@ben")
    assert g.status == "verified"
    assert out and out[0].confirmed and out[0].measured == 2.1 and out[0].in_tolerance is True


def test_measure_in_tolerance(channel):
    out = []
    g = _gate(channel, out.append)
    channel.inject_action("measure")
    channel.inject_message("2.2", author="@ben")
    assert g.status == "verified"
    assert out[0].measured == 2.2 and out[0].confirmed is False and out[0].in_tolerance is True


def test_measure_out_of_tolerance_flagged(channel):
    out = []
    _gate(channel, out.append)
    channel.inject_action("measure")
    channel.inject_message("3.0", author="@ben")  # |3.0-2.1|=0.9 > 0.3
    assert out[0].in_tolerance is False
    assert any("OUT of tolerance" in t for t in channel.texts())


def test_reject_records_no_value(channel):
    out = []
    g = _gate(channel, out.append)
    channel.inject_action("reject", actor="@ben")
    assert g.status == "rejected"
    assert out[0].rejected and out[0].measured is None


def test_non_number_during_measure_reprompts_no_sink(channel):
    out = []
    _gate(channel, out.append)
    channel.inject_action("measure")
    channel.inject_message("about two", author="@ben")
    assert not out  # no outcome yet
    channel.inject_message("2.0", author="@ben")
    assert out and out[0].measured == 2.0


def test_freeform_question_answered_before_resolution(channel):
    g = _gate(channel, lambda o: None)
    channel.inject_message("what does the twin predict?", author="@ben")
    assert any("2.1" in t for t in channel.texts())
    assert g.status == "awaiting"  # still open


def test_posts_attributed_to_agent_with_icon(channel):
    _gate(channel, lambda o: None, agent="Ben's Axi", agent_icon="https://x/a.png")
    brief = channel.posts[0]
    assert brief.author == "Ben's Axi" and brief.icon_url == "https://x/a.png"
