# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RACI escalation state machine for steward agents.

Per `feedback_raci_automation_escalation`: any agent observing work it
could automate must default to the conservative lane — propose, ask
permission to schedule, then either run on schedule (yes) or back off
exponentially (no, no, no → off). Pre-approval at install time skips
the propose-and-ask phase.

Action classes are the dedupe key. "ingest local-repo updates" is one
class; "diagnose CI failures" is another; refusing one does not affect
the other.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

DEFAULT_BACKOFF_SCHEDULE_SECONDS: tuple[float, ...] = (
    86400.0,        # 1 day after the 1st no
    86400.0 * 3,    # 3 days after the 2nd no
)
DEFAULT_MAX_REJECTIONS: int = 3


class RACIState(str, Enum):
    UNKNOWN = "unknown"
    APPROVED = "approved"
    BACKING_OFF = "backing_off"
    DISABLED = "disabled"
    PRE_APPROVED = "pre_approved"


class ProposalDecision(str, Enum):
    AUTO = "auto"   # already approved — proceed silently
    ASK = "ask"     # surface the proposal to the user now
    SKIP = "skip"   # in cooldown or disabled — do not act, do not ask


@dataclass
class ActionApproval:
    state: RACIState = RACIState.UNKNOWN
    no_count: int = 0
    next_ask_at: float = 0.0
    approved_at: float = 0.0


@dataclass
class RACILedger:
    """Per-agent persistent ledger of automation approvals.

    Keyed by `action_class` — a stable string the agent uses to identify
    the kind of work it wants to schedule. Persistence is a flat JSON
    object; snapshot to disk after every state change.
    """

    actions: dict[str, ActionApproval] = field(default_factory=dict)
    backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE_SECONDS
    max_rejections: int = DEFAULT_MAX_REJECTIONS
    now: Callable[[], float] = field(default=time.time)

    def _entry(self, action_class: str) -> ActionApproval:
        if action_class not in self.actions:
            self.actions[action_class] = ActionApproval()
        return self.actions[action_class]

    def propose(self, action_class: str) -> ProposalDecision:
        entry = self._entry(action_class)
        if entry.state in (RACIState.APPROVED, RACIState.PRE_APPROVED):
            return ProposalDecision.AUTO
        if entry.state is RACIState.DISABLED:
            return ProposalDecision.SKIP
        if entry.state is RACIState.BACKING_OFF:
            if self.now() >= entry.next_ask_at:
                return ProposalDecision.ASK
            return ProposalDecision.SKIP
        return ProposalDecision.ASK

    def record_yes(self, action_class: str) -> None:
        entry = self._entry(action_class)
        entry.state = RACIState.APPROVED
        entry.approved_at = self.now()

    def record_no(self, action_class: str) -> None:
        entry = self._entry(action_class)
        entry.no_count += 1
        if entry.no_count >= self.max_rejections:
            entry.state = RACIState.DISABLED
            entry.next_ask_at = 0.0
            return
        # no_count is now 1 or 2; pick cooldown[no_count-1]
        idx = min(entry.no_count - 1, len(self.backoff_schedule) - 1)
        entry.state = RACIState.BACKING_OFF
        entry.next_ask_at = self.now() + self.backoff_schedule[idx]

    def pre_approve(self, action_class: str) -> None:
        entry = self._entry(action_class)
        entry.state = RACIState.PRE_APPROVED
        entry.approved_at = self.now()

    def disable(self, action_class: str) -> None:
        entry = self._entry(action_class)
        entry.state = RACIState.DISABLED

    def is_disabled(self, action_class: str) -> bool:
        return self._entry(action_class).state is RACIState.DISABLED

    def next_ask_at(self, action_class: str) -> float:
        return self._entry(action_class).next_ask_at

    def to_dict(self) -> dict:
        return {
            "actions": {k: asdict(v) for k, v in self.actions.items()},
            "backoff_schedule": list(self.backoff_schedule),
            "max_rejections": self.max_rejections,
        }

    @classmethod
    def from_dict(cls, data: dict, *, now: Callable[[], float] = time.time) -> RACILedger:
        actions = {}
        for k, v in data.get("actions", {}).items():
            v = dict(v)
            v["state"] = RACIState(v["state"])
            actions[k] = ActionApproval(**v)
        return cls(
            actions=actions,
            backoff_schedule=tuple(data.get("backoff_schedule", DEFAULT_BACKOFF_SCHEDULE_SECONDS)),
            max_rejections=data.get("max_rejections", DEFAULT_MAX_REJECTIONS),
            now=now,
        )

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: Path | str, *, now: Callable[[], float] = time.time) -> RACILedger:
        path = Path(path)
        if not path.exists():
            return cls(now=now)
        return cls.from_dict(json.loads(path.read_text()), now=now)
