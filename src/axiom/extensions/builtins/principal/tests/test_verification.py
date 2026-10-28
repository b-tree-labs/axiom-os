# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Setup is not complete until a real message has round-tripped.

These tests pin the property the whole capability exists for: a channel that is
*configured* is not a channel that *delivers*. A live install once ran for weeks
with a valid webhook shadowed by an empty override -- registration looked
healthy, delivery went nowhere, and nothing noticed.
"""

from __future__ import annotations

from axiom.extensions.builtins.principal.models import EndpointHealth
from axiom.extensions.builtins.principal.verify import VerificationOutcome, verify_endpoints


class _Sender:
    """Stands in for the notifications adapter; records what it was asked to do."""

    def __init__(self, results):
        self._results = results
        self.sent = []

    def __call__(self, endpoint, message):
        self.sent.append((endpoint.kind, message))
        outcome = self._results.get(endpoint.kind, True)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _endpoints():
    from axiom.extensions.builtins.principal.models import ContactEndpoint

    return [
        ContactEndpoint(kind="chat", address="room:ops"),
        ContactEndpoint(kind="email", address="p@example.test"),
    ]


def test_successful_send_marks_endpoint_verified():
    eps = _endpoints()
    send = _Sender({"chat": "rcpt-1", "email": "rcpt-2"})
    out = verify_endpoints(eps, send=send)
    assert all(e.verified_at is not None for e in eps)
    assert all(e.health is EndpointHealth.OK for e in eps)
    assert out.verified == ["chat", "email"]


def test_a_failing_endpoint_degrades_but_does_not_abort_the_run():
    eps = _endpoints()
    send = _Sender({"chat": RuntimeError("webhook 404"), "email": "rcpt-2"})
    out = verify_endpoints(eps, send=send)
    chat, email = eps
    assert chat.verified_at is None
    assert chat.health is EndpointHealth.FAILED
    assert "webhook 404" in (chat.health_reason or "")
    assert email.verified_at is not None, "one bad endpoint must not stop the others"
    assert out.degraded == ["chat"]


def test_no_receipt_is_a_failure_not_a_success():
    """A send that returns nothing proved nothing. This is the exact shape of the
    outage that motivated the capability: no exception, no delivery."""
    eps = _endpoints()
    send = _Sender({"chat": None, "email": "rcpt-2"})
    out = verify_endpoints(eps, send=send)
    assert eps[0].health is EndpointHealth.FAILED
    assert eps[0].verified_at is None
    assert "receipt" in (eps[0].health_reason or "").lower()
    assert out.verified == ["email"]


def test_outcome_reports_overall_readiness():
    eps = _endpoints()
    out = verify_endpoints(eps, send=_Sender({"chat": RuntimeError("x"), "email": "r"}))
    assert isinstance(out, VerificationOutcome)
    assert out.any_verified is True
    assert out.fully_verified is False


def test_nothing_verified_is_reported_as_not_ready():
    eps = _endpoints()
    out = verify_endpoints(eps, send=_Sender({"chat": RuntimeError("x"), "email": None}))
    assert out.any_verified is False
    assert out.fully_verified is False


def test_reverification_never_discards_a_previously_verified_endpoint():
    eps = _endpoints()
    verify_endpoints(eps, send=_Sender({"chat": "rcpt-1", "email": "rcpt-2"}))
    first = eps[0].verified_at
    verify_endpoints(eps, send=_Sender({"chat": RuntimeError("transient"), "email": "r"}))
    assert eps[0].verified_at == first, "a transient failure must not un-verify a channel"
    assert eps[0].health is EndpointHealth.DEGRADED, "but it must be visibly degraded"
