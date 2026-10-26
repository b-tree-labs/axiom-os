# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Real sessions on a real SessionStore for the /renew tests (no mocks of the thing under test)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from axiom.infra.orchestrator.session import Session, SessionStore

ME = "@ben:test"
SOMEONE_ELSE = "@mallory:test"


def make_session(store, sid, title, *, principal=ME, hours_ago=1, messages=2, context=None, **scope):
    s = Session(session_id=sid, title=title, principal_id=principal, context=dict(context or {}), **scope)
    for i in range(messages):
        s.add_message("user" if i % 2 == 0 else "assistant", f"{title} message {i}")
    s.updated_at = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    s.title = title
    path = store.save(s)
    stamp = (datetime.now(UTC) - timedelta(hours=hours_ago)).timestamp()
    os.utime(path, (stamp, stamp))
    return s


def provider(name, tier="any"):
    return SimpleNamespace(name=name, routing_tier=tier, model="m", api_key="k", api_key_env="")


class FakeGateway:
    """A gateway in the shape the chat reads: providers, an override, an active provider."""

    def __init__(self, *providers, override=None):
        self.providers = list(providers)
        self._provider_override = override
        self.last_selected = None

    @property
    def active_provider(self):
        return self.providers[0] if self.providers else None


class Agent:
    """The attributes /renew touches on a ChatAgent, with the real interaction-mode validation."""

    INTERACTION_MODES = ("ask", "plan", "agent")

    def __init__(self, session, gateway=None):
        self.session = session
        self.gateway = gateway or FakeGateway(provider("local"))
        self._interaction_mode = "agent"
        self._session_mode = "auto"

    def set_interaction_mode(self, mode):
        if mode not in self.INTERACTION_MODES:
            raise ValueError(mode)
        self._interaction_mode = mode


def new_store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")
