# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the classroom chat pipeline.

The pipeline sits between Open WebUI and the LLM gateway:
  Open WebUI → [Classroom Pipeline] → Gateway → LLM Provider

The pipeline:
1. Identifies the student + course from the request context
2. Injects the course system prompt
3. Runs RAG retrieval from the course corpus
4. Passes the augmented messages to the gateway
5. Appends "Next steps" suggestions to the response
6. Returns the OpenAI-compatible response to Open WebUI
"""

from __future__ import annotations


class TestClassroomPipelineInit:
    def test_import(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        assert ClassroomChatPipeline is not None

    def test_creates_with_config(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a helpful tutor for STEM Course 2026.",
            rag_retriever=None,  # no RAG for this test
            llm_backend=lambda messages, **kw: "Hello, student!",
        )
        assert pipeline is not None


class TestSystemPromptInjection:
    def test_course_system_prompt_prepended(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        captured = {}

        def mock_backend(messages, **kw):
            captured["messages"] = messages
            return "Response."

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a tutor for Nuclear Engineering.",
            rag_retriever=None,
            llm_backend=mock_backend,
        )

        pipeline.chat([{"role": "user", "content": "What is fission?"}])

        # System prompt should be the first message
        assert captured["messages"][0]["role"] == "system"
        assert "Nuclear Engineering" in captured["messages"][0]["content"]
        # User message follows
        assert captured["messages"][1]["role"] == "user"
        assert captured["messages"][1]["content"] == "What is fission?"

    def test_existing_system_prompt_is_replaced(self):
        """If Open WebUI sends a system prompt, ours takes precedence."""
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        captured = {}

        def mock_backend(messages, **kw):
            captured["messages"] = messages
            return "Response."

        pipeline = ClassroomChatPipeline(
            course_system_prompt="Course-specific prompt.",
            rag_retriever=None,
            llm_backend=mock_backend,
        )

        pipeline.chat(
            [
                {"role": "system", "content": "Open WebUI default system prompt"},
                {"role": "user", "content": "Hello"},
            ]
        )

        # Only one system message, and it's ours
        system_msgs = [m for m in captured["messages"] if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert "Course-specific" in system_msgs[0]["content"]


class TestRAGRetrieval:
    def test_rag_context_injected_before_user_message(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        captured = {}

        def mock_backend(messages, **kw):
            captured["messages"] = messages
            return "Response with context."

        def mock_retriever(query: str, top_k: int = 3):
            return [
                {
                    "text": "Fission splits heavy nuclei into lighter ones.",
                    "source": "textbook ch3",
                },
                {"text": "Chain reactions sustain fission in reactors.", "source": "textbook ch4"},
            ]

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            rag_retriever=mock_retriever,
            llm_backend=mock_backend,
        )

        pipeline.chat([{"role": "user", "content": "Explain fission"}])

        # RAG context should appear as a system message between the system prompt and user message
        assert len(captured["messages"]) >= 3
        rag_msg = captured["messages"][1]
        assert rag_msg["role"] == "system"
        assert "Fission splits heavy nuclei" in rag_msg["content"]
        assert "textbook ch3" in rag_msg["content"]

    def test_no_rag_when_retriever_is_none(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        captured = {}

        def mock_backend(messages, **kw):
            captured["messages"] = messages
            return "Response."

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            rag_retriever=None,
            llm_backend=mock_backend,
        )

        pipeline.chat([{"role": "user", "content": "Hello"}])

        # Only system prompt + user message, no RAG injection
        assert len(captured["messages"]) == 2


class TestNextStepsSuggestions:
    def test_response_includes_next_steps(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            rag_retriever=None,
            llm_backend=lambda messages, **kw: "Fission is the splitting of atoms.",
            suggest_next_steps=True,
        )

        result = pipeline.chat([{"role": "user", "content": "What is fission?"}])

        assert "Next steps" in result or "next steps" in result.lower()

    def test_suggestions_disabled_returns_raw(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            rag_retriever=None,
            llm_backend=lambda messages, **kw: "Fission is the splitting of atoms.",
            suggest_next_steps=False,
        )

        result = pipeline.chat([{"role": "user", "content": "What is fission?"}])

        assert "Next steps" not in result


class TestOpenAICompatibleResponse:
    def test_handle_returns_openai_format(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            rag_retriever=None,
            llm_backend=lambda messages, **kw: "Hello!",
        )

        response = pipeline.handle_completion(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "model": "axiom-classroom",
            }
        )

        assert response["object"] == "chat.completion"
        assert response["choices"][0]["message"]["role"] == "assistant"
        assert "Hello!" in response["choices"][0]["message"]["content"]
        assert "id" in response
        assert "created" in response


class TestSlashCommandRouting:
    def test_research_command_recognized(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        captured = {}

        def mock_backend(messages, **kw):
            captured["messages"] = messages
            return "Starting research loop on Topic X..."

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            rag_retriever=None,
            llm_backend=mock_backend,
        )

        result = pipeline.chat([{"role": "user", "content": "/research fusion energy"}])

        # Pipeline should recognize /research and route appropriately
        # (for now: the command is passed through; full CURIO integration is P1)
        assert "fusion energy" in result.lower() or "research" in result.lower()

    def test_submit_command_recognized(self):
        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        pipeline = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            rag_retriever=None,
            llm_backend=lambda messages, **kw: "Submission acknowledged.",
        )

        result = pipeline.chat([{"role": "user", "content": "/submit homework-1"}])
        assert "submit" in result.lower() or "homework" in result.lower()
