# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for trace export — CSV + optional Parquet + anonymization.

Per spec-classroom.md §2.11 + prd §5.10. Standalone-first: CSV always
works with stdlib; Parquet is optional (requires pyarrow).

Anonymization is a flag: student_id → deterministic pseudonym (sha256
of student_id truncated) so research exports strip PII while
preserving per-student aggregates.
"""

from __future__ import annotations

import csv

import pytest


def _trace(student_id: str, session_id: str, **kw):
    base = {
        "trace_id": f"{session_id}-{kw.get('turn_index', 0)}",
        "student_id": student_id,
        "session_id": session_id,
        "session_type": kw.get("session_type", "chat"),
        "turn_index": kw.get("turn_index", 0),
        "timestamp": "2026-04-16T10:00:00+00:00",
        "tokens": {"prompt": 100, "completion": 50},
        "rag_results_count": 3,
        "labels": kw.get("labels", ["q_and_a"]),
        "topics": kw.get("topics", []),
    }
    return base


class TestCSVExport:
    def test_csv_round_trip(self, tmp_path):
        from axiom.extensions.builtins.classroom.trace_export import export_traces_csv

        traces = [
            _trace("s1", "sess-a"),
            _trace("s1", "sess-a", turn_index=1),
            _trace("s2", "sess-b"),
        ]
        path = tmp_path / "out.csv"
        export_traces_csv(traces, path, anonymize=False)

        assert path.exists()
        with path.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 3
        assert rows[0]["student_id"] == "s1"
        assert rows[0]["session_id"] == "sess-a"
        assert rows[0]["labels"]  # comma-joined

    def test_csv_anonymization(self, tmp_path):
        from axiom.extensions.builtins.classroom.trace_export import export_traces_csv

        traces = [_trace("real-student-id", "a")]
        path = tmp_path / "out.csv"
        export_traces_csv(traces, path, anonymize=True)

        with path.open() as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["student_id"] != "real-student-id"
        # Pseudonym is non-empty and deterministic
        assert len(rows[0]["student_id"]) > 0

    def test_anonymization_deterministic(self, tmp_path):
        from axiom.extensions.builtins.classroom.trace_export import (
            pseudonymize,
        )

        assert pseudonymize("s1") == pseudonymize("s1")
        assert pseudonymize("s1") != pseudonymize("s2")


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401

        return True
    except ImportError:
        return False


class TestParquetExport:
    @pytest.mark.skipif(not _has_pyarrow(), reason="pyarrow not installed")
    def test_parquet_written_when_pyarrow_available(self, tmp_path):
        from axiom.extensions.builtins.classroom.trace_export import (
            export_traces_parquet,
        )

        traces = [_trace("s1", "a"), _trace("s2", "b")]
        path = tmp_path / "out.parquet"
        export_traces_parquet(traces, path, anonymize=False)
        assert path.exists()

    @pytest.mark.skipif(_has_pyarrow(), reason="pyarrow is installed")
    def test_parquet_without_pyarrow_raises_informative_error(self, tmp_path):
        from axiom.extensions.builtins.classroom.trace_export import (
            export_traces_parquet,
        )

        with pytest.raises(RuntimeError, match="pyarrow"):
            export_traces_parquet([_trace("s1", "a")], tmp_path / "x.parquet")


class TestEnrichedExport:
    """Export combines traces + metrics + quiz scores in one dataset."""

    def test_enriched_includes_quiz_grades(self, tmp_path):
        from axiom.extensions.builtins.classroom.quiz_scoring import ScoredResponse
        from axiom.extensions.builtins.classroom.trace_export import (
            export_research_dataset,
        )

        traces = [_trace("s1", "a")]
        quiz_responses = [
            ScoredResponse("s1", "pre", "Q1", "mcq", final_score=1.0),
        ]

        path = tmp_path / "dataset.csv"
        export_research_dataset(
            traces=traces,
            quiz_responses=quiz_responses,
            path=path,
            anonymize=False,
        )
        assert path.exists()
        with path.open() as f:
            text = f.read()
        # Dataset should contain both trace + quiz rows
        assert "s1" in text
        assert "pre" in text


class TestConsentFilter:
    def test_students_without_consent_excluded(self, tmp_path):
        from axiom.extensions.builtins.classroom.trace_export import export_traces_csv

        traces = [
            _trace("s1", "a"),
            _trace("s2", "b"),
        ]
        path = tmp_path / "out.csv"
        # Only s1 consented
        export_traces_csv(traces, path, anonymize=False,
                          consented_student_ids={"s1"})

        with path.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["student_id"] == "s1"
