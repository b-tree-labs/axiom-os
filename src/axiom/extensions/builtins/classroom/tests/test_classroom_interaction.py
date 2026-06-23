# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the classroom interaction store.

Tier B piece — coordinator-side log of student questions so CHALKE
can surface signals like "hot topics this week" and "quiet students"
in the daily brief. Append-only JSONL so an instructor can cat /
grep / pipe the raw records for ad-hoc inspection.
"""

from __future__ import annotations

import json

from axiom.extensions.builtins.classroom.classroom_interaction import (
    ClassroomInteractionStore,
    InteractionRecord,
    topic_histogram,
)

# ---------------------------------------------------------------------------
# Append + read
# ---------------------------------------------------------------------------


class TestAppendAndRead:
    def test_empty_store_lists_nothing(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        assert store.list() == []

    def test_append_record_persists(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        store.append(InteractionRecord(
            student_id="alice@example.org",
            question="What is a control rod?",
            had_answer=True,
            citations_count=2,
            timestamp="2026-04-23T10:00:00+00:00",
        ))

        records = store.list()
        assert len(records) == 1
        assert records[0].student_id == "alice@example.org"
        assert records[0].question == "What is a control rod?"
        assert records[0].had_answer is True
        assert records[0].citations_count == 2

    def test_multiple_appends_preserve_order(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        for i in range(5):
            store.append(InteractionRecord(
                student_id=f"s{i}",
                question=f"Q{i}",
                had_answer=True,
                citations_count=1,
                timestamp=f"2026-04-23T10:{i:02d}:00+00:00",
            ))
        records = store.list()
        assert [r.student_id for r in records] == [f"s{i}" for i in range(5)]

    def test_records_survive_fresh_instance(self, tmp_path):
        s1 = ClassroomInteractionStore(tmp_path)
        s1.append(InteractionRecord(
            student_id="s", question="q", had_answer=True,
            citations_count=0, timestamp="t",
        ))
        s2 = ClassroomInteractionStore(tmp_path)
        assert len(s2.list()) == 1


# ---------------------------------------------------------------------------
# Disk layout — locked so CHALKE can depend on it
# ---------------------------------------------------------------------------


class TestDiskLayout:
    def test_jsonl_file_at_expected_path(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        store.append(InteractionRecord(
            student_id="s", question="q", had_answer=True,
            citations_count=0, timestamp="t",
        ))
        path = tmp_path / "interactions.jsonl"
        assert path.is_file()
        line = path.read_text().strip()
        obj = json.loads(line)
        assert obj["student_id"] == "s"
        assert obj["question"] == "q"


# ---------------------------------------------------------------------------
# Stats — filter by student, filter by recent, topic histogram
# ---------------------------------------------------------------------------


class TestStats:
    def _populate(self, store: ClassroomInteractionStore) -> None:
        for question, student in [
            ("What is a control rod?", "alice"),
            ("How do control rods work?", "alice"),
            ("What is fission?", "bob"),
            ("Tell me about control rods", "carol"),
            ("What is the primary coolant loop?", "bob"),
        ]:
            store.append(InteractionRecord(
                student_id=student,
                question=question,
                had_answer=True,
                citations_count=1,
                timestamp="2026-04-23T10:00:00+00:00",
            ))

    def test_by_student(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate(store)
        alice = store.by_student("alice")
        assert len(alice) == 2

    def test_quiet_students_list(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate(store)
        quiet = store.quiet_students(
            roster=["alice", "bob", "carol", "dave", "scan"],
        )
        # dave + scan asked nothing.
        assert set(quiet) == {"dave", "scan"}

    def test_distinct_student_count(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate(store)
        assert store.distinct_students() == 3  # alice + bob + carol

    def test_topic_histogram_clusters_by_keyword(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate(store)
        # 3 questions mention "control rod(s)", 1 mentions fission, 1 coolant.
        hist = topic_histogram(store.list(), top_n=3)
        keywords = [entry[0] for entry in hist]
        assert "control" in keywords or "rods" in keywords or "control rod" in " ".join(keywords)


class TestSummaryForStudent:
    """Memory-transparency view: what's on file for a single student.

    Returned shape is the wire format the student-side `axi classroom me
    --memory` fetches via GET /classroom/memory/{student_id}. The point
    is to make the coordinator's memory legible to the student themselves.
    """

    def test_summary_empty_for_unknown_student(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        summary = store.summary_for_student("nobody@here")
        assert summary["student_id"] == "nobody@here"
        assert summary["question_count"] == 0
        assert summary["answered_count"] == 0
        assert summary["unanswered_count"] == 0
        assert summary["modes_used"] == {}
        assert summary["recent_questions"] == []

    def test_summary_counts_questions_and_modes(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        store.append(InteractionRecord(
            student_id="alice", question="Q1", had_answer=True,
            citations_count=2, timestamp="2026-04-23T10:00:00+00:00",
            mode="ask",
        ))
        store.append(InteractionRecord(
            student_id="alice", question="Q2", had_answer=False,
            citations_count=0, timestamp="2026-04-23T11:00:00+00:00",
            mode="tutor",
        ))
        store.append(InteractionRecord(
            student_id="alice", question="Q3", had_answer=True,
            citations_count=1, timestamp="2026-04-23T12:00:00+00:00",
            mode="ask",
        ))
        store.append(InteractionRecord(
            student_id="bob", question="other", had_answer=True,
            citations_count=1, timestamp="2026-04-23T11:30:00+00:00",
            mode="ask",
        ))

        summary = store.summary_for_student("alice")
        assert summary["question_count"] == 3
        assert summary["answered_count"] == 2
        assert summary["unanswered_count"] == 1
        assert summary["modes_used"] == {"ask": 2, "tutor": 1}

    def test_summary_recent_questions_sorted_newest_first(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        for ts, q in [
            ("2026-04-23T10:00:00+00:00", "old"),
            ("2026-04-23T12:00:00+00:00", "new"),
            ("2026-04-23T11:00:00+00:00", "middle"),
        ]:
            store.append(InteractionRecord(
                student_id="alice", question=q, had_answer=True,
                citations_count=1, timestamp=ts, mode="ask",
            ))
        summary = store.summary_for_student("alice", recent_n=3)
        ordered = [r["question"] for r in summary["recent_questions"]]
        assert ordered == ["new", "middle", "old"]

    def test_summary_excludes_other_students(self, tmp_path):
        """Memory transparency must scope to the requesting student only —
        no leakage of peer questions."""
        store = ClassroomInteractionStore(tmp_path)
        store.append(InteractionRecord(
            student_id="alice", question="Alice's question", had_answer=True,
            citations_count=1, timestamp="2026-04-23T10:00:00+00:00",
            mode="ask",
        ))
        store.append(InteractionRecord(
            student_id="bob", question="Bob's question", had_answer=True,
            citations_count=1, timestamp="2026-04-23T11:00:00+00:00",
            mode="ask",
        ))
        summary = store.summary_for_student("alice")
        questions = [r["question"] for r in summary["recent_questions"]]
        assert "Bob's question" not in questions
        assert "Alice's question" in questions


class TestInteractionId:
    """Hash-based, deterministic ID. Old records (no id field on disk)
    still get the same id every time the file is read, so tombstones
    work without a migration."""

    def test_id_is_stable(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        rec = InteractionRecord(
            student_id="alice", question="Q", had_answer=True,
            citations_count=1, timestamp="2026-04-23T10:00:00+00:00",
            mode="ask",
        )
        store.append(rec)
        loaded = store.list()[0]
        assert loaded.interaction_id == rec.interaction_id

    def test_id_differs_per_question(self, tmp_path):
        a = InteractionRecord(
            student_id="alice", question="Q1", had_answer=True,
            citations_count=1, timestamp="2026-04-23T10:00:00+00:00",
        )
        b = InteractionRecord(
            student_id="alice", question="Q2", had_answer=True,
            citations_count=1, timestamp="2026-04-23T10:00:00+00:00",
        )
        assert a.interaction_id != b.interaction_id

    def test_id_differs_per_student(self):
        a = InteractionRecord(
            student_id="alice", question="Q", had_answer=True,
            citations_count=1, timestamp="2026-04-23T10:00:00+00:00",
        )
        b = InteractionRecord(
            student_id="bob", question="Q", had_answer=True,
            citations_count=1, timestamp="2026-04-23T10:00:00+00:00",
        )
        assert a.interaction_id != b.interaction_id


class TestForget:
    """Tombstone-based retraction. Append-only JSONL preserves the
    audit trail (the original line stays on disk) but ``list()``
    filters tombstoned ids, so no caller surfaces the retracted
    content. The instructor sees a count, not the question."""

    def _populate_alice(self, store):
        for i, q in enumerate(["Q1", "Q2", "Q3"]):
            store.append(InteractionRecord(
                student_id="alice", question=q, had_answer=True,
                citations_count=1,
                timestamp=f"2026-04-23T10:0{i}:00+00:00",
                mode="ask",
            ))

    def test_forget_removes_record_from_list(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate_alice(store)
        target = store.list()[1]
        result = store.forget(
            student_id="alice", interaction_id=target.interaction_id,
        )
        assert result["forgotten"] is True
        questions = [r.question for r in store.list()]
        assert target.question not in questions

    def test_forget_unknown_id_fails(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate_alice(store)
        result = store.forget(student_id="alice", interaction_id="bogus")
        assert result["forgotten"] is False

    def test_forget_wrong_student_fails(self, tmp_path):
        """Bob can't forget Alice's question even if he knows the id."""
        store = ClassroomInteractionStore(tmp_path)
        self._populate_alice(store)
        target = store.list()[0]
        result = store.forget(
            student_id="bob", interaction_id=target.interaction_id,
        )
        assert result["forgotten"] is False

    def test_forget_is_idempotent(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate_alice(store)
        target = store.list()[0]
        first = store.forget(
            student_id="alice", interaction_id=target.interaction_id,
        )
        assert first["forgotten"] is True
        second = store.forget(
            student_id="alice", interaction_id=target.interaction_id,
        )
        assert second["forgotten"] is True
        assert second.get("idempotent") is True

    def test_forgotten_count_increments(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate_alice(store)
        assert store.forgotten_count("alice") == 0
        target = store.list()[0]
        store.forget(
            student_id="alice", interaction_id=target.interaction_id,
        )
        assert store.forgotten_count("alice") == 1

    def test_summary_reflects_retraction(self, tmp_path):
        store = ClassroomInteractionStore(tmp_path)
        self._populate_alice(store)
        target = store.list()[0]
        store.forget(
            student_id="alice", interaction_id=target.interaction_id,
        )
        summary = store.summary_for_student("alice")
        assert summary["question_count"] == 2  # one tombstoned out
        assert summary["forgotten_count"] == 1
        questions = [r["question"] for r in summary["recent_questions"]]
        assert target.question not in questions

    def test_audit_trail_preserved_on_disk(self, tmp_path):
        """The instructor can grep the raw file to see retractions —
        original record + tombstone both stay on disk."""
        store = ClassroomInteractionStore(tmp_path)
        self._populate_alice(store)
        target = store.list()[0]
        store.forget(
            student_id="alice", interaction_id=target.interaction_id,
        )
        raw = (tmp_path / "interactions.jsonl").read_text().splitlines()
        # 3 originals + 1 tombstone = 4 lines.
        assert len(raw) == 4
        tombstone_lines = [
            json.loads(line) for line in raw
            if json.loads(line).get("_tombstone")
        ]
        assert len(tombstone_lines) == 1
        assert tombstone_lines[0]["interaction_id"] == target.interaction_id


# ---------------------------------------------------------------------------
# ADR-033 Stage 1 — dual-write hook into the canonical L1 memory layer
# ---------------------------------------------------------------------------


class TestStage1DualWrite:
    """The optional ``memory_writer`` callable on ``ClassroomInteractionStore``
    is invoked alongside the JSONL append. When unset, behaviour is
    unchanged — the JSONL primary write is the only one. When set, the
    writer receives the appended record + the scope id; the JSONL write
    is never blocked by writer failures during Stage 1 migration.
    """

    def test_writer_unset_is_default(self, tmp_path):
        """No writer = no extra calls. Plain append still works."""
        calls: list = []
        store = ClassroomInteractionStore(tmp_path)
        store.append(InteractionRecord(
            student_id="alice", question="Q", had_answer=True,
            citations_count=1, timestamp="2026-04-25T10:00:00+00:00",
            classroom_id="NE101", mode="ask",
        ))
        # No spy means we just confirm the JSONL write happened.
        records = store.list()
        assert len(records) == 1
        # And calls (a separate list outside the store) stays empty.
        assert calls == []

    def test_writer_invoked_alongside_jsonl(self, tmp_path):
        calls: list = []

        def writer(record, scope):
            calls.append((record, scope))

        store = ClassroomInteractionStore(
            tmp_path, memory_writer=writer, scope_id="NE101",
        )
        rec = InteractionRecord(
            student_id="alice", question="What is criticality?",
            had_answer=True, citations_count=2,
            timestamp="2026-04-25T10:00:00+00:00",
            classroom_id="NE101", mode="ask",
        )
        store.append(rec)

        # Both writes happened.
        assert len(store.list()) == 1
        assert len(calls) == 1
        assert calls[0][0] == rec
        assert calls[0][1] == "NE101"

    def test_writer_failure_does_not_break_jsonl(self, tmp_path):
        """Stage 1 migration: L1 mirror is best-effort. A failure in the
        memory writer must not stop the primary JSONL write — the
        bespoke store stays authoritative until Stage 4 promotes L1."""
        def writer(record, scope):
            raise RuntimeError("L1 backend unreachable")

        store = ClassroomInteractionStore(tmp_path, memory_writer=writer)
        store.append(InteractionRecord(
            student_id="alice", question="Q", had_answer=True,
            citations_count=1, timestamp="2026-04-25T10:00:00+00:00",
            classroom_id="NE101", mode="ask",
        ))
        # JSONL persisted despite writer raising.
        assert len(store.list()) == 1

    def test_scope_id_defaults_to_directory_name(self, tmp_path):
        """If the caller doesn't set scope_id, the directory name is the
        fallback — useful for tests + simple deployments."""
        calls: list = []

        def writer(record, scope):
            calls.append((record, scope))

        # tmp_path's name is the unique pytest-generated id; we just
        # verify it gets passed through unchanged.
        store = ClassroomInteractionStore(tmp_path, memory_writer=writer)
        store.append(InteractionRecord(
            student_id="alice", question="Q", had_answer=True,
            citations_count=1, timestamp="2026-04-25T10:00:00+00:00",
            classroom_id=None, mode="ask",
        ))
        assert calls[0][1] == tmp_path.name


class TestStage1IntegrationWithCompositionService:
    """Integration test: interaction_writer adapter feeds a real
    CompositionService that persists fragments to ArtifactRegistry.
    Proves the production-shape bridge actually works end-to-end."""

    def _build_service(self, tmp_path):
        """Minimal wired CompositionService — same pattern tests/memory/
        test_composition.py uses."""
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
        from axiom.memory.access import AccessGraphs
        from axiom.memory.attest import AuditLog
        from axiom.memory.composition import CompositionService
        from axiom.memory.policy import PolicyCoord
        from axiom.memory.trust import TrustGraph

        return CompositionService(
            artifact_registry=ArtifactRegistry(
                backend=SQLiteBackend(tmp_path / "artifacts.db"),
            ),
            audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=None),
            signing_keypair=None,
            policy_coord=PolicyCoord(global_policy={"write": "private"}),
            access_graphs=AccessGraphs(),
            trust_graph=TrustGraph(),
        )

    def test_appended_record_lands_as_fragment(self, tmp_path):
        from axiom.memory.adapters import interaction_writer

        cs = self._build_service(tmp_path)
        store = ClassroomInteractionStore(
            tmp_path / "store",
            memory_writer=interaction_writer(cs),
            scope_id="NE101",
        )
        rec = InteractionRecord(
            student_id="alice@u.edu",
            question="What is criticality?",
            had_answer=True, citations_count=2,
            timestamp="2026-04-25T10:00:00+00:00",
            classroom_id="NE101", mode="ask",
        )
        store.append(rec)

        # JSONL primary write happened.
        assert len(store.list()) == 1

        # And the fragment landed in ArtifactRegistry.
        artifacts = list(cs.artifact_registry.list(kind="fragment"))
        assert len(artifacts) == 1
        frag_data = artifacts[0].data
        assert frag_data["cognitive_type"] == "episodic"
        assert frag_data["content"]["question"] == "What is criticality?"
        assert frag_data["content"]["interaction_id"] == rec.interaction_id
        assert frag_data["content"]["classroom_id"] == "NE101"
        assert frag_data["provenance"]["principal_id"] == "alice@u.edu"

    def test_fragment_inherits_default_visibility_and_classification(
        self, tmp_path,
    ):
        """Default-deny posture: fragments written through the adapter
        carry SCOPE_INTERNAL visibility + unclassified classification
        unless explicitly overridden. Means a student question never
        leaks even if federation is misconfigured."""
        from axiom.memory.adapters import interaction_writer

        cs = self._build_service(tmp_path)
        store = ClassroomInteractionStore(
            tmp_path / "store",
            memory_writer=interaction_writer(cs),
            scope_id="NE101",
        )
        store.append(InteractionRecord(
            student_id="alice", question="Q", had_answer=True,
            citations_count=1, timestamp="2026-04-25T10:00:00+00:00",
            classroom_id="NE101", mode="ask",
        ))
        artifacts = list(cs.artifact_registry.list(kind="fragment"))
        frag_data = artifacts[0].data
        assert frag_data["visibility"] == "scope_internal"
        assert frag_data["classification"]["level"] == "unclassified"
