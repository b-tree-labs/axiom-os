# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.governance.resource` — typed resource references."""

from __future__ import annotations

import pytest

from axiom.governance.resource import ResourceRef, ResourcePattern


class TestResourceRefConstruction:
    def test_extension_ref(self):
        r = ResourceRef.extension("expman")
        assert r.scheme == "extension"
        assert r.identifier == "expman"

    def test_fragment_ref(self):
        r = ResourceRef.fragment("memory://localhost/fragments/abc123")
        assert r.scheme == "memory"

    def test_channel_ref(self):
        r = ResourceRef.channel("slack://team-rsc/#alerts")
        assert r.scheme == "slack"

    def test_endpoint_ref(self):
        r = ResourceRef.endpoint("https://api.openai.com/v1/chat/completions")
        assert r.scheme == "https"

    def test_arbitrary_scheme_via_parse(self):
        r = ResourceRef.parse("axiom://example-consortium/principal/@user:example-org")
        assert r.scheme == "axiom"

    def test_empty_uri_raises(self):
        with pytest.raises(ValueError):
            ResourceRef.parse("")

    def test_no_scheme_raises(self):
        with pytest.raises(ValueError):
            ResourceRef.parse("just-a-string")


class TestResourceRefSerialization:
    @pytest.mark.parametrize(
        "uri",
        [
            "extension://expman",
            "memory://localhost/fragments/abc123",
            "slack://team-rsc/#alerts",
            "https://api.openai.com/v1/chat/completions",
        ],
    )
    def test_round_trip(self, uri):
        r = ResourceRef.parse(uri)
        assert str(r) == uri


class TestResourcePattern:
    def test_exact_match(self):
        pat = ResourcePattern("slack://team-rsc/#alerts")
        assert pat.matches(ResourceRef.parse("slack://team-rsc/#alerts"))
        assert not pat.matches(ResourceRef.parse("slack://team-rsc/#noise"))

    def test_scheme_wildcard(self):
        pat = ResourcePattern("slack://*")
        assert pat.matches(ResourceRef.parse("slack://team-rsc/#alerts"))
        assert pat.matches(ResourceRef.parse("slack://other-team/#general"))
        assert not pat.matches(ResourceRef.parse("https://example.com"))

    def test_extension_pattern(self):
        pat = ResourcePattern("extension://expman")
        assert pat.matches(ResourceRef.extension("expman"))
        assert not pat.matches(ResourceRef.extension("signals"))

    def test_universal_wildcard(self):
        pat = ResourcePattern("*")
        assert pat.matches(ResourceRef.parse("anything://goes"))
