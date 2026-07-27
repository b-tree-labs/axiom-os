# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``CommandTests`` — base conformance for cmd capabilities (AEOS §4.3)."""

from __future__ import annotations

import argparse
from typing import Any

import pytest


class CommandTests:
    """Conformance for an AEOS ``cmd`` capability.

    Subclasses provide the CLI entrypoint (a callable or argparse-compatible
    object). The base checks that a ``noun`` is declared, subcommands are
    registered, and argument parsing does not crash for a trivial invocation.
    """

    # ---- Overridable fixtures -------------------------------------------

    @pytest.fixture
    def command_entry(self) -> Any:
        """Return the command entry object under test (override).

        Typical return value: the ``cli`` attribute of a commands module,
        either an argparse ``ArgumentParser`` or a callable that accepts
        ``argv``.
        """
        raise NotImplementedError("subclasses of CommandTests must override command_entry")

    @pytest.fixture
    def command_manifest_block(self) -> dict[str, Any] | None:
        return None

    # ---- Capability properties -----------------------------------------

    @property
    def expected_noun(self) -> str | None:
        """The noun this command group binds to (e.g. ``enrollment``)."""
        return None

    @property
    def expected_subcommands(self) -> tuple[str, ...]:
        """Subcommands that MUST be registered."""
        return ()

    # ---- Standard tests -------------------------------------------------

    def test_manifest_declares_noun(self, command_manifest_block: dict[str, Any] | None) -> None:
        if command_manifest_block is None:
            pytest.skip("command_manifest_block not provided")
        assert command_manifest_block.get("noun"), (
            "cmd capability manifest block must declare ``noun`` per AEOS §4.3"
        )

    def test_noun_matches_expected(self, command_manifest_block: dict[str, Any] | None) -> None:
        if command_manifest_block is None or self.expected_noun is None:
            pytest.skip("manifest or expected_noun not provided")
        assert command_manifest_block.get("noun") == self.expected_noun, (
            f"manifest noun {command_manifest_block.get('noun')!r} does not "
            f"match expected {self.expected_noun!r}"
        )

    def test_subcommands_registered_in_manifest(
        self, command_manifest_block: dict[str, Any] | None
    ) -> None:
        if command_manifest_block is None:
            pytest.skip("command_manifest_block not provided")
        if not self.expected_subcommands:
            pytest.skip("no expected subcommands declared")
        declared = set(command_manifest_block.get("subcommands", []))
        missing = [s for s in self.expected_subcommands if s not in declared]
        assert not missing, (
            f"manifest does not declare subcommands: {missing} (declared: {sorted(declared)})"
        )

    def test_command_entry_is_usable(self, command_entry: Any) -> None:
        """Entry is either an ArgumentParser or a callable accepting argv."""
        if isinstance(command_entry, argparse.ArgumentParser):
            # Just make sure help doesn't crash.
            help_text = command_entry.format_help()
            assert help_text
        else:
            assert callable(command_entry), (
                f"command entry must be callable or an ArgumentParser; "
                f"got {type(command_entry).__name__}"
            )


__all__ = ["CommandTests"]
