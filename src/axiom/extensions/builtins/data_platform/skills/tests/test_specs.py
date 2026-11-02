# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Every data verb is a declared capability, so every surface can see it.

A skill registered without a :class:`SkillSpec` exists at the terminal and
nowhere else: no MCP tool, no agent-facing function, no generated SKILL.md —
and, because a surface cannot scope what it cannot see, no per-tenant catalog
filtering either (ADR-025 §4, §11).

These are guards, not documentation. The failure they prevent is silent: the
verb keeps working for a human and quietly stops existing for everything else.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import skills
from axiom.infra.capability_projection import (
    capability_to_surface_name,
    inputs_to_json_schema,
    surface_to_capability_name,
)


def _registry():
    return skills.bind_default()


def test_every_verb_declares_a_spec() -> None:
    registry = _registry()
    missing = [v for v in skills.verbs() if registry.spec(f"data.{v}") is None]
    assert missing == [], f"verbs with no SkillSpec are invisible to MCP/agents: {missing}"


def test_every_spec_has_a_description() -> None:
    """The description is what an agent reads to decide whether to call it."""
    registry = _registry()
    bare = [
        v
        for v in skills.verbs()
        if not (registry.spec(f"data.{v}") or None) or not registry.spec(f"data.{v}").description
    ]
    assert bare == []


def test_enroll_projects_to_a_callable_tool() -> None:
    """The end-to-end claim of ADR-025 §11, asserted rather than assumed."""
    spec = _registry().spec("data.enroll")
    assert spec is not None
    name = capability_to_surface_name(spec.name)
    assert name == "data__enroll"
    assert surface_to_capability_name(name) == "data.enroll"
    schema = inputs_to_json_schema(spec.inputs)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"manifest", "bronze_root"}


def test_required_inputs_are_marked_where_the_verb_needs_them() -> None:
    """A capability whose required inputs are all optional invites an agent to
    call it with nothing and get an error it cannot learn from."""
    registry = _registry()
    for verb in ("enroll", "register", "ingest", "unregister"):
        schema = inputs_to_json_schema(registry.spec(f"data.{verb}").inputs)
        assert schema.get("required"), f"data.{verb} declares no required inputs"


def test_specs_cover_the_non_cli_skills_too() -> None:
    """`ingest_push` has no CLI verb precisely because it is a machine surface,
    which makes its spec more important, not less."""
    assert _registry().spec("data.ingest_push") is not None
