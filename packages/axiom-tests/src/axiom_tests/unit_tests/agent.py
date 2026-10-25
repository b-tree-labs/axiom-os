# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``AgentTests`` — base conformance for agent capabilities (AEOS §4.1)."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import pytest

_AGENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9-]*$")


class AgentTests:
    """Conformance for an AEOS ``agent`` capability.

    Subclasses override ``agent_class`` (required) and any capability
    properties whose default of ``False`` does not match their agent.
    """

    # ---- Overridable fixtures -------------------------------------------

    @pytest.fixture
    def agent_class(self) -> type:
        """Return the agent class under test (override)."""
        raise NotImplementedError(
            "subclasses of AgentTests must override agent_class to return the class"
        )

    @pytest.fixture
    def agent_manifest_block(self) -> dict[str, Any] | None:
        """Return the ``[[extension.provides]]`` block for this agent, if any.

        Tests that depend on manifest-derived values (e.g.
        ``uses_skills`` cross-checking) use this fixture. Returning
        ``None`` is allowed — those tests skip.
        """
        return None

    @pytest.fixture
    def extension_manifest(self) -> dict[str, Any] | None:
        """Full manifest for cross-field checks; override in subclass."""
        return None

    @pytest.fixture
    def extension_root(self) -> Path | None:
        """Extension root for persona.md lookup; override in subclass."""
        return None

    # ---- Overridable capability properties -----------------------------

    @property
    def implements_classify(self) -> bool:
        return False

    @property
    def implements_plan(self) -> bool:
        return False

    @property
    def implements_execute(self) -> bool:
        return True  # Nearly every agent implements execute

    @property
    def implements_learn(self) -> bool:
        return False

    @property
    def requires_persona(self) -> bool:
        """Whether this agent declares ``persona = "..."`` in its manifest."""
        return True

    # ---- Standard tests -------------------------------------------------

    def test_agent_class_exists(self, agent_class: type) -> None:
        assert inspect.isclass(agent_class), (
            f"agent_class fixture must return a class, got {type(agent_class).__name__}"
        )

    def test_agent_has_name_attribute(self, agent_class: type) -> None:
        assert hasattr(agent_class, "name"), (
            f"{agent_class.__name__} must expose a class-level ``name`` attribute per AEOS §4.1"
        )
        name = agent_class.name
        assert isinstance(name, str) and name, (
            f"{agent_class.__name__}.name must be a non-empty string"
        )

    def test_agent_name_follows_wall_e_convention(self, agent_class: type) -> None:
        name = getattr(agent_class, "name", "")
        assert _AGENT_NAME_PATTERN.match(name), (
            f"agent name {name!r} must follow AXI ALL-CAPS-HYPHEN convention "
            "(e.g., SCAN, CHALKE, WARDEN)"
        )

    def test_implements_declared_interface(self, agent_class: type) -> None:
        method_flags = {
            "classify": self.implements_classify,
            "plan": self.implements_plan,
            "execute": self.implements_execute,
            "learn": self.implements_learn,
        }
        missing = [
            method
            for method, required in method_flags.items()
            if required and not hasattr(agent_class, method)
        ]
        assert not missing, (
            f"{agent_class.__name__} declares these capabilities but does not "
            f"implement the methods: {missing}"
        )

    def test_persona_file_present_if_declared(
        self,
        agent_manifest_block: dict[str, Any] | None,
        extension_root: Path | None,
    ) -> None:
        if not self.requires_persona:
            pytest.skip("agent does not declare a persona")
        if agent_manifest_block is None or extension_root is None:
            pytest.skip("agent_manifest_block or extension_root not provided")
        persona = agent_manifest_block.get("persona")
        if not persona:
            pytest.skip("agent manifest block has no persona declaration")
        persona_path = extension_root / persona
        assert persona_path.exists(), f"declared persona file {persona_path} does not exist"

    def test_uses_skills_resolve(
        self,
        agent_manifest_block: dict[str, Any] | None,
        extension_manifest: dict[str, Any] | None,
    ) -> None:
        """Every ``uses_skills`` entry must point to a declared skill.

        Per AEOS §4.1 / §4.6, skills are standalone. An agent may reference
        them via ``uses_skills`` but the referenced skills must be declared
        as ``[[extension.provides]] kind = "skill"`` somewhere (either in
        this extension or — by convention — in a consumed extension).
        """
        if agent_manifest_block is None or extension_manifest is None:
            pytest.skip("manifest context not provided")
        uses = agent_manifest_block.get("uses_skills") or []
        if not uses:
            pytest.skip("agent does not declare uses_skills")
        declared_skills = {
            p.get("name")
            for p in extension_manifest.get("extension", {}).get("provides", [])
            if p.get("kind") == "skill"
        }
        # Un-resolvable skills are allowed if they live in another extension.
        # We only flag ones that look local (no dotted prefix) and are not
        # declared here — that's almost certainly a typo.
        missing = [s for s in uses if "." not in s and s not in declared_skills]
        assert not missing, (
            f"agent declares uses_skills {missing!r} but extension provides no "
            f"matching skill blocks (declared: {sorted(declared_skills)})"
        )


__all__ = ["AgentTests"]
