# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A test that reaches a real model says so with ``@pytest.mark.llm`` (#275).

The pre-push hook leaves ``llm`` tests to CI, which only works if the marker is
on every test that waits on a model. An unmarked test whose code path posts to
a model endpoint off this machine is refused at once (no retry clock) and fails
at teardown, naming the marker. A loopback endpoint is a fake server the test
started itself and is allowed.
"""

from __future__ import annotations

import time

import pytest

# TEST-NET-3: documentation-only, never routed. Nothing is ever sent there.
_REMOTE = "http://203.0.113.7/v1/chat/completions"


def test_an_unmarked_test_is_refused_at_once_and_recorded(request):
    import requests

    import axiom.llm.gateway as gw

    start = time.monotonic()
    with pytest.raises(requests.ConnectionError, match="mark"):
        gw._post_with_rate_limit_retry(requests, _REMOTE, {"messages": []}, {}, timeout=60)
    assert time.monotonic() - start < 1.0, "refused without the retry clock"
    reached = request.node._llm_reached
    assert reached == [_REMOTE]
    reached.clear()  # this test's subject is the refusal; do not fail its teardown


def test_a_loopback_fake_server_is_not_a_model(request):
    import requests

    import axiom.llm.gateway as gw

    with pytest.raises(requests.RequestException):
        gw._post_with_rate_limit_retry(
            requests, "http://127.0.0.1:9/v1/chat/completions", {}, {}, timeout=1
        )
    assert request.node._llm_reached == []


def test_a_mocked_transport_is_not_a_model(request):
    from unittest import mock

    import axiom.llm.gateway as gw

    fake = mock.Mock()
    fake.post.return_value = mock.Mock(status_code=200, headers={})
    gw._post_with_rate_limit_retry(fake, _REMOTE, {}, {}, timeout=60)
    assert request.node._llm_reached == []


@pytest.mark.llm
def test_a_marked_test_keeps_the_real_transport():
    import axiom.llm.gateway as gw

    assert gw._post_with_rate_limit_retry.__name__ == "_post_with_rate_limit_retry"
