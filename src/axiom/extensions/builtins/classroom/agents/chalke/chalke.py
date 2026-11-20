# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CHALKE — AI Training Assistant (ATA) v0 for the classroom.

Dual-role always-on agent. See SKILLS.md in this directory for the
charter, perspective split, and coordination protocols.

v0 scope (what this module implements):
- Chalke class with composition + per-classroom wiring
- for_instructor() / for_student() perspective views
- Instructor daily brief compiled from signal fragments
- Student tutoring response with RPE + memory integration
- Per-student profile reads/writes via composition
- Seam points for future agent routing (AXI, CURIO, SCAN)

Out of scope for v0 (documented in SKILLS.md):
- Actual LLM calls routed through agent-specific LLMs
- Full skill library beyond the 2-3 core methods
- Cross-agent coordination protocol implementation (stubbed)
- Federation-aware features (architecture ready; wiring later)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment

    from ...tracing import ClassroomTracer


# Type alias — LLM backend for CHALKE to route prose generation
ChalkeLLM = Callable[..., str]


# ---------------------------------------------------------------------------
# InstructorView
# ---------------------------------------------------------------------------


@dataclass
class InstructorView:
    """CHALKE's instructor-facing perspective bound to a classroom."""

    chalke: Chalke

    def daily_brief(
        self,
        instructor_id: str,
        at: str | None = None,
    ) -> dict:
        """Compile the 3-5 most important things for the instructor.

        Reads signal + ticket + metric fragments through the composition
        stack (bipartite access respected). Never returns content the
        instructor can't see.
        """
        comp = self.chalke.composition

        # Find all signal fragments + ticket fragments in the registry.
        # In v0 we list-and-filter; future version uses indexes.
        signals = []
        tickets = []
        for art in comp.artifact_registry.list(kind="fragment"):
            data = art.data
            content = data.get("content", {})
            if content.get("fact_kind") == "course_resource":
                continue
            if "signal_type" in content:
                signals.append((art, data, content))
            elif "ticket_id" in content:
                tickets.append((art, data, content))

        # Categorize signals
        stuck = [c for (_, _, c) in signals if c.get("signal_type") == "student_stuck"]
        misconceptions = [
            c for (_, _, c) in signals
            if c.get("signal_type") == "misconception_detected"
        ]
        low_eng = [
            c for (_, _, c) in signals
            if c.get("signal_type") == "low_engagement"
        ]
        objective_gaps = [
            c for (_, _, c) in signals
            if c.get("signal_type") == "objective_gap"
        ]

        open_tickets = [
            c for (_, _, c) in tickets
            if c.get("status") in ("open", "in_progress")
        ]

        return {
            "classroom_id": self.chalke.classroom_id,
            "compiled_at": at or datetime.now(UTC).isoformat(),
            "stuck_students": stuck,
            "misconceptions": misconceptions,
            "low_engagement": low_eng,
            "objective_gaps": objective_gaps,
            "open_help_tickets": len(open_tickets),
            "top_priority": self._top_priority(stuck, misconceptions, open_tickets),
        }

    def _top_priority(
        self, stuck: list, misconceptions: list, open_tickets: list,
    ) -> dict | None:
        """Return the single highest-priority item for the brief headline."""
        if misconceptions:
            return {"kind": "misconception",
                    "detail": misconceptions[0]}
        if stuck:
            return {"kind": "stuck_student", "detail": stuck[0]}
        if open_tickets:
            return {"kind": "open_ticket", "count": len(open_tickets)}
        return None


# ---------------------------------------------------------------------------
# StudentView
# ---------------------------------------------------------------------------


@dataclass
class StudentView:
    """CHALKE's student-facing perspective bound to one student."""

    chalke: Chalke
    student_id: str

    def explain(
        self,
        topic: str,
        level: str = "Frameworks",
    ) -> dict:
        """Produce a teaching-intent retrieval plan + LLM response.

        v0: returns the plan + a stub LLM response. Full LLM routing
        comes when agent infrastructure is wired.
        """
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal=self.student_id,
            intent_id="teaching",
            constraints={"maturity_floor": level},
        )
        # Layer 1 — CHALKE persona prepended to identity. Best-effort.
        _persona_text = ""
        try:
            from pathlib import Path as _Path

            from axiom.agents.persona_loader import load_agent_persona

            _persona_dir = _Path(__file__).parent
            _persona_text = load_agent_persona(_persona_dir) or ""
        except Exception:
            pass

        _system_content = "You are CHALKE, a patient tutor."
        if _persona_text:
            _system_content = _persona_text + "\n\n" + _system_content
        # Student overlay — pin the system prompt to this student's id.
        _system_content += f"\n\nYou are addressing student `{self.student_id}` for this turn."

        response = self.chalke.llm_backend(
            [
                {"role": "system", "content": _system_content},
                {"role": "user", "content": f"Explain {topic} at {level} level."},
            ]
        )
        return {
            "student_id": self.student_id,
            "topic": topic,
            "level": level,
            "plan": {
                "strategy": plan.strategy,
                "params": plan.params,
                "source_count": len(plan.sources),
            },
            "response": response,
        }

    def metacognitive_review(self) -> dict:
        """Review the student's own trace history — "how am I doing?" """
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal=self.student_id,
            intent_id="metacognitive",
            constraints={},
        )
        return {
            "student_id": self.student_id,
            "plan_strategy": plan.strategy,
            "window_days": plan.params.get("time_window_days", 30),
            "message": (
                "I'll review your recent sessions and highlight patterns. "
                "Your strongest topics and next focus areas will appear here."
            ),
        }

    def profile(self) -> MemoryFragment | None:
        """Return the student's CHALKE-maintained profile fragment, or None.

        Profile is a CognitiveType.CORE fragment — always-loaded, never
        expired. Student is master; CHALKE has EFFORT delegation.
        Returns the most-recent profile if multiple writes have occurred.
        """
        from axiom.memory.fragment import fragment_from_dict

        comp = self.chalke.composition
        candidates = []
        for art in comp.artifact_registry.list(kind="fragment"):
            data = art.data
            content = data.get("content", {})
            if (
                content.get("fact_kind") == "student_profile"
                and content.get("student_id") == self.student_id
            ):
                candidates.append((art.created_at, data))

        if not candidates:
            return None
        # Most recent wins
        candidates.sort(key=lambda pair: pair[0])
        return fragment_from_dict(candidates[-1][1])

    def update_profile(self, updates: dict) -> MemoryFragment:
        """Upsert the student's profile fragment with new values."""
        from axiom.memory.ownership import Right, delegate, new_ownership

        # Ownership: student master, CHALKE EFFORT delegate
        own = new_ownership(master=self.student_id)
        own = delegate(
            own,
            delegate_principal="chalke",
            rights={Right.EFFORT},
            expires_at="2099-12-31T23:59:59Z",
        )

        # Merge with existing if present
        existing = self.profile()
        merged = dict(existing.content) if existing else {}
        merged.update(updates)
        merged["student_id"] = self.student_id
        merged["fact_kind"] = "student_profile"

        return self.chalke.composition.write(
            content=merged,
            cognitive_type="core",
            principal_id=self.student_id,
            agents={"chalke"},
            resources={f"classroom:{self.chalke.classroom_id}",
                       f"student:{self.student_id}"},
            ownership=own,
        )


# ---------------------------------------------------------------------------
# Chalke
# ---------------------------------------------------------------------------


@dataclass
class Chalke:
    """AI Training Assistant — the classroom's TA agent.

    Coordinates other agents and tools. Serves instructor + student
    perspectives through the scoped views. All memory operations flow
    through the composition stack.
    """

    classroom_id: str
    composition: CompositionService
    tracer: ClassroomTracer | None = None
    llm_backend: ChalkeLLM = field(default=lambda msgs, **kw: "")

    def for_instructor(self) -> InstructorView:
        """Return the instructor-facing view of CHALKE."""
        return InstructorView(chalke=self)

    def for_student(self, student_id: str) -> StudentView:
        """Return a view scoped to a single student."""
        return StudentView(chalke=self, student_id=student_id)

    def name(self) -> str:
        """Friendly name for UI surfaces."""
        return "CHALKE"

    def agent_id(self) -> str:
        """Canonical agent id used across trust + access graphs."""
        return "chalke"
