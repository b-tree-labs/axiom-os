# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Phase 0 end-to-end compose test — prove the primitives fit together.

Scenario:
  1. An instructor opens a Classroom, schedules a Period, enrolls students
  2. During the Period, instructor broadcasts an NL policy to @all-curios
  3. A student's chat request drives the research loop (via chat completion)
  4. Trace events land in bronze
  5. The research result is minted as a signed Finding
  6. The Finding is wrapped in a Digest and sent to a peer node
  7. The peer node verifies the chain, runs the eval gate, accepts it
  8. When the Period ends, the policy expires automatically

Every primitive touches this test. If any single one breaks, it fails here.
"""

from __future__ import annotations


def test_full_phase0_pipeline() -> None:
    # --- imports -------------------------------------------------------
    from axiom.artifacts import ArtifactRegistry
    from axiom.chat import AddressBook
    from axiom.classroom import ClassroomService, CourseService, PeriodService
    from axiom.evals import EvalCase, EvalHarness
    from axiom.findings import mint, verify_finding
    from axiom.medallion import BronzeStore, BronzeTraceSink
    from axiom.policy import PolicyEngine, expand_targets
    from axiom.research import ResearchLoop
    from axiom.serve import ChatCompletionsHandler
    from axiom.serve.research_backend import research_loop_backend
    from axiom.vega.federation import build_digest, receive_digest
    from axiom.vega.identity import generate_keypair

    # --- keys & identity ----------------------------------------------
    instructor_kp = generate_keypair()   # @ben:example-org (human)
    student_kp = generate_keypair()      # @alice:example-org (human)
    origin_node_kp = generate_keypair()  # @example-host:example-org (local node)
    peer_node_kp = generate_keypair()    # @laptop:axiom (peer node)

    pubkeys = {
        "@ben:example-org": instructor_kp.public_bytes,
        "@alice:example-org": student_kp.public_bytes,
        "@example-host:example-org": origin_node_kp.public_bytes,
        "@laptop:axiom": peer_node_kp.public_bytes,
    }

    # --- classroom setup ----------------------------------------------
    registry = ArtifactRegistry()
    courses = CourseService(registry=registry)
    classrooms = ClassroomService(registry=registry, courses=courses)
    periods = PeriodService(registry=registry, classrooms=classrooms)

    course_id = courses.create(name="NE101", owner="@ben:example-org")
    room_id = classrooms.open(
        course_id=course_id, term="Spring 2026", instructor="@ben:example-org"
    )
    classrooms.enroll(room_id, student="@alice:example-org", role="student")
    classrooms.enroll(room_id, student="@bob:example-org", role="student")

    period_id = periods.schedule(
        classroom_id=room_id, title="Lecture 1", starts_at=0.0, ends_at=3600.0
    )
    periods.start(period_id, now=10.0)
    periods.join(period_id, participant="@alice:example-org", now=15.0)

    # --- NL policy broadcast ------------------------------------------
    book = AddressBook()
    book.register("@alice-curio", agent="alice-curio", context="example-org")
    book.register("@bob-curio", agent="bob-curio", context="example-org")

    targets = expand_targets(
        raw_mentions=["@all-curios"],
        book=book,
        period_roster=["@alice-curio", "@bob-curio"],
    )
    policies = PolicyEngine()
    policies.broadcast(
        issuer="@ben:example-org",
        targets=targets,
        body="prioritize primary-source references",
        scope_kind="period",
        scope_id=period_id,
        now=20.0,
    )
    assert len(policies.active_for("alice-curio", now=30.0)) == 1

    # --- tracing → bronze --------------------------------------------
    bronze = BronzeStore()
    tracer = BronzeTraceSink(bronze=bronze, day="2026-04-13")

    # --- research loop + chat completion -----------------------------
    def runner(q, i, prior):
        return f"kinetics answer to: {q}"

    loop = ResearchLoop(
        runner=runner,
        scorer=lambda a, q: 0.95,
        refiner=lambda q, a, s: q,
        threshold=0.9,
        max_iterations=3,
        trace_provider=tracer,
    )

    handler = ChatCompletionsHandler(
        backend=research_loop_backend(loop), trace_provider=tracer
    )
    resp = handler.handle(
        {
            "model": "axiom-research",
            "messages": [
                {"role": "system", "content": "you are CURIO"},
                {"role": "user", "content": "what is Xe-135?"},
            ],
        }
    )
    answer = resp["choices"][0]["message"]["content"]
    assert "Xe-135" in answer

    # Bronze now has chat + research traces, generations, and scores.
    assert len(list(bronze.scan(source="traces", day="2026-04-13"))) >= 2
    assert len(list(bronze.scan(source="generations", day="2026-04-13"))) >= 2
    assert len(list(bronze.scan(source="scores", day="2026-04-13"))) >= 1

    # --- eval gate over the research output --------------------------
    gate = EvalHarness(thresholds={"nonempty": 1.0})
    gate_report = gate.run(
        runner=lambda _: answer,
        cases=[EvalCase(name="answer-present", input=None, expected=None)],
        scorers={"nonempty": lambda out, exp, **_: 1.0 if out else 0.0},
    )
    assert gate_report.passed is True

    # --- mint signed finding -----------------------------------------
    finding = mint(
        claim=answer,
        evidence=["period:" + period_id, "classroom:" + room_id],
        author_handle="@alice:example-org",
        author_keypair=student_kp,
    )
    assert verify_finding(finding, pubkeys) is True

    # --- federation: wrap in digest, ship to peer --------------------
    digest = build_digest(
        findings=[finding],
        from_node="@example-host:example-org",
        node_keypair=origin_node_kp,
        to_node="@laptop:axiom",
    )
    outcome = receive_digest(
        digest,
        pubkeys=pubkeys,
        peer_status="cluster",
        eval_fn=lambda f: 0.9,  # simulate peer's local eval gate
    )
    assert outcome.accepted == [finding]
    assert outcome.rejected == []
    assert outcome.peer_pass_rate == 1.0

    # --- period ends → policy expires --------------------------------
    periods.end(period_id, now=3700.0)
    policies.expire_scope(scope_kind="period", scope_id=period_id, now=3700.0)
    assert policies.active_for("alice-curio", now=3701.0) == []

    # --- classroom archive after term --------------------------------
    classrooms.archive(room_id, reason="term ended")
    assert classrooms.get(room_id).status == "archived"
