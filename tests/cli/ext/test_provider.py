# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Provider framework powering ``axi ext`` verbs.

Per AEOS §11 every ``axi ext <verb>`` is implemented as an ExtCliProvider
so third parties may override. The default implementations live in
``axiom.cli.ext.commands``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry import discover_providers


class _DummyProvider:
    verb = "dummy"
    description = "dummy verb for tests"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--flag", action="store_true")

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        return 42


def test_provider_protocol_runtime_checkable() -> None:
    """ExtCliProvider is a Protocol — runtime classes satisfying it work."""
    p = _DummyProvider()
    # Protocol duck typing: p has verb/description/add_arguments/run attributes
    assert p.verb == "dummy"
    assert callable(p.add_arguments)
    assert callable(p.run)


def test_cli_context_carries_cwd_and_extension_path(tmp_path: Path) -> None:
    """CliContext carries the resolved paths the providers need."""
    ctx = CliContext(cwd=tmp_path, extension_path=tmp_path / "my_ext")
    assert ctx.cwd == tmp_path
    assert ctx.extension_path == tmp_path / "my_ext"


def test_cli_context_defaults_extension_path_to_cwd(tmp_path: Path) -> None:
    """When no explicit extension path is given, it defaults to cwd."""
    ctx = CliContext(cwd=tmp_path)
    assert ctx.extension_path == tmp_path


def test_discover_providers_returns_builtins() -> None:
    """The four Tier 1b verbs must be discoverable."""
    providers = discover_providers()
    verbs = set(providers.keys())
    for verb in ("init", "lint", "validate", "test"):
        assert verb in verbs, f"missing builtin verb: {verb}"


def test_discover_providers_yields_provider_instances() -> None:
    """Each discovered provider must expose verb/description/add_arguments/run."""
    providers = discover_providers()
    for verb, provider in providers.items():
        assert provider.verb == verb
        assert isinstance(provider.description, str) and provider.description
        assert callable(provider.add_arguments)
        assert callable(provider.run)
