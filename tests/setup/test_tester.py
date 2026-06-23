# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.setup.tester."""

from pathlib import Path
from unittest.mock import patch

import pytest

from axiom.setup.tester import ChannelTester, TestResult


class TestTestResult:
    def test_to_dict(self):
        result = TestResult(
            channel="gitlab",
            display_name="Code repository",
            passed=True,
            message="Connected",
            duration_ms=150,
        )
        d = result.to_dict()
        assert d["channel"] == "gitlab"
        assert d["passed"] is True
        assert d["duration_ms"] == 150


class TestChannelTester:
    @pytest.fixture
    def tester(self, tmp_path):
        return ChannelTester(project_root=tmp_path)

    def test_gitlab_no_token(self, tester, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        result = tester.test_gitlab()
        assert result.passed is False
        assert result.skipped is True

    def test_gitlab_no_library(self, tester, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        with patch.dict("sys.modules", {"gitlab": None}):
            result = tester.test_gitlab()
            assert result.passed is False

    @patch("axiom.setup.tester.subprocess.check_output")
    def test_pandoc_found(self, mock_check, tester):
        mock_check.return_value = b"pandoc 3.1.0\n"
        result = tester.test_pandoc()
        assert result.passed is True
        assert "pandoc" in result.message.lower()

    @patch("axiom.setup.tester.subprocess.check_output")
    def test_pandoc_not_found(self, mock_check, tester):
        mock_check.side_effect = FileNotFoundError()
        result = tester.test_pandoc()
        assert result.passed is False
        assert "not found" in result.message.lower()

    def test_local_docs_no_dir(self, tester):
        result = tester.test_local_docs()
        assert result.passed is False

    def test_local_docs_with_files(self, tester, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "readme.md").write_text("# Hello")
        (docs / "guide.md").write_text("# Guide")
        result = tester.test_local_docs()
        assert result.passed is True
        assert "2" in result.message

    def test_microsoft_365_missing_creds(self, tester, monkeypatch):
        monkeypatch.delenv("MS_GRAPH_CLIENT_ID", raising=False)
        monkeypatch.delenv("MS_GRAPH_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("MS_GRAPH_TENANT_ID", raising=False)
        result = tester.test_microsoft_365()
        assert result.passed is False
        assert result.skipped is True

    def test_llm_gateway_no_keys(self, tester, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("PRIVATE_LLM_API_KEY", raising=False)
        with patch("socket.create_connection", side_effect=OSError("mocked")), \
             patch("axiom.infra.gateway.CONFIG_DIR", Path("/nonexistent")), \
             patch("axiom.infra.gateway.CONFIG_EXAMPLE_DIR", Path("/nonexistent")):
            result = tester.test_llm_gateway()
        # Without API keys, no config, AND no local LLM, gateway should be unavailable
        assert result.passed is False

    def test_llm_gateway_coherent_response_passes(self, tester):
        from types import SimpleNamespace

        fake_gw = SimpleNamespace(
            available=True,
            complete=lambda *a, **k: SimpleNamespace(
                success=True, text="Hello there friend", provider="local", error=None
            ),
        )
        with patch("axiom.infra.gateway.Gateway", return_value=fake_gw):
            result = tester.test_llm_gateway()
        assert result.passed is True

    def test_llm_gateway_degenerate_output_fails(self, tester):
        # The bonsai fixture: provider responds successfully with looping garbage.
        # resp.success was the only old check — it would have passed. The
        # coherence gate must fail it (axiom-os #499).
        from types import SimpleNamespace

        garbage = "the the the the the the the the the the the the"
        fake_gw = SimpleNamespace(
            available=True,
            complete=lambda *a, **k: SimpleNamespace(
                success=True, text=garbage, provider="local", error=None
            ),
        )
        with patch("axiom.infra.gateway.Gateway", return_value=fake_gw):
            result = tester.test_llm_gateway()
        assert result.passed is False
        assert "incoherent" in result.message.lower()

    def test_run_all(self, tester, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("MS_GRAPH_CLIENT_ID", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        results = tester.run_all()
        assert len(results) == 5
        assert all(isinstance(r, TestResult) for r in results)
        # Each result has a duration
        for r in results:
            assert r.duration_ms >= 0

    def test_result_has_display_name(self, tester, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        result = tester.test_gitlab()
        assert result.display_name != ""
