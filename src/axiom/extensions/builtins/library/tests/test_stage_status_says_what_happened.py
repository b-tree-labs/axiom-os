# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A stage says ``done`` only when it did its work (#1954, spec-upload-pipeline §1).

The tray draws one dot per stage from these statuses. Before this, every run
ended with six ``done`` dots: the indexed stage was ``done`` with "no retrieval
store on this install" or "no text was extracted", and the extracted stage was
``done`` when an extractor had failed. A person could not tell that the file
was not searchable, or why.

Acceptance rows (tagged in each test):

- ST-1: a file whose text went into the retrieval store ends ``indexed: done``.
- ST-2: retrieval down: ``indexed`` is ``pending`` and its detail says why.
- ST-3: nothing to index (no text, a binary type): ``indexed`` is ``skipped``,
  its detail says the file is not searchable and why.
- ST-4: an extractor failed: ``extracted`` is ``partial`` and names it.
- ST-5: a route failed: ``conformed`` is ``partial``.
- ST-6: an export-control hold: ``indexed`` is ``held``.
- ST-7: a finished job's progress is 1.0 whatever its stages say.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library import store as lib_store
from axiom.extensions.builtins.library.extract import registry
from axiom.extensions.builtins.library.extract.contract import (
    ExtractionResult,
    TextSection,
    manifest_from_dict,
)
from axiom.extensions.builtins.library.extract.routes import clear_routes, register_route
from axiom.extensions.builtins.library.tests.test_library_upload import ALICE, _up

API = "/api/v1/library"


@pytest.fixture(autouse=True)
def _pipeline(monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    contrib.reset(rediscover=False)
    registry.clear_local()
    clear_routes()
    yield
    registry.clear_local()
    clear_routes()
    contrib.reset()


def _m(name, **kw):
    base = {"name": name, "version": "1.0.0", "summary": name, "outputs": ["metadata"], "match": {"extensions": [".wid"]}}
    base.update(kw)
    return manifest_from_dict(base, scope="pack")


def _stages(client, item):
    job = client.get(f"{API}/items/{item['id']}/pipeline", headers=ALICE).json()["job"]
    return job, {s["stage"]: s for s in job["stages"]}


def test_st1_indexed_text_ends_done(client, library):
    item = _up(client, "notes.txt", b"the pump was serviced on tuesday\n").json()["items"][0]
    job, by = _stages(client, item)
    assert item["index_status"] == "indexed"
    assert by["indexed"]["status"] == "done", by["indexed"]
    assert job["progress"] == 1.0


def test_st2_retrieval_down_is_pending_with_the_reason(client, library):
    lib_store.set_rag_store(None)
    item = _up(client, "notes.txt", b"the pump was serviced on tuesday\n").json()["items"][0]
    job, by = _stages(client, item)
    assert item["index_status"] == "pending"
    assert by["indexed"]["status"] == "pending", by["indexed"]
    assert "no retrieval store" in by["indexed"]["detail"]
    assert job["state"] == "done" and job["progress"] == 1.0  # ST-7: the file is filed; the stage says what is owed


def test_st3_a_file_with_nothing_to_index_is_skipped_and_says_why(client, library):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    item = _up(client, "plot.png", png).json()["items"][0]
    job, by = _stages(client, item)
    assert item["index_status"] == "unsupported"
    assert by["indexed"]["status"] == "skipped", by["indexed"]
    assert by["indexed"]["detail"].startswith("not searchable: "), by["indexed"]["detail"]
    assert job["progress"] == 1.0


def test_st4_a_failed_extractor_makes_extracted_partial(client, library):
    def boom(ctx):
        raise RuntimeError("parser bug")

    def good(ctx):
        return ExtractionResult(metadata={"run": "42"}, text=[TextSection("widget run 42 notes")], summary="widget log")

    registry.register(_m("pack.extract.boom", priority=5), boom)
    registry.register(_m("pack.extract.good", outputs=["metadata", "text"], tags=["rag"],
                         accepts=[{"extension": ".wid", "sniff": "text"}]), good)
    item = _up(client, "run.wid", b"WIDGET\n").json()["items"][0]
    _, by = _stages(client, item)
    assert by["extracted"]["status"] == "partial", by["extracted"]
    assert "pack.extract.boom" in by["extracted"]["detail"]
    assert by["indexed"]["status"] == "done"


def test_st5_a_failed_route_makes_conformed_partial(client, library):
    def deck(ctx):
        return ExtractionResult(metadata={}, summary="w")

    def broken(ctx):
        raise RuntimeError("registry offline")

    registry.register(_m("pack.extract.deck", tags=["input-deck"], accepts=[{"extension": ".wid", "sniff": "text"}]), deck)
    register_route("input-deck", "pack.route.broken", broken)
    item = _up(client, "run.wid", b"WIDGET\n").json()["items"][0]
    _, by = _stages(client, item)
    assert by["conformed"]["status"] == "partial", by["conformed"]
    assert "registry offline" in by["conformed"]["detail"]


def test_st6_a_hold_is_held(client, library):
    item = _up(client, "memo.md", b"# Memo\n\nExport Controlled Information. Do not share.\n").json()["items"][0]
    assert item["index_status"] == "held"
    _, by = _stages(client, item)
    assert by["indexed"]["status"] == "held", by["indexed"]
