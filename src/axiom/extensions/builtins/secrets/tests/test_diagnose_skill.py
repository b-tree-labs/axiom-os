# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``secrets.diagnose`` skill.

Pure unit tests using monkeypatched provider classes — no real backends.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

import pytest

from axiom.extensions.builtins.secrets.providers.protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)
from axiom.extensions.builtins.secrets.providers.registry import SecretStoreRegistry
from axiom.extensions.builtins.secrets.skills import diagnose
from axiom.infra.skills import SkillContext, SkillRegistry


_SECRET_PLAINTEXT = "super-secret-plaintext-value"


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("test.secrets.diagnose"),
        user_prompt=None,
    )


class _GoodStore:
    capabilities = Capabilities()

    def get(self, ref: SecretRef) -> Secret:
        return Secret(value=_SECRET_PLAINTEXT.encode("utf-8"))

    def put(self, ref, value): raise PermissionError
    def delete(self, ref): raise PermissionError
    def list_paths(self, prefix): return []
    def lease(self, ref, ttl): raise PermissionError
    def rotate(self, ref): raise PermissionError


class _GoodProvider(SecretStoreProvider):
    kind: ClassVar[str] = "diagtestgood"
    capabilities: ClassVar[Capabilities] = Capabilities()

    def __init__(self, config: dict) -> None:
        super().__init__(config)

    def open(self) -> SecretStore:  # type: ignore[override]
        return _GoodStore()

    def available(self) -> bool:  # type: ignore[override]
        return True


class _UnreachableProvider(SecretStoreProvider):
    kind: ClassVar[str] = "diagtestdown"
    capabilities: ClassVar[Capabilities] = Capabilities()

    def __init__(self, config: dict) -> None:
        super().__init__(config)

    def open(self) -> SecretStore:  # type: ignore[override]
        raise RuntimeError("backend unreachable")

    def available(self) -> bool:  # type: ignore[override]
        return False


@pytest.fixture
def fresh_registry(monkeypatch):
    """Swap SecretStoreRegistry._providers for an isolated dict."""
    saved = SecretStoreRegistry._providers
    SecretStoreRegistry._providers = {}
    try:
        SecretStoreRegistry.register(_GoodProvider)
        SecretStoreRegistry.register(_UnreachableProvider)
        yield SecretStoreRegistry
    finally:
        SecretStoreRegistry._providers = saved


def test_walk_all_kinds_returns_one_item_per_kind(tmp_path, fresh_registry):
    result = diagnose.run({}, _ctx(tmp_path))
    items = result.value["items"]
    kinds = {i["kind"] for i in items}
    assert kinds == {"diagtestgood", "diagtestdown"}
    assert result.value["resource"] == "diagnose"
    good = next(i for i in items if i["kind"] == "diagtestgood")
    assert good["registered"] is True
    assert good["constructible"] is True
    assert good["available"] is True
    assert good["error"] is None
    down = next(i for i in items if i["kind"] == "diagtestdown")
    assert down["available"] is False
    assert result.ok is False  # because diagtestdown unavailable


def test_walk_all_available_returns_ok(tmp_path, monkeypatch):
    saved = SecretStoreRegistry._providers
    SecretStoreRegistry._providers = {}
    try:
        SecretStoreRegistry.register(_GoodProvider)
        result = diagnose.run({}, _ctx(tmp_path))
        assert result.ok is True
        assert all(i["available"] for i in result.value["items"])
    finally:
        SecretStoreRegistry._providers = saved


def test_ref_resolves_returns_value_length(tmp_path, fresh_registry):
    ref = "diagtestgood://some/path"
    result = diagnose.run({"ref": ref}, _ctx(tmp_path))
    assert result.ok is True
    items = result.value["items"]
    assert len(items) == 1
    item = items[0]
    assert item["scheme"] == "diagtestgood"
    assert item["registered"] is True
    assert item["constructible"] is True
    assert item["available"] is True
    assert item["resolved"] is True
    assert item["value_length"] == len(_SECRET_PLAINTEXT)
    assert item["error"] is None


def test_bogus_kind_returns_unregistered(tmp_path, fresh_registry):
    result = diagnose.run({"ref": "floop://nowhere"}, _ctx(tmp_path))
    assert result.ok is False
    item = result.value["items"][0]
    assert item["scheme"] == "floop"
    assert item["registered"] is False
    assert item["resolved"] is False
    assert item["error"]


def test_unreachable_backend_returns_unavailable(tmp_path, fresh_registry):
    result = diagnose.run({"ref": "diagtestdown://kv/x"}, _ctx(tmp_path))
    assert result.ok is False
    item = result.value["items"][0]
    assert item["registered"] is True
    assert item["available"] is False
    assert item["resolved"] is False
    assert item["error"]


def test_resolved_value_never_leaks(tmp_path, fresh_registry):
    """The plaintext secret must NOT appear anywhere in the result."""
    import json
    result = diagnose.run({"ref": "diagtestgood://x"}, _ctx(tmp_path))
    serialized = json.dumps({
        "value": result.value,
        "errors": result.errors,
    }, default=str)
    assert _SECRET_PLAINTEXT not in serialized
