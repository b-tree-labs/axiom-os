# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""BoxSessionApiClient — transparent 429 retry-with-backoff (#13)."""
from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.sources.box.session_api import (
    BoxSessionApiClient,
)
from axiom.infra.ratelimit import RateLimited, RateLimitWindow

_W = RateLimitWindow(retry_after_s=0)  # 0s → no real sleep in tests


def _client(max_retries=3):
    c = BoxSessionApiClient(session_dir=None)
    c._max_retries = max_retries
    return c


def test_get_json_retries_then_succeeds():
    c = _client()
    n = {"v": 0}

    def flaky(*a, **k):
        n["v"] += 1
        if n["v"] <= 2:
            raise RateLimited(_W)
        return {"ok": True}

    c._get_json_once = flaky
    assert c.get_json("/x") == {"ok": True}
    assert n["v"] == 3            # 2 retries + success


def test_get_bytes_propagates_after_budget():
    c = _client(max_retries=3)
    n = {"v": 0}

    def always(*a, **k):
        n["v"] += 1
        raise RateLimited(_W)

    c._get_bytes_once = always
    with pytest.raises(RateLimited):
        c.get_bytes("/y")
    assert n["v"] == 4            # 1 initial + 3 retries
