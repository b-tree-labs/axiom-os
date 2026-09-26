# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The 2026-09-21 incident, pinned: the orchestrator's authz context is
cached for the life of the process, and its capability had a 1-hour TTL
with no renewal — every PULSE-fired dispatch after the first hour was
floor-denied EXPIRED_CAPABILITY, and four nightly backups were lost
before the fleet console surfaced it. This test ages the cached token
and asserts dispatch still authorizes."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from axiom.extensions.builtins.data_platform import _authz
from axiom.extensions.builtins.data_platform.orchestration.service import DispatchAuthz


@pytest.fixture()
def aged_context(monkeypatch):
    monkeypatch.setenv("AXIOM_ACTOR", "@pulse:test")
    monkeypatch.setattr(_authz, "_ctx", None)
    monkeypatch.setattr(_authz, "_ctx_init_failed", False)
    ctx = _authz._build_context()
    assert ctx is not None and ctx.default_capability is not None
    now = datetime.now(timezone.utc)
    ctx.default_capability = dataclasses.replace(
        ctx.default_capability,
        not_before=now - timedelta(hours=3),
        not_after=now - timedelta(hours=2),
    )
    yield ctx
    monkeypatch.setattr(_authz, "_ctx", None)


def test_dispatch_authorizes_after_the_first_hour(aged_context):
    ok = DispatchAuthz().decide({"action": "data.backup"})
    assert ok is True, (
        "a host older than the capability TTL must renew its own grant, "
        "not silently lose all scheduled authority"
    )


def test_renewed_token_is_actually_valid_now(aged_context):
    DispatchAuthz().decide({"action": "data.backup"})
    cap = aged_context.default_capability
    assert cap.is_valid_at(datetime.now(timezone.utc))
