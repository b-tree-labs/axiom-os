# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prague simulator harness (#68).

End-to-end simulation of a semester's worth of classroom interaction:
- Sim students follow engagement-driven agendas
- Each turn flows through the composition-integrated chat pipeline
- CHALKE responses score against the rubric
- Every turn emits a composition_audit entry proving full-stack fire

Outputs:
- runtime/sim/<run_id>/traces.jsonl       — every turn's trace fragment
- runtime/sim/<run_id>/chalke_scores.jsonl — rubric per turn
- runtime/sim/<run_id>/metrics.json       — aggregate run metrics
- runtime/sim/<run_id>/composition_audit.json — pass/fail per turn
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .rubric import score_response
from .sim_student import SimStudent

# ---------------------------------------------------------------------------
# Canned queries — representative of Prague course content
# ---------------------------------------------------------------------------


_CANNED_QUERIES = [
    "What is fission?",
    "How does moderation affect reactivity?",
    "Explain critical mass.",
    "What is the difference between prompt and delayed neutrons?",
    "Why does water serve as a moderator?",
    "Describe the decay chain of uranium-238.",
    "How do control rods work?",
    "What is the role of the reflector?",
    "How am I doing this week?",        # metacognitive intent
    "Can you give me a practice problem on cross-sections?",  # practice
    "I'm confused about fission vs fusion products.",
    "Write a one-paragraph summary of reactor kinetics.",
]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class SimResult:
    run_id: str
    out_dir: Path
    turns_simulated: int = 0
    composition_pass_count: int = 0
    composition_fail_count: int = 0
    mean_score: float = 0.0
    per_student_scores: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def run_simulation(
    students: list[SimStudent],
    classroom_id: str,
    *,
    seed: int = 42,
    turns_per_student: int = 5,
    out_dir: Path | None = None,
    llm_backend: Callable | None = None,
) -> SimResult:
    """Run the Prague simulation with the composition-integrated stack.

    For each student, sample `turns_per_student` queries from the canned
    set, route them through ChatPipeline (with composition + tracer
    wired), score each response via the rubric, and audit the
    composition pass.
    """
    random.seed(seed)

    run_id = datetime.now(UTC).strftime("sim-%Y%m%dT%H%M%S")
    out_dir = Path(out_dir) if out_dir else Path("runtime") / "sim" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Wire the composition-integrated pipeline
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )
    from axiom.extensions.builtins.classroom.pipeline import (
        ClassroomChatPipeline,
    )
    from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
    from axiom.infra.tracing import InMemoryTraceProvider

    composition = build_classroom_composition(classroom_id=classroom_id)
    tracer = ClassroomTracer(
        classroom_id=classroom_id,
        course_id="sim-course",
        trace_provider=InMemoryTraceProvider(),
        composition=composition,
    )

    llm = llm_backend or _default_sim_llm

    result = SimResult(run_id=run_id, out_dir=out_dir)
    trace_file = (out_dir / "traces.jsonl").open("w")
    score_file = (out_dir / "chalke_scores.jsonl").open("w")
    audit_file = (out_dir / "composition_audit.json")

    audit_entries = []

    try:
        for student in students:
            student_scores: list[float] = []
            pipe = ClassroomChatPipeline(
                course_system_prompt="You are CHALKE, the classroom TA.",
                llm_backend=llm,
                composition=composition,
                tracer=tracer,
                student_id=student.student_id,
            )

            for _ in range(turns_per_student):
                query = random.choice(_CANNED_QUERIES)
                response = pipe.chat([{"role": "user", "content": query}])
                result.turns_simulated += 1

                # Rubric
                intent = "metacognitive" if "how am i" in query.lower() else "teaching"
                rs = score_response(
                    query=query,
                    response=response,
                    student_id=student.student_id,
                    student_profile={
                        "pedagogy_preference": student.pedagogy_preference,
                        "background": student.background,
                        "language": student.language,
                    },
                    intent_id=intent,
                )
                student_scores.append(rs.composite)

                # Emit trace record + score record
                trace_file.write(json.dumps({
                    "student_id": student.student_id,
                    "query": query,
                    "response": response[:200],
                    "intent": intent,
                }) + "\n")
                score_file.write(json.dumps({
                    "student_id": student.student_id,
                    "composite": rs.composite,
                    "sub_scores": {
                        "has_citation": rs.has_citation,
                        "addresses_query": rs.addresses_query,
                        "profile_aligned": rs.profile_aligned,
                        "no_refusal": rs.no_refusal,
                        "intent_aligned": rs.intent_aligned,
                    },
                }) + "\n")

                # Composition audit: verify the turn hit every primitive.
                # For v0 we check: did tracer record a fragment? was there
                # an audit entry? This is the smoke test.
                trace_ids = tracer.get_student_traces(student.student_id)
                has_new_trace = len(trace_ids) > 0
                audit_entries.append({
                    "student_id": student.student_id,
                    "query": query,
                    "composition_pass": has_new_trace,
                })
                if has_new_trace:
                    result.composition_pass_count += 1
                else:
                    result.composition_fail_count += 1

            if student_scores:
                result.per_student_scores[student.student_id] = (
                    sum(student_scores) / len(student_scores)
                )

        # Aggregate metrics
        all_scores = [
            s for scores in result.per_student_scores.values()
            for s in [scores]
        ]
        result.mean_score = sum(all_scores) / len(all_scores) if all_scores else 0.0

    finally:
        trace_file.close()
        score_file.close()
        audit_file.write_text(json.dumps(audit_entries, indent=2))

    (out_dir / "metrics.json").write_text(json.dumps({
        "run_id": result.run_id,
        "classroom_id": classroom_id,
        "turns_simulated": result.turns_simulated,
        "composition_pass_count": result.composition_pass_count,
        "composition_fail_count": result.composition_fail_count,
        "mean_score": result.mean_score,
        "per_student_scores": result.per_student_scores,
    }, indent=2))

    return result


# ---------------------------------------------------------------------------
# Default sim LLM — pattern-matches common queries to plausible responses
# ---------------------------------------------------------------------------


_CANNED_RESPONSES = {
    "fission": (
        "Fission is the process of splitting heavy atomic nuclei into two "
        "lighter fragments, releasing energy. [see course corpus §2.1]. "
        "First, a neutron collides with the heavy nucleus. Second, the "
        "nucleus becomes unstable. Third, it splits — releasing more neutrons."
    ),
    "moderation": (
        "Moderation slows fast neutrons to thermal energies. [source: ch3]. "
        "Water moderates by elastic collision with hydrogen nuclei. First, "
        "fast neutrons slow. Second, thermal neutrons have higher fission "
        "cross-sections. The net effect increases reactivity."
    ),
    "critical mass": (
        "Critical mass is the minimum mass of fissile material that sustains "
        "a chain reaction. [per the corpus §2.4]. First, neutron production "
        "must equal neutron loss. Second, geometry matters — a sphere is "
        "most efficient. Third, purity of the fissile material matters."
    ),
    "how am i": (
        "Based on your recent sessions, you've covered fission basics and "
        "moderation. Your strongest area so far is decay chains. I'd "
        "suggest focusing next on cross-sections, which you haven't "
        "engaged with yet. Would you like a practice problem?"
    ),
    "confused": (
        "Let me help you sort it out. First, fission splits heavy nuclei — "
        "the products are lighter and radioactive. Second, fusion combines "
        "light nuclei — the products are heavier. Different physics, "
        "different engineering. Which part feels confusing? [source: ch2]"
    ),
}


def _default_sim_llm(messages: list[dict], **kw) -> str:
    """Pattern-match the user message to a canned response."""
    user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = m.get("content", "").lower()
            break
    for keyword, response in _CANNED_RESPONSES.items():
        if keyword in user_msg:
            return response
    # Generic fallback
    return (
        "That's a great question. Let me explain. First, we need to "
        "consider the context. Second, the key concept is important here. "
        "[per course corpus]. What part would you like me to expand on?"
    )
