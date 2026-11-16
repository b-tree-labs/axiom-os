# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The feedback world: the loop's flag world (features' ``loop`` fixture: a private SQLite flag
store, an inbox-only send context, a temporary state dir) plus a private SQLite feedback store,
pinned settings, this extension's own flags and one capability of an extension that declares
``beta_editor``."""

from __future__ import annotations

import contextlib
import logging
import tomllib
from pathlib import Path

import pytest

from axiom.extensions.builtins.features import flag_registry
from axiom.extensions.builtins.features.flag_registry import FlagDef
from axiom.extensions.builtins.features.tests.conftest import (  # noqa: F401 - fixtures used by name
    FLAGS,
    LOOP_FLAGS,
    loop,
    switchboard,
)
from axiom.extensions.builtins.feedback import service, store
from axiom.extensions.builtins.feedback.db_models import Base
from axiom.infra import capability_outcomes
from axiom.infra.skill_dispatch import invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

MANIFEST = Path(__file__).resolve().parents[1] / "axiom-extension.toml"
FEEDBACK_FLAGS = tuple(FlagDef.from_mapping(f, owner="feedback")
                       for f in tomllib.loads(MANIFEST.read_text())["flags"])


def _export(params, ctx):
    """A capability of ``core_ext`` (the extension that declares ``beta_editor``) that fails."""
    return SkillResult(ok=False, errors=["export timed out after 30 s"])


def _search(params, ctx):
    return SkillResult(value={"hits": []})


@pytest.fixture
def fb(loop, monkeypatch):  # noqa: F811 - the imported fixture, requested by name
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    flag_registry.set_registry(FLAGS + LOOP_FLAGS + FEEDBACK_FLAGS)
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
    service.set_settings({})
    capability_outcomes.reset()
    monkeypatch.setattr(service, "release", lambda: "0.71.0")
    # beta_editor is on for ana and bo by a user override: they were exposed on purpose.
    from axiom.extensions.builtins.features.resolver import FlagService

    for user in ("ana", "bo", "cy", "di", "ana-agent"):
        FlagService().set_override("user", user, "beta_editor", "on", by="@t")
    yield loop
    store.set_provider(None)
    service.set_settings(None)
    capability_outcomes.reset()
    engine.dispose()


def registry() -> SkillRegistry:
    from axiom.extensions.builtins.feedback.skills import bind

    reg = SkillRegistry()
    bind(reg)
    reg.register_skill(SkillSpec(name="core_ext.export", fn=_export, risk="read", requires=()), mutating=False)
    reg.register_skill(SkillSpec(name="core_ext.search", fn=_search, risk="read", requires=()), mutating=False)
    return reg


def call(capability: str, params: dict, state: Path, *, actor: str = "@ana", surface: str = "mcp",
         reg: SkillRegistry | None = None):
    """One call through the dispatch chokepoint as ``actor`` on ``surface``."""
    reg = reg or registry()
    ctx = SkillContext(registry=reg, state_dir=state, logger=logging.getLogger("t"), actor=actor)
    return invoke_capability(reg, capability, params, ctx, surface=surface)


def report(state: Path, actor: str = "@ana", **params):
    params.setdefault("verdict", "bad")
    return call("feedback.report", params, state, actor=actor)
