# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the switchboard tests (ADR-254).

``switchboard`` pins one small world: an optional extension ``widgets`` (off by
default) that provides the ``widget`` command and declares the ``panel`` flag;
a core command ``chat`` owned by the ``assistant`` function; two more flags.
Flags and optional blocks are pinned in-process (never discovered), overrides
live in a private SQLite store (never the dev database), the node file is a
temporary file, and the audit chain writes under a temporary state dir.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.features import flag_registry, resolver
from axiom.extensions.builtins.features.flag_registry import FlagDef
from axiom.extensions.builtins.features.skills import _node
from axiom.extensions.builtins.flags import store
from axiom.extensions.builtins.flags.db_models import Base
from axiom.infra import audit_trail
from axiom.infra import optional_extensions as oe

FLAGS = (
    FlagDef(key="panel", description="the widgets panel", default="on", owner="widgets"),
    FlagDef(key="timeline", description="timeline panels", default="off", owner="core_ext"),
    FlagDef(key="library", description="the Book verb", default="soon", owner="library_ext"),
)

OPTIONAL = {
    "widgets": {"default": "off", "in_use_when": ["widgets"], "description": "an optional widgets extension"},
}

COMMANDS = {
    "chat": {"description": "talk", "extension": "chat", "functions": ["assistant"], "optional": None},
    "data": {"description": "the data platform", "extension": "data_platform", "functions": ["data_platform"]},
    "widget": {"description": "widgets", "extension": "widgets", "functions": [],
               "optional": {"default": "off", "in_use_when": ["widgets"]}},
}


@pytest.fixture
def switchboard(tmp_path: Path, monkeypatch):
    """A pinned world: node file, state dir, flags, optional blocks, commands, SQLite overrides."""
    import os

    for name in list(os.environ):
        if name.startswith("AXIOM_FLAG_"):
            monkeypatch.delenv(name)
    for name in ("AXIOM_ENVIRONMENT", "ENVIRONMENT", "AXIOM_SITE", "AXIOM_VARIANT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AXIOM_NODE_NAME", "node-a")
    node = tmp_path / "node.toml"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXIOM_NODE_CONFIG", str(node))
    monkeypatch.setenv("AXI_STATE_DIR", str(state))

    flag_registry.set_registry(FLAGS)
    oe.set_optional_blocks(OPTIONAL)
    resolver.set_switched_off_owners(None)

    def fake_commands(*, include_off: bool = False) -> dict:
        """What discovery answers: an off extension's commands only with ``include_off``."""
        out = {k: dict(v) for k, v in COMMANDS.items()}
        if include_off:
            return out
        return {
            k: v for k, v in out.items()
            if v.get("optional") is None or oe.is_enabled(v["extension"], v["optional"])
        }

    monkeypatch.setattr(_node, "_commands", fake_commands)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextlib.contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    store.set_provider(provider)
    audit_trail._reset_for_tests()

    # The alert path (ADR-249, ADR-252): an incident store on its own SQLite, and
    # an inbox-only send context, so an alert is recorded and never delivered.
    import importlib

    from axiom.extensions.builtins.notifications import incidents
    from axiom.extensions.builtins.notifications.channels.base import ChannelAdapterRegistry
    from axiom.extensions.builtins.notifications.channels.inbox import InboxChannelAdapterProvider
    from axiom.extensions.builtins.notifications.db_models import Base as NotificationsBase
    from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
    from axiom.extensions.builtins.notifications.incidents.store import IncidentStore
    from axiom.extensions.builtins.notifications.send import SendContext

    alerts_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    NotificationsBase.metadata.create_all(alerts_engine)
    incident_store = IncidentStore(engine=alerts_engine)
    incidents.set_default_store(incident_store)
    inbox = InMemoryInboxStore()
    channels = ChannelAdapterRegistry()
    channels.register(InboxChannelAdapterProvider(store=inbox))
    skills_send = importlib.import_module("axiom.extensions.builtins.notifications.skills.send")
    monkeypatch.setattr(skills_send, "_CTX", SendContext(registry=channels, inbox_store=inbox))
    monkeypatch.setattr("axiom.extensions.builtins.notifications.preferences.resolve_role", lambda r: r)
    # send() also reads the process-wide recipient preference store. Left alone,
    # it is whatever an earlier test in this worker left (a disposed engine, the
    # machine's database), and the alert is dropped with OperationalError (#1940).
    from axiom.extensions.builtins.notifications import preferences

    monkeypatch.setattr(preferences, "_DEFAULT_STORE", preferences.InMemoryRecipientPreferenceStore())

    yield {"node": node, "state": state, "store": store.OverrideStore(), "engine": engine,
           "incidents": incident_store, "inbox": inbox}
    incidents.set_default_store(None)
    alerts_engine.dispose()
    audit_trail._reset_for_tests()
    store.reset_provider()
    engine.dispose()
    flag_registry.set_registry(None)
    oe.set_optional_blocks(None)
    resolver.set_switched_off_owners(None)


def mcp(tool: str, args: dict, state_dir: Path, *, bind=None) -> dict:
    """Dispatch one MCP tool the way the aggregated server does."""
    import asyncio

    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.infra.skills import SkillContext, SkillRegistry

    if bind is None:
        from axiom.extensions.builtins.features.skills import bind
    reg = SkillRegistry()
    bind(reg)
    contrib = skill_tool_contribution(
        reg, ctx_factory=lambda: SkillContext(registry=reg, state_dir=state_dir, logger=logging.getLogger("t"))
    )
    return asyncio.run(contrib.dispatch[tool](args))


def web(monkeypatch, *, sub="u9", roles=(), site="acct"):
    """A test client with both faces mounted: /api/v1/features and the /api/v1/flags alias."""
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.features import api
    from axiom.extensions.builtins.flags import api as alias

    claims = {"sub": sub, "roles": list(roles), "site": site} if sub else {}
    monkeypatch.setattr(api, "_claims", lambda request: claims)
    router = APIRouter()
    api.register_routes(router)
    alias.register_routes(router)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


# ------------------------------------------------------------------ the loop, phase 2 (LOOP-F)

#: Flags the cohort and rollout tests use, beside the switchboard's own. ``by_account``
#: buckets by account; every other flag by the principal (the documented default).
LOOP_FLAGS = (
    FlagDef(key="beta_editor", description="the new editor", default="off", owner="core_ext"),
    FlagDef(key="fast_search", description="the new search", default="off", owner="core_ext"),
    FlagDef(key="guarded", description="protected in production", default="off",
            protected_in=("production",), owner="core_ext"),
    FlagDef(key="by_account", description="rolled out per account", default="off",
            owner="core_ext", bucket_by="account"),
)


@pytest.fixture
def loop(switchboard):
    """The switchboard world plus the loop flags, a pinned attribute catalogue and the
    targeting store (cohorts, rollouts, kill switches) on the same private SQLite."""
    from axiom.extensions.builtins.features import attributes

    flag_registry.set_registry(FLAGS + LOOP_FLAGS)
    attributes.set_catalogue(None)  # the platform attributes only, no discovery
    attributes.set_extension_attributes(())
    yield {**switchboard, "targeting": store.TargetingStore()}
    attributes.set_extension_attributes(None)
    attributes.set_catalogue(None)


def svc(**kw):
    """A flag service bound to the test store (overrides and targeting)."""
    return resolver.FlagService(**kw)


# ------------------------------------------------------------------ every rollout mode (LOOP-F14 to F23)

#: Flags the rollout-mode tests add: a multivariate flag (F17), a prerequisite chain (F20),
#: a kill-switch kind (F22) and a flag that declares its own approvals (F23).
MODE_FLAGS = (
    FlagDef.from_mapping({"key": "layout", "description": "the page layout",
                          "variants": {"compact": "compact", "wide": {"columns": 3}, "ratio": 0.5},
                          "default": "compact"}, owner="core_ext"),
    FlagDef.from_mapping({"key": "checkout", "description": "needs the new editor",
                          "requires": [{"flag": "beta_editor", "value": "on"}]}, owner="core_ext"),
    FlagDef.from_mapping({"key": "checkout_v2", "description": "needs checkout",
                          "requires": [{"flag": "checkout", "value": ["on", "soon"]}]}, owner="core_ext"),
    FlagDef.from_mapping({"key": "ops_brake", "description": "stop the sync", "kind": "kill_switch"},
                         owner="core_ext"),
    FlagDef.from_mapping({"key": "prod_guard", "description": "two people in production",
                          "approvals": {"production": {"approvals": 1, "roles": ["release_manager"]}}},
                         owner="core_ext"),
)


@pytest.fixture
def modes(loop, monkeypatch):
    """The loop world plus the rollout-mode flags; nothing read from the machine's settings."""
    from axiom.extensions.builtins.features import approvals, attributes, cohort_sources, progressive

    for name in ("AXIOM_SITE_TZ", "AXIOM_DIRECTORY_PROVIDER", "AXIOM_DIRECTORY_LOCAL_FILE",
                 "AXIOM_DIRECTORY_ROLE_MAP"):
        monkeypatch.delenv(name, raising=False)
    flag_registry.set_registry(FLAGS + LOOP_FLAGS + MODE_FLAGS)
    attributes.set_site_settings({})
    approvals.set_environment_policies({})
    yield loop
    attributes.set_site_settings(None)
    approvals.set_environment_policies(None)
    approvals.set_role_resolver(None)
    cohort_sources.reset_sources()
    progressive.reset_gate_kinds()
    progressive.set_skill_registry(None)


def run(capability: str, params: dict, state_dir: Path, *, actor: str = "@ana", surface: str = "cli"):
    """Invoke one features capability through the chokepoint as ``actor`` on ``surface``."""
    from axiom.extensions.builtins.features.skills import bind
    from axiom.infra.skill_dispatch import invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    ctx = SkillContext(registry=reg, state_dir=state_dir, logger=logging.getLogger("t"), actor=actor)
    return invoke_capability(reg, capability, params, ctx, surface=surface)
