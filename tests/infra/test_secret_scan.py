# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the secret pattern scanner (ADR-098 D1 secret gate)."""

from __future__ import annotations

import pytest

from axiom.infra.secret_scan import scan_bytes, scan_text


class TestDetects:
    @pytest.mark.parametrize(("name", "sample"), [
        ("private-key-block", "-----BEGIN OPENSSH PRIVATE KEY-----"),
        ("private-key-block", "-----BEGIN PRIVATE KEY-----"),
        ("aws-access-key-id", "aws_key = AKIAIOSFODNN7EXAMPLE"),
        ("github-token", "token: ghp_" + "a1B2" * 10),
        ("github-fine-grained-pat", "github_pat_" + "x1" * 15),
        ("gitlab-pat", "glpat-" + "aB3_-x9z" * 3),
        ("slack-token", "xoxb-123456789012-abcdefABCDEF"),
        ("anthropic-api-key", "sk-ant-api03-" + "q7" * 12),
    ])
    def test_known_formats(self, name, sample):
        findings = scan_text(sample, location="t.jsonl")
        assert name in {f.pattern for f in findings}

    def test_openai_style_key_needs_length(self):
        assert scan_text("sk-" + "a" * 32)
        assert not scan_text("sk-short")

    def test_finding_carries_location_and_line_never_the_match(self):
        text = "line one\nkey AKIAIOSFODNN7EXAMPLE here\n"
        (finding,) = scan_text(text, location="proj/session.jsonl")
        assert finding.location == "proj/session.jsonl"
        assert finding.line == 2
        assert "AKIA" not in repr(finding.pattern)

    def test_scan_bytes_survives_binary_noise(self):
        blob = b"\xff\xfe garbage " + b"AKIAIOSFODNN7EXAMPLE" + b" \x00"
        assert scan_bytes(blob)


class TestStaysQuiet:
    @pytest.mark.parametrize("sample", [
        "ordinary prose about api keys in general",
        "the task risks nothing",           # 'risks' must not trip anything
        "sk- is a prefix we discussed",
        "ghp_ is the github prefix",        # prefix alone, no token body
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",  # JWTs deliberately out
        "password = hunter2",               # generic assignments out
    ])
    def test_no_false_positive(self, sample):
        assert scan_text(sample) == []
