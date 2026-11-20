# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the instrument extension: a state directory, a catalogue of stand-in kinds, a brand."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_receipt_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Receipts off, as the repo's own suite runs: every capability call passes the
    authority gate, which otherwise probes the platform database for its receipt table."""
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(path))
    return path


@pytest.fixture
def builtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for Axiom's builtins directory: a manifest here is platform code."""
    from axiom.extensions.builtins.instrument import registry

    root = tmp_path / "axiom" / "extensions" / "builtins"
    root.mkdir(parents=True)
    monkeypatch.setattr(registry, "builtins_dir", lambda: root.resolve())
    return root


@pytest.fixture
def gauges():
    from .fakes import GaugeKind

    return GaugeKind()


@pytest.fixture
def kinds(monkeypatch: pytest.MonkeyPatch, gauges) -> Iterator:
    """This process's catalogue, holding the stand-in ``gauges`` kind and nothing discovered."""
    from axiom.extensions.builtins.instrument import registry

    cat = registry.KindCatalog()
    cat.register(gauges)
    monkeypatch.setattr(registry, "catalog", lambda *, refresh=False: cat)
    yield cat


@pytest.fixture
def acme() -> Iterator[None]:
    """A consumer's distribution is active: its CLI is ``acme``."""
    from axiom.infra import branding

    branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme"))
    try:
        yield
    finally:
        branding.reset()


@pytest.fixture
def ctx(state_dir: Path):
    from axiom.extensions.builtins.instrument import skills
    from axiom.infra.skills import SkillContext, SkillRegistry

    registry = SkillRegistry()
    skills.bind(registry)
    return SkillContext(
        registry=registry, state_dir=state_dir, logger=logging.getLogger("test.instrument")
    )


@pytest.fixture
def call(ctx):
    """Invoke an instrument capability the way every surface does: through the gateway."""
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability

    def _call(capability: str, **params):
        return invoke_capability(ctx.registry, capability, params, ctx, surface=CLI_SURFACE)

    return _call
