# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the LaTeX→PDF generation provider (Tectonic). See ADR-090."""

import shutil

import pytest

from axiom.extensions.builtins.publishing.factory import PublisherFactory
from axiom.extensions.builtins.publishing.providers.base import (
    GenerationOptions,
    GenerationProvider,
)

# Ensure providers self-register.
import axiom.extensions.builtins.publishing.providers  # noqa: F401,E402

_HAVE_TECTONIC = shutil.which("tectonic") is not None


@pytest.fixture
def provider():
    return PublisherFactory.create("generation", "latex-pdf", {})


@pytest.fixture
def sample_tex(tmp_path):
    f = tmp_path / "main.tex"
    f.write_text(
        "\\documentclass[10pt]{article}\n"
        "\\begin{document}\n"
        "\\title{Sample}\\author{A}\\maketitle\n"
        "Hello from a compiled LaTeX project.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    return f


class TestRegistration:
    def test_registered_under_latex_pdf(self):
        assert "latex-pdf" in PublisherFactory.available("generation")

    def test_is_generation_provider(self, provider):
        assert isinstance(provider, GenerationProvider)

    def test_output_extension_is_pdf(self, provider):
        assert provider.get_output_extension() == ".pdf"

    def test_does_not_promise_watermark(self, provider):
        assert provider.supports_watermark() is False


class TestFormatRouting:
    def test_latex_and_tex_formats_map_to_provider(self):
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        assert PublisherEngine._FORMAT_PROVIDERS["latex"] == "latex-pdf"
        assert PublisherEngine._FORMAT_PROVIDERS["tex"] == "latex-pdf"


@pytest.mark.skipif(not _HAVE_TECTONIC, reason="tectonic binary required")
class TestCompile:
    def test_compiles_tex_to_pdf(self, provider, sample_tex, tmp_path):
        out = tmp_path / "out.pdf"
        result = provider.generate(sample_tex, out, GenerationOptions())
        assert out.exists()
        assert result.format == "pdf"
        assert result.size_bytes > 0
        # A real PDF starts with the %PDF- magic.
        assert out.read_bytes()[:5] == b"%PDF-"

    def test_directory_resolves_to_main_tex(self, provider, sample_tex, tmp_path):
        out = tmp_path / "out2.pdf"
        # Pass the directory (containing main.tex), not the file.
        result = provider.generate(sample_tex.parent, out, GenerationOptions())
        assert out.exists() and result.size_bytes > 0

    def test_missing_main_tex_in_dir_raises(self, provider, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(RuntimeError, match="main.tex"):
            provider.generate(empty, tmp_path / "x.pdf", GenerationOptions())


def test_missing_tectonic_raises_actionable(monkeypatch, tmp_path):
    """When tectonic is absent, the provider raises an install hint, not a
    cryptic FileNotFoundError."""
    from axiom.extensions.builtins.publishing.providers.generation.latex_pdf import (
        LatexPdfProvider,
    )

    p = LatexPdfProvider()
    monkeypatch.setattr(p, "tectonic_path", None)
    tex = tmp_path / "main.tex"
    tex.write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    with pytest.raises(RuntimeError, match="tectonic not found"):
        p.generate(tex, tmp_path / "o.pdf", GenerationOptions())
