# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the LLM-mediated fallback diagnosis path.

Per the user's 2026-05-03 directive: when the catalog has no match,
TRIAGE falls back to Qwen + RAG to generate a best-guess diagnosis.
Loop protection: if the failure is LLM-related, skip the fallback
(would recursively hit the same broken model).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.diagnostics import cli_diagnoses

# ---------------------------------------------------------------------------
# Stubs — let tests stay deterministic and offline.
# ---------------------------------------------------------------------------


@dataclass
class _StubResponse:
    text: str
    success: bool = True
    provider: str = "stub"
    error: str | None = None


class _StubGateway:
    """Implements the `complete` shape Gateway.complete returns."""

    def __init__(self, text: str = "", success: bool = True) -> None:
        self._text = text
        self._success = success
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    def complete(
        self, prompt: str, system: str = "", task: str = "", max_tokens: int = 0
    ) -> _StubResponse:
        self.last_prompt = prompt
        self.last_system = system
        return _StubResponse(text=self._text, success=self._success)


def _generic_event(error_message: str = "config file is malformed") -> dict[str, Any]:
    return {
        "command": "ext",
        "argv": ["axi", "ext", "list"],
        "error_type": "ValueError",
        "error_message": error_message,
        "traceback": "ValueError: config file is malformed\n",
        "fingerprint": "ext:ValueError:config",
        "recovered": False,
        "environment": {},
        "timestamp": "2026-05-03T20:00:00Z",
    }


def _llm_related_event() -> dict[str, Any]:
    return {
        "command": "chat",
        "argv": ["axi", "chat"],
        "error_type": "ConnectionError",
        "error_message": "qwen-<remote> endpoint unreachable: gateway timed out",
        "traceback": "...gateway.py: ConnectionError\n",
        "fingerprint": "chat:ConnectionError:qwen",
        "recovered": False,
        "environment": {},
        "timestamp": "2026-05-03T20:00:00Z",
    }


# ---------------------------------------------------------------------------
# Loop protection
# ---------------------------------------------------------------------------


class TestLLMRelatedSkipsFallback:
    def test_qwen_error_does_not_call_llm(self) -> None:
        gateway = _StubGateway(
            text='{"summary": "should not be reached", "remedy": "should not be reached"}'
        )

        diagnosis = cli_diagnoses.diagnose(
            _llm_related_event(), gateway=gateway, allow_llm=True
        )

        assert diagnosis is None
        # Confirm the gateway was never invoked.
        assert gateway.last_prompt is None

    def test_gateway_keyword_alone_blocks_fallback(self) -> None:
        event = _generic_event(error_message="LLM gateway initialization crashed")
        gateway = _StubGateway(text="{}")

        diagnosis = cli_diagnoses.diagnose(event, gateway=gateway)

        assert diagnosis is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLLMFallbackProducesDiagnosis:
    def test_returns_diagnosis_from_valid_json(self) -> None:
        gateway = _StubGateway(
            text=(
                '{"summary": "your config file is missing a required field",'
                ' "remedy": "add `name = \\"my-ext\\"` to axiom-extension.toml"}'
            )
        )

        diagnosis = cli_diagnoses.diagnose(_generic_event(), gateway=gateway)

        assert diagnosis is not None
        assert "config" in diagnosis.summary.lower()
        assert "axiom-extension.toml" in diagnosis.remedy
        # LLM-fallback diagnoses are lower confidence than catalog matches.
        assert 0.5 <= diagnosis.confidence < 0.9

    def test_pattern_id_marks_diagnosis_as_llm_fallback(self) -> None:
        gateway = _StubGateway(
            text='{"summary": "x", "remedy": "y"}'
        )

        diagnosis = cli_diagnoses.diagnose(_generic_event(), gateway=gateway)

        assert diagnosis is not None
        assert diagnosis.pattern_id.startswith("llm-fallback")

    def test_distinct_errors_get_distinct_fingerprints(self) -> None:
        """Two unrelated unknown errors must NOT dedupe under one
        llm-fallback id (the catalog deliberately dedupes; LLM fallback
        must not)."""
        gateway = _StubGateway(text='{"summary": "x", "remedy": "y"}')

        d1 = cli_diagnoses.diagnose(
            _generic_event(error_message="error A"), gateway=gateway
        )
        d2 = cli_diagnoses.diagnose(
            _generic_event(error_message="error B"), gateway=gateway
        )

        assert d1 is not None and d2 is not None
        assert d1.fingerprint != d2.fingerprint

    def test_prompt_includes_command_and_error(self) -> None:
        gateway = _StubGateway(text='{"summary": "x", "remedy": "y"}')

        cli_diagnoses.diagnose(_generic_event(), gateway=gateway)

        assert gateway.last_prompt is not None
        assert "ext" in gateway.last_prompt  # command
        assert "config file is malformed" in gateway.last_prompt  # error_message


# ---------------------------------------------------------------------------
# Failure modes — must always soft-fail to None, never raise.
# ---------------------------------------------------------------------------


class TestLLMFallbackResilience:
    def test_unsuccessful_response_returns_none(self) -> None:
        gateway = _StubGateway(text="", success=False)

        assert cli_diagnoses.diagnose(_generic_event(), gateway=gateway) is None

    def test_non_json_response_returns_none(self) -> None:
        gateway = _StubGateway(text="I think you should reboot.")

        assert cli_diagnoses.diagnose(_generic_event(), gateway=gateway) is None

    def test_empty_summary_returns_none(self) -> None:
        gateway = _StubGateway(text='{"summary": "", "remedy": "do x"}')

        assert cli_diagnoses.diagnose(_generic_event(), gateway=gateway) is None

    def test_empty_remedy_returns_none(self) -> None:
        gateway = _StubGateway(text='{"summary": "x", "remedy": ""}')

        assert cli_diagnoses.diagnose(_generic_event(), gateway=gateway) is None

    def test_extracts_json_from_fenced_response(self) -> None:
        """Some models wrap JSON in ```json fences. Be tolerant."""
        gateway = _StubGateway(
            text='```json\n{"summary": "boom", "remedy": "fix it"}\n```'
        )

        d = cli_diagnoses.diagnose(_generic_event(), gateway=gateway)

        assert d is not None
        assert d.summary == "boom"


# ---------------------------------------------------------------------------
# Catalog still wins when both could match
# ---------------------------------------------------------------------------


class TestCatalogWinsOverLLM:
    def test_bonsai_match_does_not_call_llm(self) -> None:
        event = {
            "command": "chat",
            "argv": ["axi", "chat"],
            "error_type": "OSError",
            "error_message": "[Errno 22] Invalid argument: bonsai-1.7b.gguf",
            "traceback": "OSError: bonsai-1.7b.gguf\n",
            "fingerprint": "chat:OSError:bonsai",
            "recovered": False,
            "environment": {},
            "timestamp": "2026-05-03T20:00:00Z",
        }
        gateway = _StubGateway(text='{"summary": "wrong answer", "remedy": "wrong"}')

        diagnosis = cli_diagnoses.diagnose(event, gateway=gateway, allow_llm=True)

        assert diagnosis is not None
        assert diagnosis.pattern_id == "bonsai-deprecated"
        # Gateway never asked.
        assert gateway.last_prompt is None


# ---------------------------------------------------------------------------
# Allow-llm flag — keeps tests/CI offline by default.
# ---------------------------------------------------------------------------


class TestAllowLLMFlag:
    def test_allow_llm_false_skips_fallback(self) -> None:
        gateway = _StubGateway(text='{"summary": "x", "remedy": "y"}')

        diagnosis = cli_diagnoses.diagnose(
            _generic_event(), gateway=gateway, allow_llm=False
        )

        assert diagnosis is None
        assert gateway.last_prompt is None
