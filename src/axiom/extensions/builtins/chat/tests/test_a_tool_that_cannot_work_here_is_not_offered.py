# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A tool is offered in chat only where it can work, by the capability check commands already use.

Seen live on a development node: chat offered an extension's five read tools that need a transport
the node did not have. Every call answered "not configured", and a small model, given 105 tools,
picked those. A command already declares what it needs (``requires``, ADR-047) and is hidden where a
need is unmet. A manifest tool now says the same thing the same way, and chat reads it. An
extension contributes the probe through the ``axiom.capabilities`` entry-point group.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import tools as chat_tools
from axiom.infra import capabilities

MANIFEST = """
[extension]
name = "demo"
version = "0.1.0"

[[extension.provides]]
kind = "tool"
name = "demo_read"
entry = "json:dumps"
description = "reads the demo store"
idempotent = true
requires = ["demo-store"]
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "axiom-extension.toml").write_text(MANIFEST, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _fresh_capabilities():
    capabilities.clear_cache()
    yield
    capabilities.clear_cache()
    capabilities._REGISTRY.pop("demo-store", None)


def _contributed(monkeypatch, available: bool) -> None:
    """The extension's probe, contributed through the entry-point group, as an installed one is."""
    probe = capabilities.Capability(
        "demo-store",
        lambda: capabilities.Availability(available, reason="no demo store here", remedy="configure it"),
        "the demo store",
    )
    point = SimpleNamespace(name="demo", load=lambda: (lambda: [probe]))
    monkeypatch.setattr(capabilities, "_entry_points", lambda: [point])
    capabilities._REGISTRY.pop("demo-store", None)
    capabilities.clear_cache()


def test_a_tool_whose_declared_need_is_unmet_here_is_not_offered(root, monkeypatch):
    _contributed(monkeypatch, available=False)
    assert "demo_read" not in chat_tools._scan_manifest_tools(roots=[root])
    _contributed(monkeypatch, available=True)
    assert "demo_read" in chat_tools._scan_manifest_tools(roots=[root])
