# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Standard AEOS conformance tests for the instrument extension."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from axiom_tests.unit_tests import ExtensionStandardTests
from axiom_tests.unit_tests.cmd import CommandTests

MANIFEST = Path(__file__).parent.parent.parent / "axiom-extension.toml"


class TestInstrumentStandard(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self) -> Path:
        return MANIFEST


class TestInstrumentCommand(CommandTests):
    """AEOS §4.3: the ``instrument`` noun and its own verbs."""

    @property
    def expected_noun(self) -> str:
        return "instrument"

    @property
    def expected_subcommands(self) -> tuple[str, ...]:
        return ("list", "status")

    @pytest.fixture
    def command_entry(self):
        from axiom.extensions.builtins.instrument.commands import cli

        return cli.get_parser()

    @pytest.fixture
    def command_manifest_block(self) -> dict:
        provides = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["extension"]["provides"]
        return next(p for p in provides if p["kind"] == "cmd")
