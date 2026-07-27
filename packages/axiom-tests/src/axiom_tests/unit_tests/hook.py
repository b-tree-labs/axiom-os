# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``HookTests`` — base conformance for hook capabilities (AEOS §4.7)."""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest

VALID_FAIL_MODES = {"abort", "warn", "ignore"}
EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class HookTests:
    """Conformance for an AEOS ``hook`` capability."""

    # ---- Overridable fixtures -------------------------------------------

    @pytest.fixture
    def hook_entry(self) -> Any:
        """Return the callable / module that implements the hook (override)."""
        raise NotImplementedError("subclasses of HookTests must override hook_entry")

    @pytest.fixture
    def hook_manifest_block(self) -> dict[str, Any] | None:
        return None

    # ---- Capability properties -----------------------------------------

    @property
    def declared_events(self) -> tuple[str, ...]:
        return ()

    @property
    def fail_mode(self) -> str:
        return "warn"

    # ---- Standard tests -------------------------------------------------

    def test_hook_entry_is_callable_or_module(self, hook_entry: Any) -> None:
        ok = callable(hook_entry) or inspect.ismodule(hook_entry)
        assert ok, f"hook_entry must be a callable or a module; got {type(hook_entry).__name__}"

    def test_fail_mode_is_valid(self) -> None:
        assert self.fail_mode in VALID_FAIL_MODES, (
            f"fail_mode {self.fail_mode!r} is not one of {sorted(VALID_FAIL_MODES)}"
        )

    def test_manifest_fail_mode_matches(self, hook_manifest_block: dict[str, Any] | None) -> None:
        if hook_manifest_block is None:
            pytest.skip("hook_manifest_block not provided")
        declared = hook_manifest_block.get("fail_mode")
        if declared is None:
            pytest.skip("manifest does not declare fail_mode")
        assert declared == self.fail_mode, (
            f"manifest fail_mode={declared!r} disagrees with subclass {self.fail_mode!r}"
        )

    def test_events_look_like_event_names(self) -> None:
        invalid = [e for e in self.declared_events if not EVENT_NAME_RE.match(e)]
        assert not invalid, (
            f"these event names do not match ``<namespace>.<event>`` form: {invalid}"
        )

    def test_manifest_events_match_declared(
        self, hook_manifest_block: dict[str, Any] | None
    ) -> None:
        if hook_manifest_block is None:
            pytest.skip("hook_manifest_block not provided")
        declared = hook_manifest_block.get("events")
        if declared is None:
            pytest.skip("manifest does not declare events")
        if not self.declared_events:
            pytest.skip("subclass did not declare events")
        assert set(declared) == set(self.declared_events), (
            f"manifest events {sorted(declared)!r} disagree with subclass "
            f"{sorted(self.declared_events)!r}"
        )


__all__ = ["EVENT_NAME_RE", "HookTests", "VALID_FAIL_MODES"]
