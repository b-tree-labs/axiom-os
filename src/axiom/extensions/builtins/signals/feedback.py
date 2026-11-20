# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signal Feedback Loop — notifies originators and collects their input.

When a signal is processed, we can:
1. Notify the originator with a "receipt" of what we extracted
2. Invite them to review and provide additional color
3. Apply their feedback to improve routing and accuracy

Feedback types:
- confirm_relevance: "Yes, this is relevant to X initiative"
- add_initiative: "Also relevant to Y workstream"
- suggest_person: "Z would be interested in this"
- correct_error: "That's not what I meant, here's clarification"
- add_context: "I forgot to mention X was blocking"
- set_priority: "This is urgent, need decision by Friday"
- set_confidential: "Don't route externally yet"
- link_signal: "This relates to what I said last week"
- assign_owner: "Kevin should own this, not me"
- request_approval: "Check with me before sending to sponsor"

Usage:
    from axiom.extensions.builtins.signals.feedback import FeedbackCollector, SignalFeedback

    collector = FeedbackCollector()
    collector.request_feedback(signal)  # Sends notification to originator

    # When originator responds:
    feedback = SignalFeedback(
        signal_id="abc123",
        feedback_type="add_initiative",
        content="Also relevant to Project Alpha",
        originator="user@example.com",
    )
    collector.apply_feedback(feedback)
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.state import atomic_write

from .models import Signal

_RUNTIME_DIR = _REPO_ROOT / "runtime"
FEEDBACK_DIR = _RUNTIME_DIR / "inbox" / "feedback"
FEEDBACK_LOG = FEEDBACK_DIR / "feedback_log.json"
PENDING_REQUESTS = FEEDBACK_DIR / "pending_requests.json"


# Valid feedback types with descriptions
FEEDBACK_TYPES = {
    "confirm_relevance": "Confirms signal is relevant to stated initiative(s)",
    "add_initiative": "Adds relevance to additional workstream/initiative",
    "suggest_person": "Suggests another person who would be interested",
    "correct_error": "Corrects an error in the extracted signal",
    "add_context": "Adds missing context or clarification",
    "set_priority": "Sets urgency/priority level",
    "set_confidential": "Marks signal as confidential (restricted routing)",
    "link_signal": "Links to a related previous signal",
    "assign_owner": "Suggests a different owner for action items",
    "request_approval": "Requests approval before routing to certain endpoints",
    "approve": "Approves the signal processing as-is",
    "dismiss": "Signal is noise, should not be processed further",
}


@dataclass
class FeedbackRequest:
    """A pending request for feedback from a signal originator."""

    request_id: str  # Unique token for the feedback URL
    signal_id: str
    originator: str  # Email or identifier
    signal_summary: str  # What we extracted (for their review)
    routed_to: list[str] = field(default_factory=list)  # Where we're sending it
    suggested_prds: list[str] = field(default_factory=list)  # LLM suggestions

    created_at: str = ""
    expires_at: str = ""  # Feedback window (e.g., 48 hours)
    notified: bool = False
    notification_method: str = ""  # email, slack, web

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "signal_id": self.signal_id,
            "originator": self.originator,
            "signal_summary": self.signal_summary,
            "routed_to": self.routed_to,
            "suggested_prds": self.suggested_prds,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "notified": self.notified,
            "notification_method": self.notification_method,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FeedbackRequest:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SignalFeedback:
    """Feedback received from a signal originator."""

    signal_id: str
    feedback_type: str  # One of FEEDBACK_TYPES
    content: str  # The actual feedback
    originator: str  # Who provided it

    # Optional structured fields for specific feedback types
    initiative: str = ""  # For add_initiative
    person: str = ""  # For suggest_person
    priority: str = ""  # For set_priority: low, medium, high, urgent
    linked_signal_id: str = ""  # For link_signal

    received_at: str = ""
    applied: bool = False
    applied_at: str = ""

    def __post_init__(self):
        if not self.received_at:
            self.received_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "feedback_type": self.feedback_type,
            "content": self.content,
            "originator": self.originator,
            "initiative": self.initiative,
            "person": self.person,
            "priority": self.priority,
            "linked_signal_id": self.linked_signal_id,
            "received_at": self.received_at,
            "applied": self.applied,
            "applied_at": self.applied_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SignalFeedback:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class FeedbackCollector:
    """Manages the signal feedback loop."""

    def __init__(self):
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        self.pending: dict[str, FeedbackRequest] = {}  # request_id -> request
        self.feedback_log: list[SignalFeedback] = []
        self._load()

    def _load(self) -> None:
        """Load pending requests and feedback log from disk."""
        if PENDING_REQUESTS.exists():
            try:
                data = json.loads(PENDING_REQUESTS.read_text())
                for item in data:
                    req = FeedbackRequest.from_dict(item)
                    self.pending[req.request_id] = req
            except (json.JSONDecodeError, OSError):
                pass

        if FEEDBACK_LOG.exists():
            try:
                data = json.loads(FEEDBACK_LOG.read_text())
                self.feedback_log = [SignalFeedback.from_dict(f) for f in data]
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self) -> None:
        """Persist state to disk."""
        atomic_write(
            PENDING_REQUESTS,
            json.dumps([r.to_dict() for r in self.pending.values()], indent=2),
        )
        atomic_write(
            FEEDBACK_LOG,
            json.dumps([f.to_dict() for f in self.feedback_log], indent=2),
        )

    def create_feedback_request(
        self,
        signal: Signal,
        routed_to: list[str] | None = None,
        suggested_prds: list[str] | None = None,
    ) -> FeedbackRequest:
        """Create a feedback request for a signal's originator.

        Returns the request with a unique token for the feedback URL.
        """
        if not signal.originator:
            raise ValueError("Signal has no originator — cannot request feedback")

        request_id = secrets.token_urlsafe(16)

        # Build a human-readable summary of what we extracted
        summary_parts = [
            f"Type: {signal.signal_type}",
            f"Summary: {signal.detail or signal.raw_text[:200]}",
        ]
        if signal.people:
            summary_parts.append(f"People mentioned: {', '.join(signal.people)}")
        if signal.initiatives:
            summary_parts.append(f"Initiatives: {', '.join(signal.initiatives)}")

        request = FeedbackRequest(
            request_id=request_id,
            signal_id=signal.signal_id,
            originator=signal.originator,
            signal_summary="\n".join(summary_parts),
            routed_to=routed_to or [],
            suggested_prds=suggested_prds or [],
        )

        self.pending[request_id] = request
        self._save()
        return request

    def get_request(self, request_id: str) -> FeedbackRequest | None:
        """Get a pending feedback request by its token."""
        return self.pending.get(request_id)

    def get_request_by_signal(self, signal_id: str) -> FeedbackRequest | None:
        """Get pending request for a specific signal."""
        for req in self.pending.values():
            if req.signal_id == signal_id:
                return req
        return None

    def submit_feedback(self, feedback: SignalFeedback) -> bool:
        """Record feedback from an originator.

        Returns True if feedback was accepted.
        """
        if feedback.feedback_type not in FEEDBACK_TYPES:
            return False

        self.feedback_log.append(feedback)

        # Remove from pending if this completes the request
        for req_id, req in list(self.pending.items()):
            if req.signal_id == feedback.signal_id:
                del self.pending[req_id]
                break

        self._save()
        return True

    def get_feedback_for_signal(self, signal_id: str) -> list[SignalFeedback]:
        """Get all feedback received for a signal."""
        return [f for f in self.feedback_log if f.signal_id == signal_id]

    def get_unapplied_feedback(self) -> list[SignalFeedback]:
        """Get feedback that hasn't been applied yet."""
        return [f for f in self.feedback_log if not f.applied]

    def mark_applied(self, signal_id: str, feedback_type: str) -> bool:
        """Mark feedback as applied."""
        for f in self.feedback_log:
            if f.signal_id == signal_id and f.feedback_type == feedback_type:
                f.applied = True
                f.applied_at = datetime.now(UTC).isoformat()
                self._save()
                return True
        return False

    def generate_feedback_url(self, request: FeedbackRequest, base_url: str = "") -> str:
        """Generate the URL for the originator to provide feedback."""
        if not base_url:
            base_url = "http://localhost:8765"
        return f"{base_url}/feedback/{request.request_id}"

    def generate_receipt_message(self, request: FeedbackRequest) -> str:
        """Generate a human-readable receipt message for the originator."""
        lines = [
            "## Signal Receipt",
            "",
            "We captured a signal from you and extracted the following:",
            "",
            "```",
            request.signal_summary,
            "```",
            "",
        ]

        if request.routed_to:
            lines.extend([
                "**Routing to:**",
                *[f"- {r}" for r in request.routed_to],
                "",
            ])

        if request.suggested_prds:
            lines.extend([
                "**May also be relevant to:**",
                *[f"- {p}" for p in request.suggested_prds],
                "",
            ])

        lines.extend([
            "---",
            "",
            "**Your input welcome!** You can:",
            "- ✓ Confirm this is accurate",
            "- ✏️ Correct any errors",
            "- ➕ Add missing context",
            "- 👤 Suggest others who should see this",
            "- 🔗 Link to related work",
            "",
            f"Review and respond: [Provide Feedback]({self.generate_feedback_url(request)})",
        ])

        return "\n".join(lines)

    def status(self) -> dict:
        """Get feedback system status."""
        return {
            "pending_requests": len(self.pending),
            "total_feedback": len(self.feedback_log),
            "unapplied_feedback": len(self.get_unapplied_feedback()),
            "by_type": self._count_by_type(),
        }

    def _count_by_type(self) -> dict[str, int]:
        """Count feedback by type."""
        counts: dict[str, int] = {}
        for f in self.feedback_log:
            counts[f.feedback_type] = counts.get(f.feedback_type, 0) + 1
        return counts


def get_feedback_collector() -> FeedbackCollector:
    """Get singleton feedback collector instance."""
    return FeedbackCollector()
