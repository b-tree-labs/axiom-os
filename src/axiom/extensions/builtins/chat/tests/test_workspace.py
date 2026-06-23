# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for workspace context detection, /save command, and enhanced system prompt."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Workspace context detection
# ---------------------------------------------------------------------------


class TestDetectWorkspaceContext:
    """Tests for detect_workspace_context()."""

    def test_returns_empty_when_no_model_yaml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from axiom.extensions.builtins.chat.workspace import detect_workspace_context

        assert detect_workspace_context() == ""

    def test_returns_context_when_model_yaml_exists(self, tmp_path, monkeypatch):
        model_yaml = tmp_path / "model.yaml"
        model_yaml.write_text(
            "model_id: triga-ss\n"
            "reactor_type: TRIGA\n"
            "physics_code: MCNP\n"
            "version: 0.1.0\n"
            "materials:\n"
            "  - name: UZrH-20\n"
            "  - name: H2O\n"
            "  - name: graphite\n"
            "input_files:\n"
            "  - path: triga.i\n"
            "description: Steady-state TRIGA model\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from axiom.extensions.builtins.chat.workspace import detect_workspace_context

        ctx = detect_workspace_context()
        assert "triga-ss" in ctx
        assert "TRIGA" in ctx
        assert "MCNP" in ctx
        assert "0.1.0" in ctx

    def test_extracts_materials(self, tmp_path, monkeypatch):
        model_yaml = tmp_path / "model.yaml"
        model_yaml.write_text(
            "model_id: test\nversion: 1.0\nmaterials:\n  - name: UZrH-20\n  - name: H2O\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from axiom.extensions.builtins.chat.workspace import detect_workspace_context

        ctx = detect_workspace_context()
        assert "UZrH-20" in ctx
        assert "H2O" in ctx

    def test_extracts_model_id_and_reactor_type(self, tmp_path, monkeypatch):
        model_yaml = tmp_path / "model.yaml"
        model_yaml.write_text(
            "model_id: pwr-core\nreactor_type: PWR\nphysics_code: Serpent\nversion: 2.0\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from axiom.extensions.builtins.chat.workspace import detect_workspace_context

        ctx = detect_workspace_context()
        assert "pwr-core" in ctx
        assert "PWR" in ctx
        assert "Serpent" in ctx

    def test_finds_model_yaml_in_parent(self, tmp_path, monkeypatch):
        model_yaml = tmp_path / "model.yaml"
        model_yaml.write_text("model_id: parent-model\nversion: 1.0\n", encoding="utf-8")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        from axiom.extensions.builtins.chat.workspace import detect_workspace_context

        ctx = detect_workspace_context()
        assert "parent-model" in ctx

    def test_handles_invalid_yaml(self, tmp_path, monkeypatch):
        model_yaml = tmp_path / "model.yaml"
        model_yaml.write_text("just a string", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        from axiom.extensions.builtins.chat.workspace import detect_workspace_context

        assert detect_workspace_context() == ""

    def test_skips_todo_description(self, tmp_path, monkeypatch):
        model_yaml = tmp_path / "model.yaml"
        model_yaml.write_text(
            "model_id: test\nversion: 1.0\ndescription: TODO fill in\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from axiom.extensions.builtins.chat.workspace import detect_workspace_context

        ctx = detect_workspace_context()
        assert "TODO" not in ctx


# ---------------------------------------------------------------------------
# /save command
# ---------------------------------------------------------------------------


class TestCmdSave:
    """Tests for the /save slash command."""

    def test_save_no_conversation(self):
        from axiom.extensions.builtins.chat.commands import cmd_save

        session = MagicMock()
        session.messages = []
        result = cmd_save(session)
        assert "Nothing to save" in result

    def test_save_no_assistant_message(self):
        from axiom.extensions.builtins.chat.commands import cmd_save

        msg = MagicMock()
        msg.role = "user"
        msg.content = "hello"
        session = MagicMock()
        session.messages = [msg]
        result = cmd_save(session)
        assert "No assistant response" in result

    def test_save_records_to_knowledge_metrics(self):
        from axiom.extensions.builtins.chat.commands import cmd_save

        user_msg = MagicMock()
        user_msg.role = "user"
        user_msg.content = "What is k-eff?"

        asst_msg = MagicMock()
        asst_msg.role = "assistant"
        asst_msg.content = "k-eff is the effective multiplication factor."

        session = MagicMock()
        session.messages = [user_msg, asst_msg]
        session.session_id = "test-session-12345678"

        mock_svc = MagicMock()
        mock_km = MagicMock()
        mock_km.KnowledgeMetricsService = MagicMock(return_value=mock_svc)

        with patch.dict("sys.modules", {"axiom.vega.federation.knowledge_metrics": mock_km}):
            result = cmd_save(session)
            assert "Saved to local knowledge corpus" in result
            mock_svc.record_event.assert_called_once()


# ---------------------------------------------------------------------------
# System prompt enhancements
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    """Tests for system prompt workspace context and nuclear guidance."""

    def test_system_prompt_includes_workspace_context(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent

        agent = ChatAgent()
        agent._workspace_context = "Working on model: triga-ss v0.1.0 (TRIGA MCNP)"
        prompt = agent._build_system_prompt()
        assert "triga-ss" in prompt
        assert "Active workspace" in prompt

    def test_system_prompt_no_workspace_when_empty(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent

        agent = ChatAgent()
        agent._workspace_context = ""
        prompt = agent._build_system_prompt()
        assert "Active workspace" not in prompt

    def test_system_prompt_includes_contributed_fragments(self):
        """A consumer extension contributes role/policy fragments via the
        prompt_contributor hook; the platform composes them by name, naming no
        specific consumer."""
        from axiom.extensions.builtins.chat import agent as agent_mod
        from axiom.extensions.builtins.chat.agent import ChatAgent

        def fake_contributions():
            return [
                {"layer": "identity", "name": "consumer_role",
                 "content": "You are a domain assistant for an example consumer.",
                 "source": "example_consumer", "required": True},
            ]

        with patch.object(agent_mod, "_discover_prompt_contributions", fake_contributions):
            agent = ChatAgent()
            prompt = agent._build_system_prompt()
            assert "domain assistant for an example consumer" in prompt

    def test_system_prompt_no_contributions_when_none_registered(self):
        from axiom.extensions.builtins.chat import agent as agent_mod
        from axiom.extensions.builtins.chat.agent import ChatAgent

        with patch.object(agent_mod, "_discover_prompt_contributions", lambda: []):
            agent = ChatAgent()
            prompt = agent._build_system_prompt()
            assert "domain assistant" not in prompt


# ---------------------------------------------------------------------------
# Workspace summary line
# ---------------------------------------------------------------------------


class TestWorkspaceSummaryLine:
    def test_empty_input(self):
        from axiom.extensions.builtins.chat.workspace import workspace_summary_line

        assert workspace_summary_line("") == ""

    def test_extracts_first_line(self):
        from axiom.extensions.builtins.chat.workspace import workspace_summary_line

        ctx = "Working on model: triga-ss v0.1.0 (TRIGA MCNP)\nMaterials: UZrH-20"
        assert workspace_summary_line(ctx) == "Working on model: triga-ss v0.1.0 (TRIGA MCNP)"
