# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for graceful tool-failure handling.

Closes the parity-doc gap 'Graceful degradation / error boundaries
(typed fallback on tool failure)'. Without this, every tool exception
becomes an opaque ``{"error": "..."}`` blob the LLM has to interpret —
the result is unpredictable retries, retries that won't help (auth
failures), and silent degradation. With this:

  - Exceptions are classified into a small typed taxonomy.
  - Transient errors get an automatic retry with jittered backoff
    BEFORE the result is fed back to the LLM.
  - Errors that won't fix themselves (auth, invalid input) skip the
    retry and surface immediately so the LLM can take a different path.
  - The structured error dict carries enough metadata for the LLM to
    make an intelligent next move (error_type, retryable, suggested
    alternatives, federation_fallback hint).

Federation-aware fallback (try peer when local fails) is data-model-
only in v0 — the field is in the schema, the wiring lands when
ComputeDecomposition (ADR-040) ships peer tool dispatch.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_classify_connection_error_as_transient():
    import requests

    from axiom.extensions.builtins.chat.tool_errors import classify_exception

    err = classify_exception(requests.exceptions.ConnectionError("boom"))
    assert err.error_type == "transient"
    assert err.retryable is True
    assert "ConnectionError" in err.error_class


def test_classify_timeout_as_transient():
    import requests

    from axiom.extensions.builtins.chat.tool_errors import classify_exception

    err = classify_exception(requests.exceptions.ReadTimeout("slow"))
    assert err.error_type == "transient"
    assert err.retryable is True


def test_classify_permission_error_not_retryable():
    from axiom.extensions.builtins.chat.tool_errors import classify_exception

    err = classify_exception(PermissionError("denied"))
    assert err.error_type == "permission_denied"
    assert err.retryable is False


def test_classify_file_not_found_not_retryable():
    from axiom.extensions.builtins.chat.tool_errors import classify_exception

    err = classify_exception(FileNotFoundError("missing"))
    assert err.error_type == "not_found"
    assert err.retryable is False


def test_classify_value_error_as_invalid_input():
    from axiom.extensions.builtins.chat.tool_errors import classify_exception

    err = classify_exception(ValueError("bad input"))
    assert err.error_type == "invalid_input"
    assert err.retryable is False


def test_classify_keyerror_as_invalid_input():
    from axiom.extensions.builtins.chat.tool_errors import classify_exception

    err = classify_exception(KeyError("missing-key"))
    assert err.error_type == "invalid_input"
    assert err.retryable is False


def test_classify_unknown_exception():
    from axiom.extensions.builtins.chat.tool_errors import classify_exception

    class WeirdError(RuntimeError):
        pass

    err = classify_exception(WeirdError("?!"))
    assert err.error_type == "unknown"
    # Unknown errors get a single conservative retry — they MIGHT be transient.
    assert err.retryable is True


def test_typed_error_to_dict_shape():
    """The structured shape that goes back to the LLM is stable + documented."""
    from axiom.extensions.builtins.chat.tool_errors import classify_exception

    err = classify_exception(TimeoutError("read"))
    d = err.to_dict()
    assert d["error_type"] == "transient"
    assert "error_class" in d
    assert "message" in d
    assert "retryable" in d
    assert "federation_fallback_eligible" in d


def test_run_with_retry_succeeds_after_transient(monkeypatch):
    """Auto-retry: a transient failure followed by success returns the success
    without bubbling the error to the LLM."""
    import requests

    from axiom.extensions.builtins.chat.tool_errors import run_with_retry

    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.tool_errors.time.sleep",
        lambda *_: None,
    )

    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.exceptions.ConnectionError("transient")
        return {"ok": True}

    result = run_with_retry(flaky)
    assert result == {"ok": True}
    assert len(attempts) == 2


def test_run_with_retry_gives_up_after_max_attempts(monkeypatch):
    import requests

    from axiom.extensions.builtins.chat.tool_errors import run_with_retry

    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.tool_errors.time.sleep",
        lambda *_: None,
    )

    attempts = []

    def always_fails():
        attempts.append(1)
        raise requests.exceptions.ConnectionError("never")

    with pytest.raises(requests.exceptions.ConnectionError):
        run_with_retry(always_fails, max_attempts=3)
    assert len(attempts) == 3


def test_run_with_retry_no_retry_for_permission_error(monkeypatch):
    """Auth failures bubble immediately — retry won't fix them."""
    from axiom.extensions.builtins.chat.tool_errors import run_with_retry

    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.tool_errors.time.sleep",
        lambda *_: None,
    )

    attempts = []

    def denied():
        attempts.append(1)
        raise PermissionError("nope")

    with pytest.raises(PermissionError):
        run_with_retry(denied, max_attempts=5)
    # Only one attempt — no retry for permission errors.
    assert len(attempts) == 1


def test_run_with_retry_no_retry_for_value_error(monkeypatch):
    """Invalid input bubbles immediately — retry can't fix the input."""
    from axiom.extensions.builtins.chat.tool_errors import run_with_retry

    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.tool_errors.time.sleep",
        lambda *_: None,
    )

    attempts = []

    def bad():
        attempts.append(1)
        raise ValueError("schema mismatch")

    with pytest.raises(ValueError):
        run_with_retry(bad, max_attempts=5)
    assert len(attempts) == 1


def test_run_with_retry_uses_jittered_backoff():
    """Backoff sleep durations vary across attempts so concurrent agents don't
    sync-pulse upstream after a coordinated failure."""
    import requests

    from axiom.extensions.builtins.chat.tool_errors import run_with_retry

    sleeps: list[float] = []

    def fail():
        raise requests.exceptions.ConnectionError("nope")

    with patch(
        "axiom.extensions.builtins.chat.tool_errors.time.sleep",
        side_effect=lambda d: sleeps.append(d),
    ):
        with pytest.raises(requests.exceptions.ConnectionError):
            run_with_retry(fail, max_attempts=4)

    # 3 backoff sleeps (after attempts 1, 2, 3; final attempt has no sleep after).
    assert len(sleeps) == 3
    # Jitter: not all values identical.
    assert len(set(round(s, 4) for s in sleeps)) > 1, sleeps


def test_safe_run_returns_typed_error_dict_on_failure(monkeypatch):
    """Top-level entry: caller gets a typed error dict OR the success result.
    Never raises."""
    from axiom.extensions.builtins.chat.tool_errors import safe_run

    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.tool_errors.time.sleep",
        lambda *_: None,
    )

    def failing():
        raise PermissionError("denied")

    result = safe_run(failing, tool_name="email_send")
    assert "error" in result
    assert result["error_type"] == "permission_denied"
    assert result["retryable"] is False
    assert result["tool_name"] == "email_send"


def test_safe_run_returns_success_when_function_succeeds():
    from axiom.extensions.builtins.chat.tool_errors import safe_run

    def ok():
        return {"output": "hello"}

    result = safe_run(ok, tool_name="echo")
    assert result == {"output": "hello"}


def test_safe_run_records_attempt_count_on_persistent_failure(monkeypatch):
    """The structured error includes how many attempts were made — helpful
    context for the LLM deciding whether to try a different tool."""
    import requests

    from axiom.extensions.builtins.chat.tool_errors import safe_run

    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.tool_errors.time.sleep",
        lambda *_: None,
    )

    def fail():
        raise requests.exceptions.ConnectionError("down")

    result = safe_run(fail, tool_name="web_fetch", max_attempts=3)
    assert result["error_type"] == "transient"
    assert result["attempts"] == 3
