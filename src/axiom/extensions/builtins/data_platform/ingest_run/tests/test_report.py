# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The generic ingest-run funnel — job-agnostic, no infra needed."""
from __future__ import annotations

from ..report import DEFAULT_STAGES, IngestRunReport, RunStatus
from ..store import InMemoryRunStore, JsonlRunStore


def test_start_seeds_declared_stages():
    rep = IngestRunReport.start("pull", source="conn-a")
    assert rep.job_kind == "pull"
    assert rep.source == "conn-a"
    assert rep.status is RunStatus.RUNNING
    assert tuple(s["stage"] for s in rep.to_dict()["funnel"]) == DEFAULT_STAGES


def test_clean_run_is_succeeded():
    rep = IngestRunReport.start("pull")
    rep.entered("discovered", 10)
    rep.advanced("discovered", 10)
    rep.entered("fetched", 10)
    rep.advanced("fetched", 10)
    rep.finish()
    assert rep.status is RunStatus.SUCCEEDED
    assert rep.total_dropped == 0 and rep.total_failed == 0
    assert rep.duration_s is not None


def test_dropped_or_failed_makes_run_partial():
    rep = IngestRunReport.start("pull")
    rep.entered("to_process", 10)
    rep.advanced("to_process", 7)
    rep.dropped("to_process", "unchanged", 3)
    rep.finish()
    assert rep.status is RunStatus.PARTIAL
    sc = rep.stage("to_process")
    assert sc.dropped_total == 3
    assert sc.dropped["unchanged"] == 3
    assert sc.unaccounted() == 0  # 7 advanced + 3 dropped == 10 entered


def test_failed_with_cause_buckets():
    rep = IngestRunReport.start("pull")
    rep.entered("fetched", 5)
    rep.advanced("fetched", 3)
    rep.failed("fetched", "401", 1)
    rep.failed("fetched", "429", 1)
    rep.finish()
    assert rep.total_failed == 2
    assert rep.stage("fetched").failed == {"401": 1, "429": 1}


def test_refuse_marks_failed():
    rep = IngestRunReport.start("pull").refuse("volume gate said no")
    assert rep.status is RunStatus.FAILED
    assert rep.refused_reason == "volume gate said no"
    assert rep.finished_at is not None


def test_finish_failed_overrides_partial():
    rep = IngestRunReport.start("pull")
    rep.entered("fetched", 1)
    rep.advanced("fetched", 1)
    rep.finish(failed=True)
    assert rep.status is RunStatus.FAILED


def test_custom_stages_for_a_non_rag_job():
    # A job that only fetches + lands (no extract/chunk/index) declares its
    # own stages — the funnel must not assume the RAG pipeline.
    rep = IngestRunReport.start(
        "mirror", source="s3://bucket",
        stages=("discovered", "fetched", "loaded"),
    )
    rep.entered("discovered", 4)
    rep.advanced("discovered", 4)
    rep.entered("loaded", 4)
    rep.advanced("loaded", 4)
    rep.finish()
    funnel_stages = [s["stage"] for s in rep.to_dict()["funnel"]]
    assert funnel_stages == ["discovered", "fetched", "loaded"]
    assert "indexed" not in funnel_stages
    assert rep.status is RunStatus.SUCCEEDED


def test_unknown_stage_recorded_appears_after_declared():
    rep = IngestRunReport.start("pull", stages=("discovered",))
    rep.entered("discovered", 1)
    rep.advanced("discovered", 1)
    rep.entered("surprise_stage", 1)  # not declared
    rep.finish()
    stages = [s["stage"] for s in rep.to_dict()["funnel"]]
    assert stages[0] == "discovered"
    assert "surprise_stage" in stages


def test_metrics_freeform():
    rep = IngestRunReport.start("push")
    rep.set_metric("bytes_fetched", 0)
    rep.add_metric("bytes_fetched", 2048)
    rep.add_metric("bytes_fetched", 1024)
    rep.set_metric("extractor", {"pdf": 3, "docx": 1})
    rep.finish()
    m = rep.to_dict()["metrics"]
    assert m["bytes_fetched"] == 3072
    assert m["extractor"] == {"pdf": 3, "docx": 1}


def test_render_is_human_readable():
    rep = IngestRunReport.start("pull", source="conn-a")
    rep.entered("discovered", 10)
    rep.advanced("discovered", 10)
    rep.entered("to_process", 10)
    rep.advanced("to_process", 8)
    rep.dropped("to_process", "unchanged", 2)
    rep.finish()
    text = rep.render()
    assert "conn-a" in text
    assert "discovered" in text and "to_process" in text
    assert "unchanged:2" in text


# -- stores ------------------------------------------------------------------


def test_inmemory_store_save_get_recent():
    store = InMemoryRunStore()
    a = IngestRunReport.start("pull", source="x").finish()
    b = IngestRunReport.start("pull", source="y").finish()
    store.save(a)
    store.save(b)
    assert store.get(a.run_id)["source"] == "x"
    recent = store.recent(limit=10)
    assert [r["run_id"] for r in recent] == [b.run_id, a.run_id]  # newest first
    assert [r["run_id"] for r in store.recent(source="x")] == [a.run_id]


def test_inmemory_store_resave_updates_in_place():
    store = InMemoryRunStore()
    rep = IngestRunReport.start("pull", source="x")
    store.save(rep)  # RUNNING snapshot
    rep.entered("fetched", 1)
    rep.advanced("fetched", 1)
    store.save(rep.finish())  # final snapshot
    assert store.get(rep.run_id)["status"] == "succeeded"
    assert len(store.recent()) == 1  # not duplicated


def test_jsonl_store_last_wins(tmp_path):
    store = JsonlRunStore(tmp_path / "runs.jsonl")
    rep = IngestRunReport.start("cdc", source="conn-a")
    store.save(rep)              # RUNNING
    store.save(rep.finish())     # final supersedes
    got = store.get(rep.run_id)
    assert got["status"] == "succeeded"
    assert len(store.recent()) == 1


def test_jsonl_store_survives_new_instance(tmp_path):
    path = tmp_path / "runs.jsonl"
    JsonlRunStore(path).save(IngestRunReport.start("pull", source="x").finish())
    # A fresh instance (new process) reads the same history.
    assert len(JsonlRunStore(path).recent()) == 1
