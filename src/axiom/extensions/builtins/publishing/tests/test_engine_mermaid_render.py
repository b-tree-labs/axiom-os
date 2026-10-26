# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``PublisherEngine.generate`` Mermaid pre-render integration."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from axiom.extensions.builtins.publishing.config import (
    GitPolicy,
    ProviderConfig,
    PublisherConfig,
)
from axiom.extensions.builtins.publishing.engine import PublisherEngine


@pytest.fixture
def engine(tmp_path: Path) -> PublisherEngine:
    """Engine with local providers; tmp_path is repo_root."""
    config = PublisherConfig(
        git=GitPolicy(
            require_clean=False,
            require_pushed=False,
            publish_branches=["*"],
        ),
        generation=ProviderConfig(provider="pandoc-docx"),
        storage=ProviderConfig(
            provider="local",
            settings={"base_dir": str(tmp_path / "published")},
        ),
        notification=ProviderConfig(provider="terminal"),
        repo_root=tmp_path,
    )
    return PublisherEngine(config)


def test_engine_invokes_mermaid_renderer_when_blocks_present(
    engine: PublisherEngine, tmp_path: Path
) -> None:
    """If the source contains ``` mermaid blocks, the engine pre-processes
    them via ``render_mermaid_blocks`` before handing the .md to pandoc."""
    src = tmp_path / "doc.md"
    src.write_text(
        "# Title\n\n```mermaid\ngraph TD; A-->B\n```\n",
        encoding="utf-8",
    )

    with mock.patch(
        "axiom.extensions.builtins.publishing.mermaid_renderer.render_mermaid_blocks"
    ) as renderer, mock.patch.object(
        engine, "_create_generation_provider"
    ) as factory:
        renderer.return_value = mock.Mock(
            content="# Title\n\n![](rendered.png)\n",
            all_succeeded=True,
            total=1,
            failed=0,
        )
        fake_gen = mock.Mock()
        fake_gen.get_output_extension.return_value = ".docx"
        fake_gen.generate.return_value = mock.Mock(
            output_path=tmp_path / "doc.docx",
            size_bytes=100,
            warnings=[],
        )
        fake_gen.rewrite_links = mock.Mock()
        factory.return_value = fake_gen

        engine.generate(src, output_dir=tmp_path)

    renderer.assert_called_once()
    called_content = renderer.call_args[0][0]
    assert "```mermaid" in called_content


def test_engine_skips_mermaid_renderer_when_no_blocks(
    engine: PublisherEngine, tmp_path: Path
) -> None:
    """No ``` mermaid in source → no pre-render call (zero cost path)."""
    src = tmp_path / "doc.md"
    src.write_text("# Title\n\nJust plain markdown.\n", encoding="utf-8")

    with mock.patch(
        "axiom.extensions.builtins.publishing.mermaid_renderer.render_mermaid_blocks"
    ) as renderer, mock.patch.object(
        engine, "_create_generation_provider"
    ) as factory:
        fake_gen = mock.Mock()
        fake_gen.get_output_extension.return_value = ".docx"
        fake_gen.generate.return_value = mock.Mock(
            output_path=tmp_path / "doc.docx",
            size_bytes=100,
            warnings=[],
        )
        fake_gen.rewrite_links = mock.Mock()
        factory.return_value = fake_gen

        engine.generate(src, output_dir=tmp_path)

    renderer.assert_not_called()


def test_engine_degrades_to_raw_source_on_render_exception(
    engine: PublisherEngine, tmp_path: Path
) -> None:
    """Pre-render is best-effort. If the renderer raises, the engine still
    produces the docx from the unmodified source — the operator gets
    something rather than nothing."""
    src = tmp_path / "doc.md"
    src.write_text(
        "# Title\n\n```mermaid\nbad-syntax;\n```\n", encoding="utf-8"
    )

    with mock.patch(
        "axiom.extensions.builtins.publishing.mermaid_renderer.render_mermaid_blocks",
        side_effect=RuntimeError("mmdc crashed"),
    ), mock.patch.object(engine, "_create_generation_provider") as factory:
        fake_gen = mock.Mock()
        fake_gen.get_output_extension.return_value = ".docx"
        fake_gen.generate.return_value = mock.Mock(
            output_path=tmp_path / "doc.docx",
            size_bytes=100,
            warnings=[],
        )
        fake_gen.rewrite_links = mock.Mock()
        factory.return_value = fake_gen

        # Must not raise:
        engine.generate(src, output_dir=tmp_path)
        # The source passed to pandoc was the RAW source (renderer failed
        # before producing a processed copy).
        assert fake_gen.generate.called
        called_src = fake_gen.generate.call_args[0][0]
        assert called_src == src
