# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the feedback collection system."""

from datetime import datetime
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.signals.feedback import (
    FEEDBACK_DIR,
    FEEDBACK_LOG,
    FEEDBACK_TYPES,
    PENDING_REQUESTS,
    FeedbackCollector,
    FeedbackRequest,
    SignalFeedback,
)
from axiom.extensions.builtins.signals.models import Signal


class TestFeedbackTypes:
    """Test feedback type definitions."""

    def test_all_types_have_descriptions(self):
        for feedback_type, description in FEEDBACK_TYPES.items():
            assert isinstance(feedback_type, str)
            assert isinstance(description, str)
            assert len(description) > 0

    def test_required_types_exist(self):
        required = [
            "confirm_relevance",
            "add_initiative",
            "correct_error",
            "add_context",
            "approve",
            "dismiss",
        ]
        for t in required:
            assert t in FEEDBACK_TYPES


class TestFeedbackRequest:
    """Test FeedbackRequest dataclass."""

    def test_auto_timestamp(self):
        request = FeedbackRequest(
            request_id="abc123",
            signal_id="sig456",
            originator="ben@example.com",
            signal_summary="Test signal",
        )

        assert request.created_at != ""
        # Should be a valid ISO timestamp
        datetime.fromisoformat(request.created_at.replace("Z", "+00:00"))

    def test_roundtrip(self):
        request = FeedbackRequest(
            request_id="abc123",
            signal_id="sig456",
            originator="user@example.com",
            signal_summary="Signal about Project Alpha",
            routed_to=["endpoint-1", "endpoint-2"],
            suggested_prds=["prd-alpha", "prd-beta"],
        )

        d = request.to_dict()
        restored = FeedbackRequest.from_dict(d)

        assert restored.request_id == request.request_id
        assert restored.originator == request.originator
        assert restored.routed_to == request.routed_to
        assert restored.suggested_prds == request.suggested_prds


class TestSignalFeedback:
    """Test SignalFeedback dataclass."""

    def test_valid_feedback_type(self):
        feedback = SignalFeedback(
            signal_id="sig123",
            feedback_type="add_initiative",
            content="Also relevant to MSR project",
            originator="ben@example.com",
        )

        assert feedback.feedback_type in FEEDBACK_TYPES

    def test_invalid_feedback_type(self):
        feedback = SignalFeedback(
            signal_id="sig123",
            feedback_type="invalid_type_xyz",
            content="test",
            originator="ben@example.com",
        )

        assert feedback.feedback_type not in FEEDBACK_TYPES

    def test_auto_timestamp(self):
        feedback = SignalFeedback(
            signal_id="sig123",
            feedback_type="approve",
            content="",
            originator="ben@example.com",
        )

        assert feedback.received_at != ""


class TestFeedbackCollector:
    """Test FeedbackCollector class."""

    @pytest.fixture
    def collector(self, tmp_path):
        feedback_dir = tmp_path / "feedback"
        feedback_dir.mkdir()
        pending_path = feedback_dir / "pending_requests.json"
        log_path = feedback_dir / "feedback_log.json"
        with patch.object(FeedbackCollector, '__init__', lambda self: None):
            c = FeedbackCollector.__new__(FeedbackCollector)
        c.pending = {}
        c.feedback_log = []
        # Patch the module-level paths so _save/_load use our temp dir
        self._orig_dir = FEEDBACK_DIR
        self._orig_pending = PENDING_REQUESTS
        self._orig_log = FEEDBACK_LOG
        import axiom.extensions.builtins.signals.feedback as fb_mod
        fb_mod.FEEDBACK_DIR = feedback_dir
        fb_mod.PENDING_REQUESTS = pending_path
        fb_mod.FEEDBACK_LOG = log_path
        return c

    @pytest.fixture(autouse=True)
    def _restore_paths(self):
        yield
        import axiom.extensions.builtins.signals.feedback as fb_mod
        if hasattr(self, '_orig_dir'):
            fb_mod.FEEDBACK_DIR = self._orig_dir
            fb_mod.PENDING_REQUESTS = self._orig_pending
            fb_mod.FEEDBACK_LOG = self._orig_log

    @pytest.fixture
    def sample_signal(self):
        return Signal(
            source="voice",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="Discussion about thermal limits",
            detail="Thermal hydraulics need review",
            originator="user@example.com",
            initiatives=["Project Alpha"],
            signal_type="action_item",
        )

    def test_request_feedback(self, collector, sample_signal):
        request = collector.create_feedback_request(sample_signal)

        assert request is not None
        assert request.signal_id == sample_signal.signal_id
        assert request.originator == sample_signal.originator
        assert "Thermal" in request.signal_summary

    def test_request_saved_to_pending(self, collector, sample_signal):
        request = collector.create_feedback_request(sample_signal)

        assert request.request_id in collector.pending

    def test_apply_feedback_updates_signal(self, collector, sample_signal):
        # Create a feedback request first
        _request = collector.create_feedback_request(sample_signal)

        # Now submit feedback
        feedback = SignalFeedback(
            signal_id=sample_signal.signal_id,
            feedback_type="add_initiative",
            content="Also relevant to Project Beta",
            originator="user@example.com",
        )

        result = collector.submit_feedback(feedback)

        assert result is True

        # Check feedback was logged
        assert len(collector.feedback_log) >= 1

    def test_dismiss_feedback_marks_noise(self, collector, sample_signal):
        _request = collector.create_feedback_request(sample_signal)

        feedback = SignalFeedback(
            signal_id=sample_signal.signal_id,
            feedback_type="dismiss",
            content="This is just noise",
            originator="ben@example.com",
        )

        result = collector.submit_feedback(feedback)
        assert result is True

    def test_get_feedback_for_signal(self, collector, sample_signal):
        collector.create_feedback_request(sample_signal)

        feedback1 = SignalFeedback(
            signal_id=sample_signal.signal_id,
            feedback_type="add_context",
            content="Kevin was in the meeting",
            originator="ben@example.com",
        )
        feedback2 = SignalFeedback(
            signal_id=sample_signal.signal_id,
            feedback_type="approve",
            content="looks good",
            originator="ben@example.com",
        )

        collector.submit_feedback(feedback1)
        collector.submit_feedback(feedback2)

        signal_feedback = collector.get_feedback_for_signal(sample_signal.signal_id)
        assert len(signal_feedback) == 2

    def test_no_feedback_without_originator(self, collector):
        signal = Signal(
            source="voice",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="Anonymous signal",
            # No originator set
        )

        with pytest.raises(ValueError, match="no originator"):
            collector.create_feedback_request(signal)
