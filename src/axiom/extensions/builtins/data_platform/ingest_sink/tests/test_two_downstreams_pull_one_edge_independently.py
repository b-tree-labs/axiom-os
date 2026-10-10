# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Two downstream nodes pull one ingest edge, each at its own pace, each getting everything once.

A data platform moving to a new home runs old and new side by side for a while:
both pull the same edge until their contents are shown to agree, and only then
does the old one stop. What has to hold:

- each downstream lands every batch exactly once, whatever the other does, even
  when one stops pulling for a long time;
- the edge deletes a batch's bulk only after every downstream it serves has
  acknowledged it, and not before a minimum age;
- a downstream falling too far behind raises an alarm, and is dropped from what
  retention waits for only if the operator declared a limit for that;
- with none of the new settings, an edge behaves exactly as before: it deletes
  nothing and its health answer is unchanged.

A downstream acknowledges by pulling: its ``after`` cursor advances only once
the batches before it are durable on its side (see the puller), so the cursor it
sends is a statement of what it holds.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from axiom.rag.ingest_router import Disposition

from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ...ingest_sink import PushRowBatch, TabularIngestSink
from ...ingest_sink.edge import EdgeOutbox
from ...ingest_sink.edge_retention import EdgeAcks, lag_report, prune

uvicorn = pytest.importorskip("uvicorn")

ROWS = [
    {"channel": "TC1", "ts": f"2026-10-08T00:00:{i:02d}Z", "value": 20.0 + i, "unit": "degC"}
    for i in range(10)
]
OLD, NEW = "@old-platform:org", "@new-platform:org"


def _writer(root: Path) -> TabularBronzeWriter:
    return TabularBronzeWriter(
        rules=[],
        sink=FilesystemTabularBronzeSink(root=root),
        default_disposition=Disposition.ALLOW,
        default_tier="restricted",
    )


def _batch(i: int) -> PushRowBatch:
    return PushRowBatch(
        item_id=f"run-{i}",
        schema_ref="loop/rows-v1",
        rows=[dict(r, run=i) for r in ROWS],
        metadata={"site": "site-a"},
    )


def _landed(root: Path, source: str = "site-a-src") -> list[str]:
    out = []
    for f in sorted((root / source / "_rows").rglob("*.jsonl")):
        out += [json.loads(line)["row_hash"] for line in f.read_text().splitlines() if line.strip()]
    return sorted(out)


def _blobs(root: Path, source: str = "site-a-src") -> int:
    return len(list((root / source / "_content").rglob("*"))) - len(
        list((root / source / "_content").glob("*"))
    )


# -- the outbox level ---------------------------------------------------------


@pytest.fixture
def edge(tmp_path):
    outbox = EdgeOutbox(tmp_path / "edge")
    sink = TabularIngestSink(writer=_writer(tmp_path / "edge-bronze"), on_landed=outbox.record)
    return outbox, sink, tmp_path / "edge-bronze", EdgeAcks(tmp_path / "edge")


def _bronze_for(root):
    return lambda source: root


def test_acks_only_move_forward_and_survive_a_restart(tmp_path):
    acks = EdgeAcks(tmp_path)
    acks.ack(OLD, 5)
    acks.ack(OLD, 3)  # a stale page request does not move it back
    acks.ack(NEW, 2)
    assert EdgeAcks(tmp_path).positions() == {OLD: 5, NEW: 2}


def test_nothing_is_deleted_until_every_downstream_has_it(edge):
    outbox, sink, bronze, acks = edge
    for i in range(6):
        sink.ingest_rows("site-a-src", [_batch(i)])
    acks.ack(OLD, 6)  # the old platform has everything; the new one has nothing yet
    result = prune(
        outbox, acks, downstreams=[OLD, NEW], bronze_root_for=_bronze_for(bronze), min_age_hours=0
    )
    assert result["deleted_batches"] == 0
    assert len(_landed(bronze)) == 60

    acks.ack(NEW, 4)
    result = prune(
        outbox, acks, downstreams=[OLD, NEW], bronze_root_for=_bronze_for(bronze), min_age_hours=0
    )
    assert result["deleted_batches"] == 4 and result["pruned_through"] == 4
    assert len(_landed(bronze)) == 20  # runs 4 and 5 are still held for the new platform
    # The outbox itself is untouched: numbering and dedupe never depend on retention.
    assert outbox.last_seq() == 6 and len(outbox.read(after=0)) == 6


def test_nothing_younger_than_the_minimum_age_is_deleted(edge):
    outbox, sink, bronze, acks = edge
    for i in range(3):
        sink.ingest_rows("site-a-src", [_batch(i)])
    acks.ack(OLD, 3)
    acks.ack(NEW, 3)
    result = prune(
        outbox, acks, downstreams=[OLD, NEW], bronze_root_for=_bronze_for(bronze), min_age_hours=1
    )
    assert result["deleted_batches"] == 0


def test_a_downstream_that_never_pulled_holds_everything(edge):
    outbox, sink, bronze, acks = edge
    for i in range(3):
        sink.ingest_rows("site-a-src", [_batch(i)])
    acks.ack(OLD, 3)
    result = prune(
        outbox, acks, downstreams=[OLD, NEW], bronze_root_for=_bronze_for(bronze), min_age_hours=0
    )
    assert result["deleted_batches"] == 0


def test_a_lagging_downstream_raises_an_alarm(edge):
    outbox, sink, bronze, acks = edge
    sink.ingest_rows("site-a-src", [_batch(0)])
    acks.ack(OLD, 1)
    later = time.time() + 3 * 3600
    report = lag_report(outbox, acks, downstreams=[OLD, NEW], max_lag_hours=2, now=later)
    assert report["status"] == "degraded"
    assert [d["principal"] for d in report["lagging"]] == [NEW]
    assert report["lagging"][0]["behind_batches"] == 1


def test_a_declared_detach_limit_lets_retention_pass_a_dead_downstream_and_keeps_the_alarm(edge):
    outbox, sink, bronze, acks = edge
    for i in range(3):
        sink.ingest_rows("site-a-src", [_batch(i)])
    acks.ack(OLD, 3)
    later = time.time() + 10 * 3600
    kept = prune(
        outbox,
        acks,
        downstreams=[OLD, NEW],
        bronze_root_for=_bronze_for(bronze),
        min_age_hours=0,
        now=later,
    )
    assert kept["deleted_batches"] == 0  # without a declared limit, the edge waits forever
    gone = prune(
        outbox,
        acks,
        downstreams=[OLD, NEW],
        bronze_root_for=_bronze_for(bronze),
        min_age_hours=0,
        detach_after_hours=8,
        now=later,
    )
    assert gone["deleted_batches"] == 3 and gone["detached"] == [NEW]
    report = lag_report(outbox, acks, downstreams=[OLD, NEW], max_lag_hours=2, now=later)
    assert report["status"] == "degraded"


def test_a_shared_item_id_keeps_its_rows_file_until_every_batch_in_it_is_prunable(edge):
    outbox, sink, bronze, acks = edge
    first = PushRowBatch(
        item_id="day", schema_ref="loop/rows-v1", rows=[dict(r, part=1) for r in ROWS]
    )
    second = PushRowBatch(
        item_id="day", schema_ref="loop/rows-v1", rows=[dict(r, part=2) for r in ROWS]
    )
    sink.ingest_rows("site-a-src", [first])
    sink.ingest_rows("site-a-src", [second])
    acks.ack(OLD, 2)
    acks.ack(NEW, 1)  # the new platform has part 1 only
    prune(
        outbox, acks, downstreams=[OLD, NEW], bronze_root_for=_bronze_for(bronze), min_age_hours=0
    )
    assert len(_landed(bronze)) == 20  # the shared rows file stays for part 2


# -- two real pullers over HTTP -------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def two_downstream_edge(tmp_path, monkeypatch):
    from axiom.webauth import append_api_key_record, mint_api_key

    from ...agents.plinth.connectors import ConnectorConfig, save_connector

    state = tmp_path / "edge-state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_INGEST_OUTBOX_DIR", str(tmp_path / "edge-outbox"))
    keys = tmp_path / "api-keys.json"
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(keys))
    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.setenv("AXIOM_EDGE_DOWNSTREAM", f"{OLD},{NEW}")
    monkeypatch.setenv("AXIOM_EDGE_RETAIN_MIN_HOURS", "0")
    monkeypatch.setenv("AXIOM_EDGE_MAX_LAG_HOURS", "2")
    for var in (
        "AXIOM_API_KEY",
        "AXIOM_HTTP_API_KEYS",
        "AXIOM_SERVE_INSECURE",
        "AXIOM_EDGE_DETACH_AFTER_HOURS",
    ):
        monkeypatch.delenv(var, raising=False)
    save_connector(
        ConnectorConfig(
            name="site-a-src",
            kind="push",
            bronze_root=str(tmp_path / "edge-bronze"),
            site="site-a",
            default_disposition="allow",
            default_tier="restricted",
        ),
        state_dir=state,
    )
    producer, rec = mint_api_key(
        principal="@daq:site-a", scopes=("data_platform:invoke", "*:read"), site="site-a"
    )
    append_api_key_record(keys, rec)
    tokens = {}
    for who in (OLD, NEW):
        tokens[who], rec = mint_api_key(principal=who, scopes=("edge_export:read",))
        append_api_key_record(keys, rec)

    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.registry import RouterRegistry

    app = compose_app(profile="ingest-edge", registry=RouterRegistry(), bind_host="0.0.0.0")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield {
        "url": f"http://127.0.0.1:{port}",
        "producer": producer,
        "tokens": tokens,
        "edge_bronze": tmp_path / "edge-bronze",
    }
    server.should_exit = True
    t.join(timeout=5)


def _http(method, url, token, body=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - test loopback
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def _push(edge, i):
    status, body = _http(
        "POST",
        f"{edge['url']}/ingest/rows",
        edge["producer"],
        {
            "source": "site-a-src",
            "batches": [
                {
                    "item_id": f"run-{i}",
                    "schema_ref": "loop/rows-v1",
                    "rows": [dict(r, run=i) for r in ROWS],
                }
            ],
        },
    )
    assert status == 200, body


def _puller(edge, who, root: Path):
    from ...sources.edge import EdgePuller, FileCursor, HttpEdge

    down = TabularIngestSink(writer=_writer(root / "bronze"))
    return EdgePuller(
        HttpEdge(edge["url"], token=edge["tokens"][who]),
        sink_for=lambda s: down,
        cursor=FileCursor(root / "cursor.json"),
    )


def test_old_and_new_platforms_each_get_everything_once_at_their_own_pace(
    two_downstream_edge, tmp_path
):
    edge = two_downstream_edge
    old, new = _puller(edge, OLD, tmp_path / "old"), _puller(edge, NEW, tmp_path / "new")

    for i in range(3):
        _push(edge, i)
    assert old.pull().records == 3  # the old platform keeps up; the new one is paused

    for i in range(3, 6):
        _push(edge, i)
    assert old.pull().records == 3
    # The new platform was away the whole time; the edge held its share.
    assert len(_landed(edge["edge_bronze"])) == 60

    first = new.pull()
    assert first.records == 6 and first.rows_landed == 60
    assert new.pull().records == 0 and old.pull().records == 0

    old_rows, new_rows = _landed(tmp_path / "old" / "bronze"), _landed(tmp_path / "new" / "bronze")
    assert old_rows == new_rows and len(old_rows) == 60
    # Both now hold everything, so the edge has let all of it go.
    assert _landed(edge["edge_bronze"]) == []


def test_the_edge_frees_space_only_after_both_have_pulled_and_says_who_is_behind(
    two_downstream_edge, tmp_path
):
    edge = two_downstream_edge
    old, new = _puller(edge, OLD, tmp_path / "old"), _puller(edge, NEW, tmp_path / "new")
    for i in range(4):
        _push(edge, i)
    old.pull()
    old.pull()  # the second page request carries the cursor that acknowledges everything
    # The new platform has acknowledged nothing, so everything is still held.
    assert len(_landed(edge["edge_bronze"])) == 40

    new.pull()
    new.pull()
    old.pull()  # any page request runs retention once acknowledgements allow it
    assert len(_landed(edge["edge_bronze"])) == 0
    # Retention never touched what a puller reads next: new batches still flow.
    _push(edge, 9)
    assert new.pull().records == 1 and old.pull().records == 1


def test_an_edge_with_one_downstream_and_no_new_settings_keeps_everything(tmp_path, monkeypatch):
    """The deployed edge (one downstream, retention unset) must not change behavior."""
    from ...ingest_sink.edge_retention import maybe_prune_after_page

    monkeypatch.setenv("AXIOM_EDGE_DOWNSTREAM", OLD)
    for var in (
        "AXIOM_EDGE_RETAIN_MIN_HOURS",
        "AXIOM_EDGE_MAX_LAG_HOURS",
        "AXIOM_EDGE_DETACH_AFTER_HOURS",
    ):
        monkeypatch.delenv(var, raising=False)
    outbox = EdgeOutbox(tmp_path / "edge")
    sink = TabularIngestSink(writer=_writer(tmp_path / "edge-bronze"), on_landed=outbox.record)
    for i in range(3):
        sink.ingest_rows("site-a-src", [_batch(i)])
    maybe_prune_after_page(
        outbox, principal=OLD, after=3, bronze_root_for=_bronze_for(tmp_path / "edge-bronze")
    )
    assert len(_landed(tmp_path / "edge-bronze")) == 30
    from ...ingest_sink.edge_retention import health_detail

    assert health_detail(outbox) == {}
