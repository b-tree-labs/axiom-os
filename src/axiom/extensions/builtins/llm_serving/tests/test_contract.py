# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the serving-contract smoke (PIVOT-8 / #49).

The smoke is the cutover + CI gate for the vLLM+LiteLLM stack. It must:
  * assert /v1/models lists at least one model;
  * assert /v1/chat/completions returns grounded content — accepting EITHER
    `content` OR `reasoning_content` (reasoning models put the answer in the
    latter), so a reasoning model never reads as "empty";
  * size the timeout to real model latency and distinguish SLOW from BROKEN —
    a slow-but-correct answer is NOT a rollback trigger (the false-rollback bug
    this replaces used a too-short timeout + content-only check).

run_contract_smoke is pure: the HTTP get/post callables + a clock are injected,
so these tests make no network calls and control latency deterministically.
"""

from __future__ import annotations

from axiom.extensions.builtins.llm_serving.contract import (
    SmokeVerdict,
    run_contract_smoke,
)


def _models_ok(url, headers=None):
    return 200, {"data": [{"id": "qwen"}, {"id": "rag-model"}]}


def _chat_content(url, headers=None, json=None):
    return 200, {"choices": [{"message": {"content": "Hello, I am grounded."},
                              "finish_reason": "stop"}]}


def _chat_reasoning_only(url, headers=None, json=None):
    # Reasoning model: answer is in reasoning_content, content empty.
    return 200, {"choices": [{"message": {"content": "",
                 "reasoning_content": "Let me think... the answer is 42."},
                 "finish_reason": "stop"}]}


class _Clock:
    """Deterministic monotonic clock; advances by `step` each read."""

    def __init__(self, step=0.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def test_healthy_when_models_and_content_present():
    result = run_contract_smoke(
        "https://localhost:41883", model="qwen",
        http_get=_models_ok, http_post=_chat_content,
        clock=_Clock(step=0.5), timeout_s=60, warn_latency_s=20,
    )
    assert result.verdict is SmokeVerdict.HEALTHY
    assert result.passed is True
    assert all(c.status == "pass" for c in result.checks)


def test_reasoning_content_counts_as_grounded():
    result = run_contract_smoke(
        "https://localhost:41883", model="qwen",
        http_get=_models_ok, http_post=_chat_reasoning_only,
        clock=_Clock(step=0.5), timeout_s=60, warn_latency_s=20,
    )
    # reasoning_content alone must NOT read as empty -> still passes
    assert result.passed is True
    assert result.verdict is SmokeVerdict.HEALTHY


def test_empty_response_is_broken():
    def _chat_empty(url, headers=None, json=None):
        return 200, {"choices": [{"message": {"content": ""},
                                  "finish_reason": "stop"}]}

    result = run_contract_smoke(
        "https://localhost:41883", model="qwen",
        http_get=_models_ok, http_post=_chat_empty,
        clock=_Clock(step=0.5), timeout_s=60, warn_latency_s=20,
    )
    assert result.passed is False
    assert result.verdict is SmokeVerdict.BROKEN


def test_slow_but_correct_is_not_a_rollback():
    # 35s latency: above the 20s warn threshold but well under the 60s hard
    # timeout, and the content is good. This must PASS (verdict SLOW), not roll
    # back — the exact false-rollback this smoke is built to prevent.
    result = run_contract_smoke(
        "https://localhost:41883", model="qwen",
        http_get=_models_ok, http_post=_chat_content,
        clock=_Clock(step=35.0), timeout_s=60, warn_latency_s=20,
    )
    assert result.verdict is SmokeVerdict.SLOW
    assert result.passed is True  # slow is a warning, not a gate failure


def test_models_error_is_broken_and_short_circuits():
    posted = {"called": False}

    def _post(url, headers=None, json=None):
        posted["called"] = True
        return 200, {}

    def _models_err(url, headers=None):
        return 503, {"error": "service unavailable"}

    result = run_contract_smoke(
        "https://localhost:41883", model="qwen",
        http_get=_models_err, http_post=_post,
        clock=_Clock(step=0.1), timeout_s=60, warn_latency_s=20,
    )
    assert result.passed is False
    assert result.verdict is SmokeVerdict.BROKEN
    # no point calling chat if the server can't even list models
    assert posted["called"] is False


def test_chat_http_error_is_broken():
    def _chat_500(url, headers=None, json=None):
        return 500, {"error": "internal"}

    result = run_contract_smoke(
        "https://localhost:41883", model="qwen",
        http_get=_models_ok, http_post=_chat_500,
        clock=_Clock(step=0.5), timeout_s=60, warn_latency_s=20,
    )
    assert result.passed is False
    assert result.verdict is SmokeVerdict.BROKEN


def test_exception_during_call_is_broken_not_raised():
    def _boom(url, headers=None):
        raise ConnectionError("connection refused")

    result = run_contract_smoke(
        "https://localhost:41883", model="qwen",
        http_get=_boom, http_post=_chat_content,
        clock=_Clock(step=0.1), timeout_s=60, warn_latency_s=20,
    )
    assert result.passed is False
    assert result.verdict is SmokeVerdict.BROKEN
