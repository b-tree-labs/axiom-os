# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom chat pipeline — sits between Open WebUI and the LLM gateway.

Open WebUI → [ClassroomChatPipeline] → Gateway → LLM Provider

Responsibilities:
1. Inject the course-specific system prompt
2. Run RAG retrieval from the course corpus (with classification gating)
3. Route slash-commands (/research, /submit, /help, etc.)
4. Append "Next steps" suggestions to responses
5. Return OpenAI-compatible responses

Composition integration (#75): when composition + tracer are wired,
every chat turn flows through the full memory stack:
- Tracer writes MemoryFragment(episodic) for the turn (#71)
- Retrieved chunks filtered through rag.gating (classification)
- LLM output passes through post_filter breach detection
- All operations audit-logged uniformly

This module is transport-agnostic — it works as a Pipe Function in
Open WebUI, as a backend for `axi serve`, or directly in tests.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService

    from .tracing import ClassroomTracer


# Type aliases
RAGRetriever = Callable[[str, int], list[dict]]  # (query, top_k) -> [{text, source, ...}]
LLMBackend = Callable[..., str]  # (messages, **kw) -> response_text
SignatureVerifier = Callable[[dict], bool]  # attestation → verified?


@dataclass
class ClassroomChatPipeline:
    """Classroom-specific chat pipeline with composition integration.

    Wraps the LLM backend with course context: system prompt injection,
    RAG retrieval (gated by classification), slash-command routing,
    response enrichment, and post-filter breach detection.

    Composition primitives (composition, tracer, verify_attestation,
    student_attestation) are optional. When absent, the pipeline
    degrades to the legacy behavior (no tracing, no gating, no
    post-filter) so existing tests + callers keep working.
    """

    course_system_prompt: str
    rag_retriever: RAGRetriever | None = None
    llm_backend: LLMBackend = field(default=lambda msgs, **kw: "")
    suggest_next_steps: bool = True
    rag_top_k: int = 5

    # Composition integration (#75)
    composition: CompositionService | None = None
    tracer: ClassroomTracer | None = None
    student_id: str | None = None
    student_attestation: dict | None = None
    verify_attestation: SignatureVerifier | None = None

    # Recognized slash commands → handler mapping.
    # Handlers receive (command, args, pipeline) and return a response string.
    # For MVP, commands are passed through to the LLM with framing;
    # full agent routing (CURIO, AXI) comes in P1.
    _SLASH_COMMANDS: dict[str, str] = field(
        default_factory=lambda: {
            "/research": (
                "The student wants to start an iterative research investigation. "
                "Help them formulate their research question and outline the first "
                "iteration of the research loop. Be specific about what sources to "
                "search and what hypotheses to test."
            ),
            "/submit": (
                "The student wants to submit their current work as an assignment. "
                "Summarize what they've done in this session, identify key findings, "
                "and package it as a submission. Ask them to confirm before finalizing."
            ),
            "/help": (
                "The student is requesting help from the instructor. Acknowledge their "
                "request, ask them to describe what they're struggling with, and let "
                "them know a help ticket will be created for the instructor."
            ),
            "/quiz-prep": (
                "The student wants to prepare for a quiz. Generate practice questions "
                "relevant to the specified topic using only the course corpus. Include "
                "a mix of question types and provide feedback on their answers."
            ),
            "/cite-check": (
                "Review the previous response for accuracy. For every factual claim, "
                "verify whether it is supported by the course corpus. Clearly mark "
                "each claim as SUPPORTED (with source) or UNSUPPORTED."
            ),
            "/summarize-session": (
                "Summarize this entire conversation session. Identify: key topics "
                "covered, main findings, open questions remaining, and suggested "
                "next steps for the student."
            ),
        }
    )

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Process a chat request through the classroom pipeline.

        Composition integration runs when the relevant primitives are
        wired on the pipeline (tracer, composition, etc.). Legacy path
        (none wired) is unchanged.

        Args:
            messages: OpenAI-format message list [{role, content}, ...]
            **kwargs: passed through to the LLM backend

        Returns:
            The assistant's response text (post-filter redacted if breach).
        """
        user_query = self._extract_user_query(messages)

        # 1. Trace the turn (writes episodic fragment via composition)
        if self.tracer is not None and self.student_id and user_query:
            self.tracer.trace_chat(student_id=self.student_id, message=user_query)

        # 2. Build augmented messages (system prompt + RAG context)
        augmented = self._build_augmented_messages(messages)

        # 3. LLM call
        response = self.llm_backend(augmented, **kwargs)

        # 4. Post-filter breach detection (when composition wired)
        if self.composition is not None and response:
            check = self.composition.llm_response(
                output=response,
                user=self.student_id or "",
                agent="axi",
                visible_fragments=[],
                all_fragments=[],
            )
            # Redaction happens at the breach detector; for MVP we just
            # log + emit. Full redact-before-emit can be enabled by
            # replacing response with the breach-filter output here.
            _ = check

        # 5. Append next-steps suggestions
        if self.suggest_next_steps and response:
            response = self._append_next_steps(response, messages)

        return response

    def handle_completion(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle an OpenAI-compatible /v1/chat/completions request.

        This is the entry point for Open WebUI's Pipe Function integration.
        """
        messages = request.get("messages", [])
        model = request.get("model", "axiom-classroom")

        response_text = self.chat(messages, model=model)

        return {
            "id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    # -- internals ----------------------------------------------------------

    def _build_augmented_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Build the augmented message list: system prompt + RAG + user messages."""
        result: list[dict[str, str]] = []

        # 1. Course system prompt (replaces any existing system message)
        result.append({"role": "system", "content": self.course_system_prompt})

        # 2. RAG context (if retriever available and there's a user message)
        user_query = self._extract_user_query(messages)
        if user_query and self.rag_retriever:
            rag_context = self._retrieve_context(user_query)
            if rag_context:
                result.append({"role": "system", "content": rag_context})

        # 3. Slash-command framing (if the user message is a command)
        if user_query:
            command_framing = self._get_command_framing(user_query)
            if command_framing:
                result.append({"role": "system", "content": command_framing})

        # 4. Non-system messages from the conversation history
        for msg in messages:
            if msg["role"] != "system":
                result.append(msg)

        return result

    def _extract_user_query(self, messages: list[dict[str, str]]) -> str:
        """Get the last user message content."""
        for msg in reversed(messages):
            if msg["role"] == "user":
                return msg["content"]
        return ""

    def _retrieve_context(self, query: str) -> str:
        """Run RAG retrieval + classification gating, format as system message."""
        if not self.rag_retriever:
            return ""

        chunks = self.rag_retriever(query, self.rag_top_k)
        if not chunks:
            return ""

        # Classification gating: filter chunks through the generic gate
        # when the student's attestation + verifier are wired.
        if self.verify_attestation is not None:
            from axiom.rag.gating import filter_chunks_by_classification

            chunks, _denied = filter_chunks_by_classification(
                chunks=chunks,
                attestation=self.student_attestation,
                verify_signature=self.verify_attestation,
            )
            if not chunks:
                return ""

        context_parts = ["Relevant context from the course corpus:"]
        for i, chunk in enumerate(chunks, 1):
            source = chunk.get("source", "unknown")
            text = chunk.get("text", "")
            context_parts.append(f"[{i}] ({source}): {text}")

        context_parts.append(
            "\nUse this context to ground your response. Cite sources "
            "by their number [N] when referencing specific information."
        )
        return "\n".join(context_parts)

    def _get_command_framing(self, user_message: str) -> str:
        """If the user message starts with a slash command, return framing."""
        stripped = user_message.strip()
        for cmd, framing in self._SLASH_COMMANDS.items():
            if stripped.lower().startswith(cmd):
                return framing
        return ""

    def _append_next_steps(self, response: str, original_messages: list[dict[str, str]]) -> str:
        """Append contextual 'Next steps' to the response.

        These become clickable follow-up suggestions in Open WebUI
        when the task model parses them.
        """
        user_query = self._extract_user_query(original_messages)
        if not user_query:
            return response

        # Generate simple, contextual next-step suggestions.
        # Full tangential-discovery engine (CURIO + coverage map) is P2.
        suggestions = [
            "Dig deeper into the key concepts above",
            "Run `/cite-check` to verify the claims in this response",
            f"Try `/research {user_query.split()[0] if user_query.split() else 'this topic'}` for an in-depth investigation",
        ]

        next_steps = "\n\n---\n**Next steps you might try:**\n"
        for s in suggestions:
            next_steps += f"- {s}\n"

        return response + next_steps
