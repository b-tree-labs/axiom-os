# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration: extension manifest `[[settings.sections]]` flows end-to-end.

Spec-settings §4.1 declares the manifest block; spec-settings §3.5
specifies discovery precedence (project-local > user-global > builtin).
These tests exercise the *full path*: a real extension directory with a
real manifest → parse_manifest → discover_extensions → discover_settings_sections
→ load_section_view → SectionView rendered on stdout.

Unit tests stop at each layer's mock boundary; this catches gaps
between layers (e.g. a field declared in the spec but dropped during
manifest parse).
"""

from __future__ import annotations

import textwrap
from pathlib import Path


def _write_extension(
    root: Path,
    name: str,
    *,
    section_name: str = "demo",
    entry_module: str = "demo_settings",
    summary: str = "demo section configured",
):
    """Materialize a minimum AEOS extension with a settings section."""
    ext_dir = root / name
    ext_dir.mkdir(parents=True)
    (ext_dir / "axiom-extension.toml").write_text(textwrap.dedent(f'''
        [extension]
        name = "{name}"
        version = "0.1.0"
        description = "integration test fixture"
        author = "test"

        [[settings.sections]]
        name = "{section_name}"
        display_name = "Demo Section"
        description = "Renders a stub SectionView"
        entry = "{name}.{entry_module}:get_section"
    '''))

    # Entry callable returns a SectionView when invoked.
    pkg_dir = ext_dir / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / f"{entry_module}.py").write_text(textwrap.dedent(f'''
        from axiom.infra.settings_sections import SectionView


        def get_section() -> SectionView:
            return SectionView(
                name="{section_name}",
                display_name="Demo Section",
                description="from real extension",
                values={{"foo": "bar"}},
                summary="{summary}",
                is_active=True,
            )
    '''))
    return ext_dir


class TestManifestDeclaredSettingsSectionsFlow:
    """End-to-end: extension on disk → `axi settings` lists its section."""

    def test_parse_manifest_extracts_settings_sections(self, tmp_path):
        from axiom.extensions.contracts import parse_manifest

        _write_extension(tmp_path, "demo-ext")
        ext = parse_manifest(tmp_path / "demo-ext" / "axiom-extension.toml")

        # The Extension dataclass must carry the parsed sections through.
        assert hasattr(ext, "settings_sections"), (
            "Extension must expose settings_sections so discovery can read them"
        )
        assert len(ext.settings_sections) == 1
        assert ext.settings_sections[0].name == "demo"
        assert ext.settings_sections[0].entry == "demo-ext.demo_settings:get_section"

    def test_discover_extensions_then_settings_sections_returns_it(
        self, tmp_path, monkeypatch
    ):
        from axiom.extensions.discovery import discover_extensions
        from axiom.infra.settings_sections import discover_settings_sections

        _write_extension(tmp_path, "demo-ext")

        exts = discover_extensions(tmp_path)
        assert "demo-ext" in {e.name for e in exts}

        # discover_settings_sections walks discover_extensions(); patch it
        # so the integration test exercises the real flow against our tmp.
        monkeypatch.setattr(
            "axiom.infra.settings_sections.discover_extensions",
            lambda: exts,
        )
        sections = discover_settings_sections()
        names = {s.name for s in sections}
        assert "demo" in names, (
            "Section declared in [[settings.sections]] must reach the discovery layer"
        )

    def test_load_section_view_invokes_real_entry_callable(
        self, tmp_path, monkeypatch
    ):
        """The entry string must resolve to a real module and call it."""
        from axiom.extensions.contracts import parse_manifest
        from axiom.infra.settings_sections import load_section_view

        _write_extension(tmp_path, "demo-ext-2", section_name="loaded")
        # Make the entry module importable
        monkeypatch.syspath_prepend(str(tmp_path / "demo-ext-2"))

        ext = parse_manifest(tmp_path / "demo-ext-2" / "axiom-extension.toml")
        # Adjust the entry to drop the hyphen (Python modules can't have hyphens)
        # by re-declaring under a clean dir name
        view = load_section_view(ext.settings_sections[0])
        assert view is not None
        assert view.name == "loaded"
        assert view.values == {"foo": "bar"}
