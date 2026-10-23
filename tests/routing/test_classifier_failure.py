# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Improvement Item 1 — typed classifier-failure result.

Distinguish "Ollama unreachable" from "model not loaded" (and other failure
modes) in :class:`OllamaClassifier`. Today ``classify()`` swallows every
exception and returns ``None``, so the router cannot tell a missing-model
from a benign uncertain-in-balanced result. That ambiguity caused the
over-blocking incident referenced in the dependency-management lessons:
the operator configured a model name but never pulled it; ``classify()``
returned ``None`` forever; the audit log gave no signal.

This module verifies:
    * :class:`OllamaClassifier.classify` returns a typed
      :class:`ClassifierFailure` for each distinct failure mode.
    * :class:`QueryRouter.classify` propagates the failure through the
      :class:`RoutingDecision.classifier_failure` field and records it
      via :class:`AuditLog`.
    * Successful paths and the pre-check ``None`` path remain unchanged.
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from axiom.infra.router import (
    SENSITIVITY_BALANCED,
    SENSITIVITY_STRICT,
    ClassifierFailure,
    OllamaClassifier,
    QueryRouter,
    RoutingTier,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_classifier(*, available: bool = True) -> OllamaClassifier:
    """Construct an OllamaClassifier with the availability pre-check stubbed.

    Bypasses the network round-trip in ``_check_available`` so each test can
    inject the desired urlopen failure on the *generate* call only.
    """
    clf = OllamaClassifier(
        base_url="http://localhost:11434",
        model="llama3.2:1b",
        timeout=0.1,
    )
    clf._available = available  # type: ignore[attr-defined]
    return clf


def _resp(payload: dict) -> MagicMock:
    """Build a context-manager-shaped urlopen response yielding ``payload``."""
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# OllamaClassifier — distinct failure modes
# ---------------------------------------------------------------------------


class TestOllamaClassifierFailureModes:
    def test_returns_failure_when_ollama_unreachable(self):
        """ConnectionRefusedError-equivalent → reason=ollama_unreachable."""
        clf = _make_classifier()
        # urllib raises URLError(ConnectionRefusedError) when port is closed.
        err = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        with patch("urllib.request.urlopen", side_effect=err):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "ollama_unreachable"
        assert result.endpoint == "http://localhost:11434"
        assert result.model == "llama3.2:1b"
        assert "refused" in result.detail.lower() or "unreachable" in result.detail.lower()

    def test_returns_failure_on_url_error_unreachable_host(self):
        """Generic URLError with non-timeout reason → ollama_unreachable."""
        clf = _make_classifier()
        err = urllib.error.URLError("nodename nor servname provided")
        with patch("urllib.request.urlopen", side_effect=err):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "ollama_unreachable"

    def test_returns_failure_when_model_not_loaded_via_404(self):
        """HTTP 404 from /api/generate → reason=model_not_loaded, model populated."""
        clf = _make_classifier()
        body = b'{"error":"model \\"llama3.2:1b\\" not found"}'
        http_err = urllib.error.HTTPError(
            url="http://localhost:11434/api/generate",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(body),
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "model_not_loaded"
        assert result.model == "llama3.2:1b"

    def test_returns_failure_when_model_not_loaded_via_response_body(self):
        """HTTPError with body containing 'model not found' → model_not_loaded.

        Some Ollama versions return a non-404 status but include the
        diagnostic in the body. Detect that path too.
        """
        clf = _make_classifier()
        body = b'{"error":"model \'llama3.2:1b\' not found, try pulling it"}'
        http_err = urllib.error.HTTPError(
            url="http://localhost:11434/api/generate",
            code=500,
            msg="Internal Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(body),
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "model_not_loaded"
        assert result.model == "llama3.2:1b"

    def test_returns_failure_on_socket_timeout(self):
        """Bare socket.timeout → reason=timeout."""
        clf = _make_classifier()
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "timeout"
        assert result.endpoint == "http://localhost:11434"

    def test_returns_failure_on_url_error_wrapping_timeout(self):
        """URLError wrapping a TimeoutError → reason=timeout."""
        clf = _make_classifier()
        err = urllib.error.URLError(TimeoutError("read timed out"))
        with patch("urllib.request.urlopen", side_effect=err):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "timeout"

    def test_returns_failure_on_unparseable_response(self):
        """Body decodes but contains no yes/no/uncertain → unexpected_response."""
        clf = _make_classifier()
        with patch(
            "urllib.request.urlopen",
            return_value=_resp({"response": "I cannot answer that"}),
        ):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "unexpected_response"
        assert result.model == "llama3.2:1b"

    def test_returns_failure_on_invalid_json_response(self):
        """Garbage body that fails json.loads → unexpected_response."""
        clf = _make_classifier()
        bad_resp = MagicMock()
        bad_resp.read.return_value = b"not json at all <<<"
        bad_resp.__enter__ = MagicMock(return_value=bad_resp)
        bad_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=bad_resp):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "unexpected_response"

    def test_returns_failure_on_generic_exception(self):
        """Any other exception → reason=transport_error, detail captured."""
        clf = _make_classifier()
        with patch("urllib.request.urlopen", side_effect=RuntimeError("kaboom")):
            result = clf.classify("hello world")
        assert isinstance(result, ClassifierFailure)
        assert result.reason == "transport_error"
        assert "kaboom" in result.detail


# ---------------------------------------------------------------------------
# OllamaClassifier — preserved behaviors
# ---------------------------------------------------------------------------


class TestOllamaClassifierPreservedBehavior:
    def test_returns_none_when_check_available_returns_false(self):
        """Pre-check failure stays as ``None`` (back-compat for callers that
        treated None as 'no signal yet')."""
        clf = OllamaClassifier(base_url="http://localhost:11434", model="x")
        clf._available = False  # type: ignore[attr-defined]
        # urlopen must NOT be called when pre-check says unavailable.
        with patch("urllib.request.urlopen") as urlopen:
            result = clf.classify("anything")
            urlopen.assert_not_called()
        assert result is None

    def test_returns_export_controlled_on_yes(self):
        clf = _make_classifier()
        with patch(
            "urllib.request.urlopen",
            return_value=_resp({"response": "yes"}),
        ):
            assert clf.classify("nuclear weapons design details") == RoutingTier.EXPORT_CONTROLLED

    def test_returns_public_on_no(self):
        clf = _make_classifier()
        with patch(
            "urllib.request.urlopen",
            return_value=_resp({"response": "no"}),
        ):
            assert clf.classify("what's the weather") == RoutingTier.PUBLIC

    def test_returns_uncertain_string(self):
        clf = _make_classifier()
        with patch(
            "urllib.request.urlopen",
            return_value=_resp({"response": "uncertain"}),
        ):
            assert clf.classify("ambiguous query") == "uncertain"


# ---------------------------------------------------------------------------
# ClassifierFailure dataclass
# ---------------------------------------------------------------------------


class TestClassifierFailureDataclass:
    def test_is_frozen(self):
        f = ClassifierFailure(
            reason="ollama_unreachable",
            detail="connection refused",
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
        )
        with pytest.raises(Exception):  # noqa: B017  # FrozenInstanceError subclasses AttributeError
            f.reason = "timeout"  # type: ignore[misc]

    def test_model_is_optional(self):
        f = ClassifierFailure(
            reason="ollama_unreachable",
            detail="x",
            endpoint="http://localhost:11434",
        )
        assert f.model is None


# ---------------------------------------------------------------------------
# QueryRouter integration — propagation + audit
# ---------------------------------------------------------------------------


class TestQueryRouterFailurePropagation:
    def test_failure_populates_routing_decision_field(self):
        """When OllamaClassifier returns ClassifierFailure, the resulting
        RoutingDecision exposes it on .classifier_failure for downstream UX."""
        failure = ClassifierFailure(
            reason="model_not_loaded",
            detail="model 'llama3.2:1b' not found",
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
        )
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = failure
        router = QueryRouter(ollama=mock_ollama)

        decision = router.classify(
            "benign question with no keywords",
            sensitivity=SENSITIVITY_BALANCED,
        )

        assert decision.classifier_failure is failure
        # Behavior preserved: balanced + no keyword + classifier failure → public fallback.
        assert decision.tier == RoutingTier.PUBLIC
        assert decision.classifier == "fallback"

    def test_failure_in_strict_still_falls_through_to_ec_fallback(self):
        """Strict mode still routes EC on classifier failure (behavior preserved),
        and the failure is surfaced for diagnostics."""
        failure = ClassifierFailure(
            reason="ollama_unreachable",
            detail="connection refused",
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
        )
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = failure
        router = QueryRouter(ollama=mock_ollama)

        decision = router.classify(
            "benign question",
            sensitivity=SENSITIVITY_STRICT,
        )

        assert decision.classifier_failure is failure
        assert decision.tier == RoutingTier.EXPORT_CONTROLLED
        assert decision.classifier == "fallback"

    def test_routing_decision_classifier_failure_defaults_to_none(self):
        """Successful classifier paths leave classifier_failure unset."""
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = RoutingTier.PUBLIC
        router = QueryRouter(ollama=mock_ollama)

        decision = router.classify(
            "benign question",
            sensitivity=SENSITIVITY_BALANCED,
        )
        assert decision.classifier_failure is None

    def test_audit_log_records_failure_details(self):
        """write_classifier_failure is invoked with reason/detail/endpoint/model."""
        failure = ClassifierFailure(
            reason="model_not_loaded",
            detail="model 'llama3.2:1b' not found",
            endpoint="http://localhost:11434",
            model="llama3.2:1b",
        )
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = failure
        router = QueryRouter(ollama=mock_ollama)

        mock_audit = MagicMock()
        with patch("axiom.infra.audit_log.AuditLog.get", return_value=mock_audit):
            router.classify(
                "benign question",
                sensitivity=SENSITIVITY_BALANCED,
            )

        # The classifier-failure write must be invoked exactly once with
        # the typed fields. write_classification is also invoked for the
        # final routing decision; we don't assert on that here.
        mock_audit.write_classifier_failure.assert_called_once()
        kwargs = mock_audit.write_classifier_failure.call_args.kwargs
        assert kwargs["reason"] == "model_not_loaded"
        assert kwargs["detail"] == "model 'llama3.2:1b' not found"
        assert kwargs["endpoint"] == "http://localhost:11434"
        assert kwargs["model"] == "llama3.2:1b"

    def test_no_failure_audit_when_classifier_succeeds(self):
        """write_classifier_failure must NOT fire on successful classification."""
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = RoutingTier.PUBLIC
        router = QueryRouter(ollama=mock_ollama)

        mock_audit = MagicMock()
        with patch("axiom.infra.audit_log.AuditLog.get", return_value=mock_audit):
            router.classify("benign", sensitivity=SENSITIVITY_BALANCED)

        mock_audit.write_classifier_failure.assert_not_called()

    def test_no_failure_audit_when_classifier_returns_none_unavailable(self):
        """The legacy ``None`` (pre-check unavailable) path is NOT a typed
        failure — no write_classifier_failure event is emitted. This preserves
        the silent-on-cold-start property."""
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = None
        router = QueryRouter(ollama=mock_ollama)

        mock_audit = MagicMock()
        with patch("axiom.infra.audit_log.AuditLog.get", return_value=mock_audit):
            router.classify("benign", sensitivity=SENSITIVITY_BALANCED)

        mock_audit.write_classifier_failure.assert_not_called()
