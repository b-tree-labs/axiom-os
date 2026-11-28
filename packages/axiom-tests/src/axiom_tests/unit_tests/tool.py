# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``ToolTests`` — base conformance for tool capabilities (AEOS §4.2)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

VALID_SIDE_EFFECTS = {
    "none",
    "reads_file",
    "writes_file",
    "calls_network",
    "local_only",
    "mutates_state",
}


class ToolTests:
    """Conformance for an AEOS ``tool`` capability."""

    # ---- Overridable fixtures -------------------------------------------

    @pytest.fixture
    def tool_class(self) -> type:
        """Return the tool class under test (override)."""
        raise NotImplementedError("subclasses of ToolTests must override tool_class")

    @pytest.fixture
    def tool_manifest_block(self) -> dict[str, Any] | None:
        return None

    # ---- Capability properties (override in subclass) ------------------

    @property
    def supports_streaming(self) -> bool:
        return False

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def side_effects(self) -> str:
        return "none"

    # ---- Standard tests -------------------------------------------------

    def test_tool_class_exists(self, tool_class: type) -> None:
        assert inspect.isclass(tool_class), "tool_class fixture must return a class"

    def test_has_input_schema(self, tool_class: type) -> None:
        assert hasattr(tool_class, "input_schema"), (
            f"{tool_class.__name__} must expose an ``input_schema`` attribute per AEOS §4.2"
        )

    def test_has_output_schema(self, tool_class: type) -> None:
        assert hasattr(tool_class, "output_schema"), (
            f"{tool_class.__name__} must expose an ``output_schema`` attribute per AEOS §4.2"
        )

    def test_has_invoke_callable(self, tool_class: type) -> None:
        assert hasattr(tool_class, "invoke"), (
            f"{tool_class.__name__} must provide an ``invoke(input) -> output`` "
            "callable per AEOS §4.2"
        )
        invoke = tool_class.invoke
        assert callable(invoke), f"{tool_class.__name__}.invoke must be callable"

    def test_side_effects_value_is_valid(self) -> None:
        assert self.side_effects in VALID_SIDE_EFFECTS, (
            f"declared side_effects {self.side_effects!r} is not one of "
            f"{sorted(VALID_SIDE_EFFECTS)}"
        )

    def test_idempotent_flag_matches_manifest(
        self, tool_manifest_block: dict[str, Any] | None
    ) -> None:
        if tool_manifest_block is None:
            pytest.skip("tool_manifest_block not provided")
        if "idempotent" not in tool_manifest_block:
            pytest.skip("manifest does not declare idempotent")
        assert tool_manifest_block["idempotent"] == self.idempotent, (
            f"manifest says idempotent={tool_manifest_block['idempotent']!r} "
            f"but subclass declares {self.idempotent!r}"
        )

    def test_side_effects_matches_manifest(
        self, tool_manifest_block: dict[str, Any] | None
    ) -> None:
        if tool_manifest_block is None:
            pytest.skip("tool_manifest_block not provided")
        if "side_effects" not in tool_manifest_block:
            pytest.skip("manifest does not declare side_effects")
        assert tool_manifest_block["side_effects"] == self.side_effects, (
            f"manifest side_effects {tool_manifest_block['side_effects']!r} "
            f"disagrees with subclass {self.side_effects!r}"
        )

    def test_streaming_interface(self, tool_class: type) -> None:
        if not self.supports_streaming:
            pytest.skip("tool does not declare streaming support")
        assert hasattr(tool_class, "stream"), (
            f"{tool_class.__name__} declares streaming support but has no ``stream`` method"
        )


__all__ = ["ToolTests", "VALID_SIDE_EFFECTS"]
