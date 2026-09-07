# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for federation URI addressing (axiom://<node>/<fragment>)."""

from __future__ import annotations

import pytest


class TestFormat:
    def test_basic_uri(self):
        from axiom.memory.addressing import format_uri

        uri = format_uri("example-host.axiom.org", "abc123")
        assert uri == "axiom://example-host.axiom.org/abc123"

    def test_empty_node_rejected(self):
        from axiom.memory.addressing import format_uri

        with pytest.raises(ValueError, match="node"):
            format_uri("", "abc")

    def test_empty_fragment_rejected(self):
        from axiom.memory.addressing import format_uri

        with pytest.raises(ValueError, match="fragment"):
            format_uri("example-host", "")


class TestParse:
    def test_parse_roundtrip(self):
        from axiom.memory.addressing import format_uri, parse_uri

        uri = format_uri("prague.axiom.eu", "frag-1")
        node, frag = parse_uri(uri)
        assert node == "prague.axiom.eu"
        assert frag == "frag-1"

    def test_parse_rejects_wrong_scheme(self):
        from axiom.memory.addressing import parse_uri

        with pytest.raises(ValueError, match="scheme"):
            parse_uri("http://example-host.axiom.org/abc")

    def test_parse_rejects_missing_fragment(self):
        from axiom.memory.addressing import parse_uri

        with pytest.raises(ValueError, match="fragment"):
            parse_uri("axiom://example-host.axiom.org/")

    def test_parse_rejects_missing_node(self):
        from axiom.memory.addressing import parse_uri

        with pytest.raises(ValueError, match="node"):
            parse_uri("axiom:///fragment-only")


class TestIsAxiomUri:
    def test_valid_uri(self):
        from axiom.memory.addressing import is_axiom_uri

        assert is_axiom_uri("axiom://node/frag") is True

    def test_invalid_uri(self):
        from axiom.memory.addressing import is_axiom_uri

        assert is_axiom_uri("not a uri") is False
        assert is_axiom_uri("http://example.com") is False
