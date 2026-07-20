# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""IngestSink core + HTTP + skill tests.

These exercise push → bronze landing → callback fired → disposition with
the real :class:`BronzeWriter` + :class:`FilesystemBronzeSink` over a
tmp bronze root (no Postgres, no network, no bound port).
"""

from __future__ import annotations

import base64

import pytest

from axiom.rag.ingest_router import Disposition, ProvenanceRule

from ...bronze import BronzeWriter, FilesystemBronzeSink
from ...ingest_sink import IngestSink, PushItem, decode_content
from ...ingest_sink.core import (
    EVENT_EXCLUDED,
    EVENT_LANDED,
    EVENT_QUARANTINED,
    CallbackRegistry,
)


def _writer(tmp_path, rules=None, default=Disposition.ALLOW):
    return BronzeWriter(
        rules=rules or [],
        sink=FilesystemBronzeSink(root=tmp_path / "bronze"),
        default_disposition=default,
        default_tier="rag-community",
    )


def test_push_lands_content_in_bronze(tmp_path):
    sink = IngestSink(writer=_writer(tmp_path))
    result = sink.ingest(
        "events-feed",
        [PushItem(item_id="a1", content=b"hello world", source_path="docs/a1.txt")],
    )

    assert result.accepted == 1
    assert result.landed == 1
    assert result.quarantined == 0
    disp = result.items[0]
    assert disp.disposition == "landed"
    assert disp.content_hash is not None
    # Content blob is content-addressed under the bronze root.
    blobs = list((tmp_path / "bronze").rglob("*"))
    assert any(p.is_file() for p in blobs)


def test_landed_callback_fires(tmp_path):
    sink = IngestSink(writer=_writer(tmp_path))
    seen = []
    sink.register_callback(EVENT_LANDED, lambda ev, d, f: seen.append((ev, d.item_id, f.size)))

    sink.ingest("events-feed", [PushItem(item_id="a1", content=b"data")])

    assert seen == [(EVENT_LANDED, "a1", 4)]


def test_quarantine_disposition_and_callback(tmp_path):
    rules = [ProvenanceRule(pattern="secret/", disposition=Disposition.QUARANTINE)]
    sink = IngestSink(writer=_writer(tmp_path, rules=rules))
    fired = []
    sink.register_callback(EVENT_QUARANTINED, lambda ev, d, f: fired.append(d.item_id))

    result = sink.ingest(
        "events-feed",
        [PushItem(item_id="q1", content=b"x", source_path="secret/q1.txt")],
    )

    assert result.quarantined == 1
    assert result.landed == 0
    assert result.items[0].disposition == "quarantined"
    assert fired == ["q1"]


def test_exclude_writes_no_content(tmp_path):
    rules = [ProvenanceRule(pattern="blocked/", disposition=Disposition.EXCLUDE)]
    sink = IngestSink(writer=_writer(tmp_path, rules=rules))
    fired = []
    sink.register_callback(EVENT_EXCLUDED, lambda ev, d, f: fired.append(d.item_id))

    result = sink.ingest(
        "events-feed",
        [PushItem(item_id="e1", content=b"x", source_path="blocked/e1.txt")],
    )

    assert result.excluded == 1
    assert result.items[0].disposition == "excluded"
    assert result.items[0].content_hash is None
    assert fired == ["e1"]


def test_mixed_batch_counts(tmp_path):
    rules = [
        ProvenanceRule(pattern="hold/", disposition=Disposition.QUARANTINE),
        ProvenanceRule(pattern="no/", disposition=Disposition.EXCLUDE),
    ]
    sink = IngestSink(writer=_writer(tmp_path, rules=rules))
    result = sink.ingest(
        "feed",
        [
            PushItem(item_id="1", content=b"a", source_path="ok/1.txt"),
            PushItem(item_id="2", content=b"b", source_path="hold/2.txt"),
            PushItem(item_id="3", content=b"c", source_path="no/3.txt"),
        ],
    )
    assert (result.landed, result.quarantined, result.excluded) == (1, 1, 1)


def test_failing_callback_does_not_break_ingest(tmp_path):
    sink = IngestSink(writer=_writer(tmp_path))

    def boom(ev, d, f):
        raise RuntimeError("hook exploded")

    sink.register_callback(EVENT_LANDED, boom)
    result = sink.ingest("feed", [PushItem(item_id="a1", content=b"data")])
    assert result.landed == 1  # landing still succeeded


def test_callback_registry_rejects_unknown_event():
    reg = CallbackRegistry()
    with pytest.raises(ValueError):
        reg.register_callback("nonsense", lambda *a: None)


def test_decode_content_text_and_base64():
    assert decode_content("hi", encoding="text") == b"hi"
    assert decode_content(base64.b64encode(b"bytes").decode(), encoding="base64") == b"bytes"
    with pytest.raises(ValueError):
        decode_content("!!!not-base64!!!", encoding="base64")


# --- HTTP front door (in-process, no bound port) --------------------------


def test_http_ingest_endpoint(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ...ingest_sink.api import create_ingest_app

    sink = IngestSink(writer=_writer(tmp_path))
    landed = []
    sink.register_callback(EVENT_LANDED, lambda ev, d, f: landed.append(d.item_id))

    client = TestClient(create_ingest_app(sink))
    resp = client.post(
        "/ingest",
        json={
            "source": "events-feed",
            "items": [
                {"item_id": "a1", "content": "hello", "source_path": "docs/a1.txt"},
                {
                    "item_id": "b2",
                    "content": base64.b64encode(b"raw").decode(),
                    "content_encoding": "base64",
                },
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 2
    assert body["landed"] == 2
    assert landed == ["a1", "b2"]


def test_resolver_routes_per_source(tmp_path):
    """The /ingest router resolves a connector-specific sink per request, so the
    item lands in that connector's bronze tree (RATIONALIZE-1, no split brain)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ...ingest_sink import IngestSink
    from ...ingest_sink.api import build_ingest_router
    from axiom.extensions.builtins.http.server import create_app

    root_a = tmp_path / "a"
    sink_a = IngestSink(writer=BronzeWriter(
        rules=[], sink=FilesystemBronzeSink(root=root_a / "bronze"),
        default_disposition=Disposition.ALLOW, default_tier="rag-community"))

    def resolver(source: str):
        if source == "conn-a":
            return sink_a
        raise KeyError(source)

    app = create_app(title="t", version="0", description="")
    app.include_router(build_ingest_router(sink_resolver=resolver))
    client = TestClient(app)

    ok = client.post("/ingest", json={"source": "conn-a",
                                      "items": [{"item_id": "x", "content": "hi"}]})
    assert ok.status_code == 200 and ok.json()["landed"] == 1
    assert list((root_a / "bronze").rglob("x*")) or list((root_a / "bronze").rglob("*"))


def test_resolver_unknown_source_is_loud_422(tmp_path):
    """An unknown connector fails loudly (422), never a silent quarantine into a
    rule-less tree — the bug RATIONALIZE-1 removes."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ...ingest_sink.api import build_ingest_router
    from axiom.extensions.builtins.http.server import create_app

    def resolver(source: str):
        raise KeyError(source)

    app = create_app(title="t", version="0", description="")
    app.include_router(build_ingest_router(sink_resolver=resolver))
    client = TestClient(app)

    resp = client.post("/ingest", json={"source": "nope",
                                        "items": [{"item_id": "x", "content": "hi"}]})
    assert resp.status_code == 422
    assert "unknown connector" in resp.json()["detail"]


def test_build_ingest_router_requires_sink_or_resolver():
    from ...ingest_sink.api import build_ingest_router

    with pytest.raises(ValueError):
        build_ingest_router()


def test_http_rejects_bad_base64(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ...ingest_sink.api import create_ingest_app

    client = TestClient(create_ingest_app(IngestSink(writer=_writer(tmp_path))))
    resp = client.post(
        "/ingest",
        json={
            "source": "feed",
            "items": [{"item_id": "x", "content": "@@@", "content_encoding": "base64"}],
        },
    )
    assert resp.status_code == 422


def test_http_rejects_oversized_batch(tmp_path, monkeypatch):
    """DoS bound: a batch over the item cap is rejected at validation (422),
    not ingested. SRV-033."""
    pytest.importorskip("fastapi")
    monkeypatch.setenv("AXIOM_INGEST_MAX_ITEMS", "3")
    import importlib

    from ...ingest_sink import api as api_mod
    importlib.reload(api_mod)  # re-read the env-driven cap

    from fastapi.testclient import TestClient
    try:
        client = TestClient(api_mod.create_ingest_app(IngestSink(writer=_writer(tmp_path))))
        resp = client.post(
            "/ingest",
            json={"source": "feed",
                  "items": [{"item_id": f"i{n}", "content": "x"} for n in range(4)]},
        )
        assert resp.status_code == 422  # over the 3-item cap
    finally:
        monkeypatch.delenv("AXIOM_INGEST_MAX_ITEMS", raising=False)
        importlib.reload(api_mod)  # restore defaults for other tests


def test_http_rejects_oversized_content(tmp_path, monkeypatch):
    """DoS bound: per-item content over the char cap is rejected (422)."""
    pytest.importorskip("fastapi")
    monkeypatch.setenv("AXIOM_INGEST_MAX_CONTENT_CHARS", "16")
    import importlib

    from ...ingest_sink import api as api_mod
    importlib.reload(api_mod)

    from fastapi.testclient import TestClient
    try:
        client = TestClient(api_mod.create_ingest_app(IngestSink(writer=_writer(tmp_path))))
        resp = client.post(
            "/ingest",
            json={"source": "feed",
                  "items": [{"item_id": "big", "content": "x" * 64}]},
        )
        assert resp.status_code == 422  # over the 16-char cap
    finally:
        monkeypatch.delenv("AXIOM_INGEST_MAX_CONTENT_CHARS", raising=False)
        importlib.reload(api_mod)


def test_push_builds_and_persists_generic_funnel(tmp_path):
    """The push job populates the SAME generic IngestRunReport funnel as the
    pull job (job-agnostic primitive), persisted via the injected RunStore."""
    from axiom.extensions.builtins.data_platform.ingest_run import InMemoryRunStore

    sink = IngestSink(writer=_writer(tmp_path))
    run_store = InMemoryRunStore()
    result = sink.ingest(
        "events-feed",
        [PushItem(item_id="a1", content=b"hello", source_path="docs/a1.txt")],
        run_store=run_store,
    )

    assert result.funnel is not None
    assert result.funnel["job_kind"] == "push"
    assert result.funnel["source"] == "events-feed"
    # push starts at to_process (no discovery step) — assert that shape.
    stages = [s["stage"] for s in result.funnel["funnel"]]
    assert stages == ["to_process", "loaded", "indexed"]
    sc = {s["stage"]: s for s in result.funnel["funnel"]}
    assert sc["to_process"]["entered"] == 1
    assert sc["loaded"]["advanced"] == 1

    persisted = run_store.recent(source="events-feed")
    assert len(persisted) == 1
    assert persisted[0]["job_kind"] == "push"


def test_push_funnel_records_write_failure(tmp_path):
    """A write failure shows up as a failed-with-cause in the funnel, not a
    silent drop — the 'why' the stage funnel exists for."""
    from axiom.extensions.builtins.data_platform.ingest_run import InMemoryRunStore

    class _BoomWriter:
        def write(self, fetched):
            raise RuntimeError("disk full")

    sink = IngestSink(writer=_BoomWriter())
    run_store = InMemoryRunStore()
    result = sink.ingest(
        "feed", [PushItem(item_id="x", content=b"data")], run_store=run_store,
    )
    assert result.errored == 1
    loaded = next(s for s in result.funnel["funnel"] if s["stage"] == "loaded")
    assert loaded["failed"] == {"write_failed": 1}
    assert result.funnel["status"] == "failed"  # all items errored, none landed


# --- MCP/skill front door -------------------------------------------------


def test_ingest_push_skill(tmp_path):
    import logging

    from axiom.infra.skills import SkillContext, SkillRegistry

    from ...skills import ingest_push

    sink = IngestSink(writer=_writer(tmp_path))
    fired = []
    sink.register_callback(EVENT_LANDED, lambda ev, d, f: fired.append(d.item_id))

    ctx = SkillContext(
        registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("test")
    )
    result = ingest_push.run(
        {
            "source": "events-feed",
            "sink": sink,
            "items": [{"item_id": "a1", "content": "hello"}],
        },
        ctx,
    )
    assert result.ok
    assert result.value["landed"] == 1
    assert result.value["items"][0]["disposition"] == "landed"
    assert fired == ["a1"]


def test_ingest_push_skill_requires_source(tmp_path):
    import logging

    from axiom.infra.skills import SkillContext, SkillRegistry

    from ...skills import ingest_push

    ctx = SkillContext(
        registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("test")
    )
    result = ingest_push.run({"items": [{"item_id": "a"}]}, ctx)
    assert not result.ok
    assert "source" in result.errors[0]
