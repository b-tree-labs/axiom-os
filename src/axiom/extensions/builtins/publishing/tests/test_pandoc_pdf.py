# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the branded-PDF generation provider (pandoc → HTML → WeasyPrint)."""

import shutil

import pytest

from axiom.extensions.builtins.publishing.factory import PublisherFactory
from axiom.extensions.builtins.publishing.providers.base import (
    GenerationOptions,
    GenerationProvider,
)

# Ensure providers self-register.
import axiom.extensions.builtins.publishing.providers  # noqa: F401,E402

_HAVE_PANDOC = shutil.which("pandoc") is not None
try:
    import weasyprint  # noqa: F401

    _HAVE_WEASY = True
except Exception:
    _HAVE_WEASY = False

_RENDER = _HAVE_PANDOC and _HAVE_WEASY


@pytest.fixture
def provider():
    return PublisherFactory.create("generation", "pandoc-pdf", {})


@pytest.fixture
def sample_md(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text(
        '---\n'
        'title: "Sample Doc"\n'
        'org: "Example Org"\n'
        'doc_type: "PRD"\n'
        'date: "2026-06-02"\n'
        'brand_color: "#BF5700"\n'
        '---\n\n'
        '# Heading\n\nSome **body** text with a [link](https://example.com).\n'
    )
    return f


class TestRegistration:
    def test_registered_under_pandoc_pdf(self):
        assert "pandoc-pdf" in PublisherFactory.available("generation")

    def test_is_generation_provider(self, provider):
        assert isinstance(provider, GenerationProvider)

    def test_output_extension_is_pdf(self, provider):
        assert provider.get_output_extension() == ".pdf"


@pytest.mark.skipif(not _RENDER, reason="pandoc + weasyprint required")
class TestRender:
    def test_generates_pdf_file(self, provider, sample_md, tmp_path):
        out = tmp_path / "doc.pdf"
        result = provider.generate(sample_md, out, GenerationOptions())
        assert out.exists()
        assert result.format == "pdf"
        assert out.read_bytes().startswith(b"%PDF")
        assert result.size_bytes > 0

    def test_branding_overrides_logo_hook(self, provider, sample_md, tmp_path):
        logo = tmp_path / "logo.png"
        # 1x1 transparent PNG
        logo.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
            )
        )
        out = tmp_path / "doc2.pdf"
        opts = GenerationOptions(metadata={"brand": {"logo": str(logo)}})
        result = provider.generate(sample_md, out, opts)
        assert out.exists()
        assert out.read_bytes().startswith(b"%PDF")
        assert result.format == "pdf"


class TestEngineFormatRouting:
    def test_format_override_selects_pdf_provider(self):
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine()
        prov = engine._create_generation_provider("pdf")
        assert prov.get_output_extension() == ".pdf"

    def test_default_provider_unchanged(self):
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine()
        prov = engine._create_generation_provider()
        assert prov.get_output_extension() == ".docx"


@pytest.mark.skipif(not _HAVE_PANDOC, reason="pandoc required")
def test_dollar_prices_are_not_parsed_as_math(provider, tmp_path):
    """Currency like ``$50–60K`` must render literally, not as LaTeX math.

    Regression: pandoc's default markdown enables ``tex_math_dollars``, so the
    span between two ``$`` (e.g. two prices) was parsed as math — stripping
    spaces, dropping ``**`` bold, and turning hyphens into minus signs.
    """
    f = tmp_path / "prices.md"
    f.write_text(
        "Option B (**~$50–60K**) uses in-stock parts and costs ~$25K less.\n",
        encoding="utf-8",
    )
    html = provider._markdown_to_html(f, GenerationOptions(), [])
    assert "$50" in html and "$25" in html  # dollar signs survive
    assert "in-stock" in html  # hyphen, not a math minus
    assert "−" not in html  # no U+2212 minus
    assert "<strong>" in html  # ** bold still parsed
