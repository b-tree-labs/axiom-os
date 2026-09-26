# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi pub draft --format pdf` arg parsing + branding overrides."""

import argparse

from axiom.extensions.builtins.publishing.cli import _add_draft_args


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    _add_draft_args(sub.add_parser("draft"))
    return parser


class TestDraftFormatArgs:
    def test_default_format_is_none(self):
        args = _parser().parse_args(["draft", "doc.md"])
        assert args.file == "doc.md"
        assert args.format is None

    def test_format_pdf(self):
        args = _parser().parse_args(["draft", "doc.md", "--format", "pdf"])
        assert args.format == "pdf"

    def test_to_alias(self):
        args = _parser().parse_args(["draft", "doc.md", "--to", "pdf"])
        assert args.format == "pdf"

    def test_brand_overrides(self):
        args = _parser().parse_args(
            [
                "draft", "doc.md", "--format", "pdf",
                "--brand-color", "#BF5700",
                "--brand-org", "Example Org",
                "--brand-logo", "/tmp/logo.png",
                "--brand-title", "Override Title",
            ]
        )
        assert args.brand_color == "#BF5700"
        assert args.brand_org == "Example Org"
        assert args.brand_logo == "/tmp/logo.png"
        assert args.brand_title == "Override Title"

    def test_rejects_unknown_format(self):
        import pytest

        with pytest.raises(SystemExit):
            _parser().parse_args(["draft", "doc.md", "--format", "epub"])
