# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The program reads on the composed MCP (prd-program R1, phase 2).

``program.status`` and ``program.validate`` project through the skill
registry (ADR-073) exactly as every other extension's read skills do:
opt in with ``surfaces=(…"mcp"…)``, declare ``side_effects=False``, and
the shared projector names them ``axiom_program__status`` /
``axiom_program__validate`` with ``read_only_hint`` set. ``render`` writes
files, so it stays on the CLI.

Over MCP the data file is the node's own: a caller-chosen ``data`` path
would let any MCP caller point the reader at any JSON file the node can
open, so the serving surfaces refuse it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from axiom.extensions.builtins.mcp.identity_gate import principal_admitted  # noqa: E402
from axiom.extensions.builtins.mcp.manifest_schema import parse_mcp_block  # noqa: E402
from axiom.extensions.builtins.mcp.skill_tools import (  # noqa: E402
    is_mcp_exposed,
    skill_tool_contribution,
)
from axiom.extensions.builtins.program import skills  # noqa: E402
from axiom.infra.skills import SkillContext, SkillRegistry  # noqa: E402

_MANIFEST = Path(skills.__file__).parent.parent / "axiom-extension.toml"


def _registry() -> SkillRegistry:
    registry = SkillRegistry()
    skills.bind(registry)
    return registry


def _contribution(state_dir: Path):
    registry = _registry()

    def factory() -> SkillContext:
        return SkillContext(
            registry=registry,
            state_dir=state_dir,
            logger=logging.getLogger("test.program.mcp"),
        )

    return skill_tool_contribution(registry, ctx_factory=factory)


def _call(contribution, tool: str, **arguments):
    return asyncio.run(contribution.dispatch[tool](arguments))


#: The mutation verbs: every one must stay CLI-only (never an MCP tool).
_MUTATIONS = (
    "person_add",
    "person_edit",
    "person_remove",
    "person_reassign",
    "lane_add",
    "lane_edit",
    "lane_remove",
    "item_add",
    "item_edit",
    "item_remove",
    "item_reassign",
    "invite",
    "redeem",
)


class TestTheToolsAreListed:
    def test_the_reads_are_mcp_tools_and_the_writes_are_not(self, state_dir):
        names = {tool.name for tool in _contribution(state_dir).tools}
        assert {
            "axiom_program__status",
            "axiom_program__validate",
            "axiom_program__changes",
            "axiom_program__ownership",
        } <= names
        # render and sync both write; neither is an MCP tool.
        assert "axiom_program__render" not in names
        assert "axiom_program__sync" not in names
        # Not one mutation verb is an MCP tool — the read-vs-write sweep.
        for verb in _MUTATIONS:
            assert f"axiom_program__{verb}" not in names, verb

    def test_writes_do_not_opt_into_mcp(self):
        specs = _registry().specs()
        assert is_mcp_exposed(specs["program.status"])
        assert is_mcp_exposed(specs["program.validate"])
        assert is_mcp_exposed(specs["program.changes"])
        assert is_mcp_exposed(specs["program.ownership"])
        assert not is_mcp_exposed(specs["program.render"])
        assert not is_mcp_exposed(specs["program.sync"])
        for verb in _MUTATIONS:
            spec = specs[f"program.{verb}"]
            assert not is_mcp_exposed(spec), verb
            assert spec.side_effects is True, verb

    def test_the_read_tools_are_read_only_and_idempotent(self, state_dir):
        tools = {tool.name: tool for tool in _contribution(state_dir).tools}
        for name in (
            "axiom_program__status",
            "axiom_program__validate",
            "axiom_program__changes",
            "axiom_program__ownership",
        ):
            assert tools[name].annotations.read_only_hint is True, name
            assert tools[name].annotations.idempotent_hint is True, name

    def test_changes_is_not_a_mutating_tool_by_default(self, state_dir):
        """The read-vs-write sweep: changes must advertise read_only so a
        harness never treats it as a write. The watermark advance is an
        explicit opt-in, not the tool's declared posture."""
        from axiom.infra.capability_projection import is_read_only

        spec = _registry().spec("program.changes")
        assert is_read_only(spec) is True
        assert spec.side_effects is False

    def test_the_status_schema_offers_the_drift_scope(self, state_dir):
        tool = next(t for t in _contribution(state_dir).tools if t.name == "axiom_program__status")
        assert "scope" in tool.input_schema["properties"]
        assert "scope" in tool.input_schema.get("required", [])
        assert "drift" in tool.description

    def test_the_manifest_declares_the_projection(self):
        cfg = parse_mcp_block(_MANIFEST)
        assert cfg is not None and cfg.enabled
        assert cfg.prefix == "axiom_program"
        assert "mcp: not-applicable" not in _MANIFEST.read_text(encoding="utf-8")


class TestTheIdentityGate:
    def test_the_tools_are_bound_to_the_owner_scoped_default(self, state_dir):
        allowed = _contribution(state_dir).allowed_principals
        for name in ("axiom_program__status", "axiom_program__validate"):
            patterns = allowed[name]
            assert principal_admitted("@casey:local", patterns)
            assert not principal_admitted("@casey:elsewhere-org", patterns)


class TestDispatchAnswersFromTheNodesFile:
    def test_status_answers_from_the_default_data_file(self, state_dir):
        out = _call(_contribution(state_dir), "axiom_program__status", scope="schedule")
        assert out["ok"] is True, out
        assert [i["id"] for i in out["value"]["items"]] == ["i-one", "i-two", "i-three", "i-four"]

    def test_an_unknown_principal_is_refused_as_absent(self, state_dir):
        out = _call(
            _contribution(state_dir),
            "axiom_program__status",
            scope="person",
            key="@nobody:example-org",
        )
        assert out["ok"] is False
        assert out["value"]["refused"] == "absent"
        assert "@nobody:example-org" in out["errors"][0]

    def test_an_unknown_item_is_refused_as_absent(self, state_dir):
        out = _call(_contribution(state_dir), "axiom_program__status", scope="item", key="i-nine")
        assert out["ok"] is False
        assert out["value"]["refused"] == "absent"

    def test_drift_is_reachable_over_mcp(self, state_dir):
        out = _call(_contribution(state_dir), "axiom_program__status", scope="drift")
        assert out["ok"] is True
        assert out["value"]["basis"] == "data-file-only"

    def test_validate_reports_the_files_shape(self, state_dir):
        out = _call(_contribution(state_dir), "axiom_program__validate")
        assert out["ok"] is True
        assert out["value"]["program"] == "example-program"

    def test_a_caller_chosen_data_path_is_refused(self, state_dir, data_file):
        contribution = _contribution(state_dir)
        for tool, args in (
            ("axiom_program__status", {"scope": "schedule"}),
            ("axiom_program__validate", {}),
        ):
            out = _call(contribution, tool, data=str(data_file), **args)
            assert out["ok"] is False, tool
            assert "data" in out["errors"][0]

    def test_an_undeclared_argument_is_refused(self, state_dir):
        out = _call(
            _contribution(state_dir), "axiom_program__status", scope="schedule", out="/tmp/x"
        )
        assert out["ok"] is False
        assert "does not declare" in out["errors"][0]


class TestChangesOverMcpStaysARead:
    """Over MCP, ``axiom_program__changes`` defaults to peek — a served call
    does not move a watermark. Advancing happens only when the caller sets
    ``advance`` explicitly, so the read-only tool never writes behind a
    caller's back."""

    def _seed(self, state_dir):
        # populate the change log the same way the node would: one sync.
        import logging

        from axiom.extensions.builtins.program.skills import sync

        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=state_dir,
            logger=logging.getLogger("test.program.mcp.seed"),
            user_prompt=None,
            surface="cli",
        )
        sync.run({}, ctx)

    def test_a_served_changes_call_does_not_advance(self, state_dir):
        self._seed(state_dir)
        contribution = _contribution(state_dir)
        first = _call(contribution, "axiom_program__changes", principal="@casey:example-org")
        assert first["ok"] is True
        assert first["value"]["advanced"] is False
        # a second served call still sees everything — nothing was marked seen
        second = _call(contribution, "axiom_program__changes", principal="@casey:example-org")
        assert first["value"]["count"] == second["value"]["count"] > 0

    def test_a_served_changes_call_advances_on_explicit_request(self, state_dir):
        self._seed(state_dir)
        contribution = _contribution(state_dir)
        advanced = _call(
            contribution, "axiom_program__changes", principal="@casey:example-org", advance=True
        )
        assert advanced["value"]["advanced"] is True
        after = _call(contribution, "axiom_program__changes", principal="@casey:example-org")
        assert after["value"]["count"] == 0

    def test_an_undeclared_argument_is_refused(self, state_dir):
        self._seed(state_dir)
        out = _call(
            _contribution(state_dir),
            "axiom_program__changes",
            principal="@casey:example-org",
            scope="schedule",
        )
        assert out["ok"] is False
        assert "does not declare" in out["errors"][0]
