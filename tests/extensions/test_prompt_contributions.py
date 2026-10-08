# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for #84: declarative PromptComposer contributions via manifest.

Extensions declare which system-prompt layer they contribute to in their
``axiom-extension.toml``:

    [[prompt_contributions]]
    layer = "domain_context"
    name = "classroom_role"
    source_module = "axiom.extensions.builtins.classroom.prompt_layers"
    source_function = "build_classroom_role_context"
    required = false

A contributor returns ``str`` to add a contribution or ``None`` to skip
this turn. The composer then calls the function during prompt build.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.contracts import parse_manifest
from axiom.infra.prompt_composer import PromptComposer
from axiom.infra.prompt_contributions import (
    PromptContributionDef,
    apply_prompt_contributions,
)

# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


class TestManifestParsing:
    def _write_manifest(self, dir_: Path, body: str) -> Path:
        (dir_ / "axiom-extension.toml").write_text(body, encoding="utf-8")
        return dir_ / "axiom-extension.toml"

    def test_parses_single_contribution(self, tmp_path):
        path = self._write_manifest(tmp_path, """
[extension]
name = "test_ext"

[[prompt_contributions]]
layer = "domain_context"
name = "classroom_role"
source_module = "tests.fake"
source_function = "build_role"
required = false
""")
        ext = parse_manifest(path)
        assert len(ext.prompt_contributions) == 1
        pc = ext.prompt_contributions[0]
        assert pc.layer == "domain_context"
        assert pc.name == "classroom_role"
        assert pc.source_module == "tests.fake"
        assert pc.source_function == "build_role"
        assert pc.required is False

    def test_parses_multiple_contributions(self, tmp_path):
        path = self._write_manifest(tmp_path, """
[extension]
name = "test_ext"

[[prompt_contributions]]
layer = "identity"
name = "role"
source_module = "tests.fake"
source_function = "role_fn"

[[prompt_contributions]]
layer = "policies"
name = "guardrails"
source_module = "tests.fake"
source_function = "guardrails_fn"
required = true
""")
        ext = parse_manifest(path)
        assert len(ext.prompt_contributions) == 2
        assert ext.prompt_contributions[0].layer == "identity"
        assert ext.prompt_contributions[1].layer == "policies"
        assert ext.prompt_contributions[1].required is True

    def test_missing_contributions_section_is_empty(self, tmp_path):
        path = self._write_manifest(tmp_path, """
[extension]
name = "test_ext"
""")
        ext = parse_manifest(path)
        assert ext.prompt_contributions == []


# ---------------------------------------------------------------------------
# apply_prompt_contributions
# ---------------------------------------------------------------------------


class TestApplyContributions:
    def test_contributed_content_lands_in_composer(self):
        composer = PromptComposer()
        # Define a module-level-ish stub using a manual import map
        defs = [PromptContributionDef(
            layer="domain_context",
            name="stub",
            source_module="axiom.infra.prompt_composer",
            source_function="_build_stub",
            required=False,
        )]

        # Patch the composer module with a stub function for this test.
        import axiom.infra.prompt_composer as module
        module._build_stub = lambda ctx: "STUB-CONTENT"
        try:
            apply_prompt_contributions(
                composer, contributions=defs, context={},
                extension_name="test_ext",
            )
            assert "STUB-CONTENT" in composer.render_text()
        finally:
            delattr(module, "_build_stub")

    def test_none_return_skips_contribution(self):
        composer = PromptComposer()
        defs = [PromptContributionDef(
            layer="domain_context", name="skipper",
            source_module="axiom.infra.prompt_composer",
            source_function="_skip_stub",
            required=False,
        )]
        import axiom.infra.prompt_composer as module
        module._skip_stub = lambda ctx: None
        try:
            apply_prompt_contributions(
                composer, contributions=defs, context={},
                extension_name="test_ext",
            )
            # No contribution added.
            assert "skipper" not in [c.name for c in composer.debug()]
        finally:
            delattr(module, "_skip_stub")

    def test_import_error_does_not_raise(self):
        """A broken contributor must not break prompt build — log + skip."""
        composer = PromptComposer()
        defs = [PromptContributionDef(
            layer="domain_context", name="broken",
            source_module="nonexistent.module.path",
            source_function="nope",
            required=False,
        )]
        # Should not raise.
        apply_prompt_contributions(
            composer, contributions=defs, context={},
            extension_name="broken_ext",
        )
        assert "broken" not in [c.name for c in composer.debug()]

    def test_function_error_does_not_raise(self):
        composer = PromptComposer()
        defs = [PromptContributionDef(
            layer="domain_context", name="raiser",
            source_module="axiom.infra.prompt_composer",
            source_function="_raise_stub",
            required=False,
        )]
        import axiom.infra.prompt_composer as module
        def _raise(_ctx):
            raise RuntimeError("boom")
        module._raise_stub = _raise
        try:
            apply_prompt_contributions(
                composer, contributions=defs, context={},
                extension_name="ext",
            )
            assert "raiser" not in [c.name for c in composer.debug()]
        finally:
            delattr(module, "_raise_stub")

    def test_unknown_layer_skipped_not_raised(self):
        composer = PromptComposer()
        defs = [PromptContributionDef(
            layer="nonexistent_layer", name="x",
            source_module="axiom.infra.prompt_composer",
            source_function="_stub_unknown",
            required=False,
        )]
        import axiom.infra.prompt_composer as module
        module._stub_unknown = lambda ctx: "X"
        try:
            # Must not raise even though the layer doesn't exist.
            apply_prompt_contributions(
                composer, contributions=defs, context={},
                extension_name="ext",
            )
        finally:
            delattr(module, "_stub_unknown")


class TestContextFlow:
    def test_context_passed_through_to_function(self):
        composer = PromptComposer()
        received = {}
        import axiom.infra.prompt_composer as module
        def _capture(ctx):
            received.update(ctx)
            return "ok"
        module._capture_stub = _capture
        try:
            apply_prompt_contributions(
                composer,
                contributions=[PromptContributionDef(
                    layer="domain_context", name="ctx",
                    source_module="axiom.infra.prompt_composer",
                    source_function="_capture_stub",
                )],
                context={"classroom_id": "cr-1", "student_id": "@alice:ut"},
                extension_name="ext",
            )
            assert received["classroom_id"] == "cr-1"
            assert received["student_id"] == "@alice:ut"
        finally:
            delattr(module, "_capture_stub")
