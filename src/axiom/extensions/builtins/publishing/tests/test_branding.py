# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the generic publishing branding model.

Branding is supplied as generic inputs (org wordmark, brand color, doc
title/type/date, optional logo path) — never hardcoded to any consumer.
"""

from axiom.extensions.builtins.publishing.branding import (
    Branding,
    parse_frontmatter,
)


class TestParseFrontmatter:
    def test_extracts_yaml_block(self):
        md = '---\ntitle: "Hello"\nbrand_color: "#BF5700"\n---\n# Body\n'
        meta, body = parse_frontmatter(md)
        assert meta["title"] == "Hello"
        assert meta["brand_color"] == "#BF5700"
        assert body.strip().startswith("# Body")

    def test_no_frontmatter(self):
        md = "# Just a body\n"
        meta, body = parse_frontmatter(md)
        assert meta == {}
        assert body == md


class TestBrandingFromMetadata:
    def test_pulls_generic_fields(self):
        meta = {
            "title": "Digital-Twin Hosting Infrastructure",
            "org": "UT Computational Nuclear Engineering",
            "doc_type": "PRD",
            "date": "2026-06-02",
            "brand_color": "#BF5700",
        }
        b = Branding.from_metadata(meta)
        assert b.title == "Digital-Twin Hosting Infrastructure"
        assert b.org == "UT Computational Nuclear Engineering"
        assert b.doc_type == "PRD"
        assert b.brand_color == "#BF5700"
        assert b.date == "2026-06-02"

    def test_overrides_win_over_metadata(self):
        meta = {"title": "From FM", "brand_color": "#000000"}
        b = Branding.from_metadata(meta, overrides={"brand_color": "#BF5700"})
        assert b.title == "From FM"
        assert b.brand_color == "#BF5700"

    def test_default_brand_color(self):
        b = Branding.from_metadata({})
        # falls back to a neutral default, not a consumer color
        assert b.brand_color.startswith("#")

    def test_logo_path_hook(self):
        b = Branding.from_metadata({}, overrides={"logo": "/tmp/logo.png"})
        assert b.logo == "/tmp/logo.png"


class TestBrandingCss:
    def test_css_contains_brand_color(self):
        b = Branding(title="T", org="O", brand_color="#BF5700")
        css = b.to_css()
        assert "#BF5700" in css
        assert "@page" in css

    def test_header_html_has_org_and_title(self):
        b = Branding(title="My Title", org="My Org", brand_color="#BF5700")
        html = b.header_html()
        assert "My Org" in html
        assert "My Title" in html

    def test_header_renders_logo_when_present(self, tmp_path):
        logo = tmp_path / "logo.png"
        logo.write_bytes(b"\x89PNG\r\n")
        b = Branding(title="T", org="O", brand_color="#BF5700", logo=str(logo))
        html = b.header_html()
        assert "<img" in html
        assert str(logo) in html or logo.name in html

    def test_header_no_img_without_logo(self):
        b = Branding(title="T", org="O", brand_color="#BF5700")
        assert "<img" not in b.header_html()
