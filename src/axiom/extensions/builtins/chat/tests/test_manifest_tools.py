# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A tool declared once reaches every surface, chat included.

An extension declares a tool neutrally:

    [[extension.provides]]
    kind = "tool"
    name = "telemetry_metrics"
    entry = "...mcp_tools:telemetry_metrics"
    idempotent = true

MCP reads those and publishes them. `neut chat` did not: it scanned its own
`tools_ext/` directory and an opt-in `chat_tools_module`, which two of
fifty-seven extensions declare. So every telemetry gold verb reached Claude
Code and every other MCP harness, and never reached Neut's own chat — the
reference surface was the weakest one.

Only idempotent tools are surfaced automatically. A tool with side effects
still has to opt in, because appearing in an agent's tool list is permission
to call it.
"""
from __future__ import annotations

import textwrap

import pytest

from axiom.extensions.builtins.chat import tools as T
from axiom.infra.orchestrator.actions import ActionCategory


@pytest.fixture
def manifest_ext(tmp_path):
    """An extension declaring one read tool and one write tool."""
    root = tmp_path / "demo_ext"
    root.mkdir()
    (root / "axiom-extension.toml").write_text(
        textwrap.dedent(
            """
            [extension]
            name = "demo"
            version = "0.1.0"

            [[extension.provides]]
            kind = "tool"
            name = "demo_read"
            entry = "demo_handlers:read_thing"
            description = "Read a thing."
            idempotent = true

            [[extension.provides]]
            kind = "tool"
            name = "demo_write"
            entry = "demo_handlers:write_thing"
            description = "Change a thing."
            idempotent = false

            [[extension.provides]]
            kind = "cmd"
            name = "demo_cmd"
            noun = "demo"
            entry = "demo_handlers:main"
            description = "Not a tool."
            idempotent = true
            """
        ).strip()
    )
    return root


class TestManifestToolsReachChat:
    def test_an_idempotent_declared_tool_appears(self, manifest_ext):
        found = T._scan_manifest_tools(roots=[manifest_ext])
        assert "demo_read" in found
        assert found["demo_read"].description == "Read a thing."

    def test_it_is_categorised_read(self, manifest_ext):
        """`idempotent = true` is the manifest's word for "safe to call"."""
        found = T._scan_manifest_tools(roots=[manifest_ext])
        assert found["demo_read"].category is ActionCategory.READ

    def test_a_tool_with_side_effects_is_not_auto_surfaced(self, manifest_ext):
        """Appearing in an agent's tool list is permission to call it, so a
        non-idempotent tool still has to opt in explicitly."""
        found = T._scan_manifest_tools(roots=[manifest_ext])
        assert "demo_write" not in found

    def test_non_tool_provides_are_ignored(self, manifest_ext):
        """The fixture's `cmd` carries a name AND `idempotent = true` on
        purpose. Without those it would be skipped for the wrong reason —
        having no name — and this would pass even if `kind` were ignored
        entirely, which is exactly what a mutant proved.
        """
        found = T._scan_manifest_tools(roots=[manifest_ext])
        assert "demo_cmd" not in found
        assert "demo" not in found

    def test_a_broken_manifest_does_not_break_chat(self, tmp_path):
        """Chat must start even if an extension ships an unparseable manifest."""
        root = tmp_path / "bad"
        root.mkdir()
        (root / "axiom-extension.toml").write_text("this is not = valid toml [[[")
        assert T._scan_manifest_tools(roots=[root]) == {}

    def test_a_missing_entry_is_skipped_not_raised(self, tmp_path):
        root = tmp_path / "noentry"
        root.mkdir()
        (root / "axiom-extension.toml").write_text(
            '[extension]\nname = "x"\n\n'
            '[[extension.provides]]\nkind = "tool"\nname = "t"\nidempotent = true\n'
        )
        assert T._scan_manifest_tools(roots=[root]) == {}


class TestPrecedence:
    def test_a_builtin_of_the_same_name_wins(self, manifest_ext):
        """A manifest must not be able to replace a platform tool by naming
        it — that would let any installed extension redefine `write_file`."""
        found = T._scan_manifest_tools(roots=[manifest_ext], taken={"demo_read"})
        assert "demo_read" not in found

    def test_the_registry_includes_manifest_tools(self, monkeypatch, manifest_ext):
        monkeypatch.setattr(
            T, "_scan_manifest_tools", lambda **kw: {
                "demo_read": T.ToolDef(
                    name="demo_read", description="d", category=ActionCategory.READ
                )
            }
        )
        assert "demo_read" in T.get_all_tools()


class TestTheCallingConventionsAreBridged:
    """Chat calls a tool with keyword arguments; an MCP handler takes one
    positional dict — `telemetry_metrics(args)`. The manifest declaration is
    neutral between them, so the bridge adapts. Without this every gold verb
    bound cleanly and then failed at call time with a TypeError about a
    missing `args`.
    """

    def test_a_single_positional_handler_receives_a_dict(self):
        seen = {}

        def mcp_style(args):
            seen.update(args)
            return {"ok": True}

        call = T._bind_entry("x:y")
        assert call is not None
        # Bind directly to avoid importing a module for the test.
        import types

        module = types.ModuleType("fake_mcp_mod")
        module.handler = mcp_style
        import sys as _sys

        _sys.modules["fake_mcp_mod"] = module
        try:
            T._bind_entry("fake_mcp_mod:handler")(site="netl")
            assert seen == {"site": "netl"}
        finally:
            del _sys.modules["fake_mcp_mod"]

    def test_a_keyword_handler_is_called_with_keywords(self):
        import sys as _sys
        import types

        seen = {}

        def chat_style(**kwargs):
            seen.update(kwargs)
            return {"ok": True}

        module = types.ModuleType("fake_chat_mod")
        module.handler = chat_style
        _sys.modules["fake_chat_mod"] = module
        try:
            T._bind_entry("fake_chat_mod:handler")(site="netl")
            assert seen == {"site": "netl"}
        finally:
            del _sys.modules["fake_chat_mod"]

    def test_binding_is_lazy(self):
        """Resolving at scan time would import every extension declaring a
        tool on every chat turn, and couple the registry to whether an
        unrelated extension's imports happen to work today."""
        call = T._bind_entry("module.that.does.not.exist:fn")
        assert call is not None
        with pytest.raises(ModuleNotFoundError):
            call()

    def test_a_malformed_entry_binds_to_nothing(self):
        assert T._bind_entry("no_colon_here") is None
        assert T._bind_entry(":nofunc") is None


class TestTheSkillConvention:
    """ADR-056 skills are `run(params, ctx) -> SkillResult`, a third shape
    alongside chat's keyword handlers and MCP's single positional dict. A
    tool pointing at a skill is the ADR-conformant declaration, so the bridge
    speaks it rather than treating it as a special case.
    """

    @staticmethod
    def _module(name, fn):
        import sys as _sys
        import types

        mod = types.ModuleType(name)
        mod.run = fn
        _sys.modules[name] = mod
        return name

    def test_a_skill_receives_params_and_a_context(self):
        import sys as _sys

        seen = {}

        def skill(params, ctx):
            seen["params"] = params
            seen["has_ctx"] = ctx is not None
            return _Result(ok=True, value={"n": 1}, errors=[])

        name = self._module("fake_skill_mod", skill)
        try:
            T._bind_entry(f"{name}:run")(recipient="@a:b")
            assert seen["params"] == {"recipient": "@a:b"}
            assert seen["has_ctx"] is True
        finally:
            del _sys.modules[name]

    def test_the_payload_comes_from_value_not_data(self):
        """A SkillResult carries its payload in `value`. Reading `data`
        returned a well-formed envelope with the answer missing — a tool that
        looks like it worked and tells the model nothing.
        """
        import sys as _sys

        def skill(params, ctx):
            return _Result(ok=True, value={"count": 3, "items": [1, 2, 3]}, errors=[])

        name = self._module("fake_skill_value", skill)
        try:
            out = T._bind_entry(f"{name}:run")()
            assert out["value"]["count"] == 3
            assert out["ok"] is True
        finally:
            del _sys.modules[name]

    def test_errors_are_carried_through(self):
        import sys as _sys

        def skill(params, ctx):
            return _Result(ok=False, value=None, errors=["nope"])

        name = self._module("fake_skill_err", skill)
        try:
            out = T._bind_entry(f"{name}:run")()
            assert out["ok"] is False and out["errors"] == ["nope"]
        finally:
            del _sys.modules[name]


class _Result:
    """Minimal stand-in for SkillResult (ok / value / errors)."""

    def __init__(self, ok, value, errors):
        self.ok = ok
        self.value = value
        self.errors = errors
