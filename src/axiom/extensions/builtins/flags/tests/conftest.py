# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures: an in-memory SQLite store (never the dev database), a pinned
registry, and an environment with no stray flag variables."""

from __future__ import annotations

import contextlib

import pytest

from axiom.extensions.builtins.flags import registry as reg
from axiom.extensions.builtins.flags import store
from axiom.extensions.builtins.flags.db_models import Base
from axiom.extensions.builtins.flags.registry import FlagDef

#: Domain-neutral stand-ins for the SoilMetrix registry the ported tests used:
#: history_timeline = field_history_timeline (flat default off),
#: beta_planner = build_plans (off, protected in production),
#: gold_first_reads = zones_gold_primary (on in development only).
FLAGS = (
    FlagDef(key="history_timeline", description="timeline panels", default="off", owner="test"),
    FlagDef(key="beta_planner", description="planner UI", default="off",
            protected_in=("production",), owner="test"),
    FlagDef(key="gold_first_reads", description="read gold first", default="off",
            default_by_env={"development": "on", "staging": "off", "production": "off"},
            owner="test"),
    FlagDef(key="library", description="the Book verb", default="soon", owner="test"),
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("AXIOM_FLAG_"):
            monkeypatch.delenv(name)
    for name in ("AXIOM_ENVIRONMENT", "ENVIRONMENT", "AXIOM_SITE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AXIOM_NODE_NAME", "node-a")


@pytest.fixture(autouse=True)
def pinned_registry():
    reg.set_registry(FLAGS)
    yield {f.key: f for f in FLAGS}
    reg.set_registry(None)


@pytest.fixture()
def sqlite_store():
    """Bind the store to a private in-memory SQLite database for one test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
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
    yield store.OverrideStore()
    store.reset_provider()
    engine.dispose()
