# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the receipts suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _an_assistant_exists(request, monkeypatch):
    """Most tests describe a deployment that HAS an assistant.

    `discuss` is offered only where one is configured (offers.py), which is
    the "no affordance the server will not honour" rule reaching the one
    offer the remedy guard never covered. Left unpatched, whether `discuss`
    appears would depend on whether the machine running the suite happens
    to have an LLM — a test that passes or fails on the developer's laptop
    configuration is not a test. A case that cares states its own world by
    passing `can_discuss` directly.
    """
    if "real_assistant_check" in request.keywords:
        return  # this test is ABOUT the predicate, so it gets the real one

    from axiom.extensions.builtins.receipts import offers

    monkeypatch.setattr(offers, "assistant_reachable", lambda: True)
