# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the claude-code headless gateway provider.

The backend is the native Claude Code CLI in ``-p`` mode riding the
user's existing login — the no-API-key path. A fake ``claude`` binary
on PATH stands in for the real one; it fails loudly if the auth env
vars were not scrubbed, which pins the subscription-auth guarantee and
the base-url loop guard.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def gateway(tmp_path: Path):
    from axiom.llm.gateway import Gateway

    empty = tmp_path / "no-config"
    empty.mkdir()
    return Gateway(config_dir=empty)


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch):
    """A `claude` stand-in that asserts env scrubbing and echoes JSON."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "claude"
    script.write_text(
        "#!/bin/bash\n"
        'if [ -n "$ANTHROPIC_API_KEY" ] || [ -n "$ANTHROPIC_AUTH_TOKEN" ]'
        ' || [ -n "$ANTHROPIC_BASE_URL" ]; then\n'
        '  echo "auth-env-not-scrubbed" >&2; exit 7\n'
        "fi\n"
        "echo '{\"type\":\"result\",\"result\":\"FAKE-OK\"}'\n"
    )
    script.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    # Simulate the dangerous env the scrub must remove.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-would-be-metered")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8788")
    return script


_PROVIDER = SimpleNamespace(
    name="claude-code", endpoint="claude-code://local", model="headless",
    api_key=None,
)


class TestRunClaudeCode:
    def test_success_scrubs_auth_env(self, gateway, fake_claude):
        ok, text = gateway._run_claude_code("hello", "")
        assert ok, text
        assert text == "FAKE-OK"

    def test_system_prompt_is_appended_flag(self, gateway, tmp_path, monkeypatch):
        bindir = tmp_path / "bin2"
        bindir.mkdir()
        script = bindir / "claude"
        script.write_text(
            "#!/bin/bash\n"
            'for a in "$@"; do\n'
            '  if [ "$a" = "--append-system-prompt" ]; then'
            "    echo '{\"result\":\"SAW-SYSTEM\"}'; exit 0; fi\n"
            "done\n"
            "echo '{\"result\":\"NO-SYSTEM\"}'\n"
        )
        script.chmod(0o755)
        import os

        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        assert gateway._run_claude_code("q", "be brief")[1] == "SAW-SYSTEM"
        assert gateway._run_claude_code("q", "")[1] == "NO-SYSTEM"

    def test_missing_binary_fails_closed(self, gateway, monkeypatch, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        ok, text = gateway._run_claude_code("hello", "")
        assert not ok
        assert "not found" in text

    def test_nonzero_exit_reports_detail(self, gateway, tmp_path, monkeypatch):
        bindir = tmp_path / "bin3"
        bindir.mkdir()
        script = bindir / "claude"
        script.write_text("#!/bin/bash\necho boom >&2; exit 3\n")
        script.chmod(0o755)
        import os

        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        ok, text = gateway._run_claude_code("hello", "")
        assert not ok
        assert "exit 3" in text and "boom" in text


class TestDispatch:
    def test_call_provider_routes_claude_code(self, gateway, fake_claude):
        resp = gateway._call_provider(_PROVIDER, "hi", "", 100)
        assert resp.success
        assert resp.text == "FAKE-OK"
        assert resp.provider == "claude-code"

    def test_tools_are_ignored_and_reply_is_prose(self, gateway, fake_claude):
        resp = gateway._call_provider_with_tools(
            _PROVIDER,
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "user", "content": [{"type": "text", "text": "there"}]},
            ],
            system="",
            tools=[{"type": "function", "function": {"name": "shell"}}],
            max_tokens=100,
        )
        assert resp.success
        assert resp.text == "FAKE-OK"
        assert resp.tool_use == []
        assert resp.stop_reason == "end_turn"
