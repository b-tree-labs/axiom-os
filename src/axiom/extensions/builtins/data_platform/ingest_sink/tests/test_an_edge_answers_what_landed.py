# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An ingest edge answers a producer's "did my rows land?" from what it holds.

Found in the 2026-10-07 partner dress rehearsal: a collector sending to an edge
held every run forever. Before sending a run it asks
``GET /ingest/summary?by=channel`` whether that run already landed, so it never
sends one twice; the composed ``/ingest`` mount did not serve the summary at
all, and where the summary existed it counted ``gold.signals``, which an edge
does not have. Unable to tell, the collector held the run: the safe failure,
and a pipeline that sends nothing.

On an edge, landed means "this edge holds it durably", which is exactly what
the producer needs to know. The count is the collector's own: one reading per
non-null channel value per row, for the caller's site only.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

ROWS = [
    {"producer_id": "p1", "feed": "loop.f", "seq": i, "ts": f"2026-10-06T00:00:{i:02d}Z",
     "schema_id": "loop/rows-v1", "values": {"TC1": 20.0 + i, "TC2": None if i == 0 else 1.0}}
    for i in range(5)
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method, url, token, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - test loopback
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode()[:300]}


@pytest.fixture
def edge(tmp_path, monkeypatch):
    pytest.importorskip("uvicorn")
    import uvicorn

    from axiom.webauth import append_api_key_record, mint_api_key

    from ...agents.plinth.connectors import ConnectorConfig, save_connector

    state = tmp_path / "state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_INGEST_OUTBOX_DIR", str(tmp_path / "outbox"))
    keys = tmp_path / "keys.json"
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(keys))
    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.setenv("AXIOM_EDGE_DOWNSTREAM", "@down:org")
    for var in ("AXIOM_API_KEY", "AXIOM_HTTP_API_KEYS", "AXIOM_SERVE_INSECURE"):
        monkeypatch.delenv(var, raising=False)
    for site in ("site-a", "site-b"):
        save_connector(ConnectorConfig(name=f"{site}-src", kind="push", bronze_root=str(tmp_path / "bronze"),
                                       site=site, default_disposition="allow", default_tier="restricted"),
                       state_dir=state)
    tokens = {}
    for site in ("site-a", "site-b"):
        tok, rec = mint_api_key(principal=f"@daq:{site}", scopes=("data_platform:invoke", "data_platform:read"), site=site)
        append_api_key_record(keys, rec)
        tokens[site] = tok
    push_only, rec = mint_api_key(principal="@pushonly:site-a", scopes=("data_platform:invoke",), site="site-a")
    append_api_key_record(keys, rec)
    tokens["push_only"] = push_only

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
    url = f"http://127.0.0.1:{port}"
    for site in ("site-a", "site-b"):
        st, body = _http("POST", f"{url}/ingest/rows", tokens[site], {
            "source": f"{site}-src",
            "batches": [{"item_id": f"{site}-run", "schema_ref": "loop/rows-v1", "rows": ROWS,
                         "metadata": {"feed": "loop.f"}}],
        })
        assert st == 200, body
    yield url, tokens
    server.should_exit = True
    t.join(timeout=5)


WINDOW = "from=2026-10-06T00:00:00Z&to=2026-10-06T00:01:00Z"


def test_the_edge_counts_a_window_per_channel_like_the_collector(edge):
    url, tokens = edge
    st, body = _http("GET", f"{url}/ingest/summary?by=channel&feed=loop.f&{WINDOW}", tokens["site-a"])
    assert st == 200, body
    assert body["site"] == "site-a" and body["by"] == "channel"
    counts = {c["channel"]: c["rows"] for c in body["channels"]}
    # TC2 is null in the first row: the collector does not count it, nor does the edge.
    assert counts == {"TC1": 5, "TC2": 4}
    assert body["total_rows"] == 9


def test_another_sites_rows_are_never_counted(edge):
    url, tokens = edge
    _, a = _http("GET", f"{url}/ingest/summary?by=channel&feed=loop.f&{WINDOW}", tokens["site-a"])
    _, b = _http("GET", f"{url}/ingest/summary?by=channel&feed=loop.f&{WINDOW}", tokens["site-b"])
    assert a["total_rows"] == 9 and b["total_rows"] == 9 and b["site"] == "site-b"


def test_outside_the_window_or_another_feed_counts_nothing(edge):
    url, tokens = edge
    _, late = _http("GET", f"{url}/ingest/summary?by=channel&feed=loop.f&from=2026-10-07T00:00:00Z&to=2026-10-07T01:00:00Z", tokens["site-a"])
    _, other = _http("GET", f"{url}/ingest/summary?by=channel&feed=other&{WINDOW}", tokens["site-a"])
    assert late["channels"] == [] and other["channels"] == []


def test_the_per_feed_summary_names_the_feed_and_its_schema(edge):
    url, tokens = edge
    st, body = _http("GET", f"{url}/ingest/summary", tokens["site-a"])
    assert st == 200, body
    assert [(f["feed"], f["schema_ref"], f["rows"]) for f in body["feeds"]] == [("loop.f", "loop/rows-v1", 9)]


def test_a_push_only_key_may_not_read_even_its_own_summary(edge):
    # Reading is a scope of its own; the producer key UT hands out carries it.
    url, tokens = edge
    assert _http("GET", f"{url}/ingest/summary?by=channel&feed=loop.f&{WINDOW}", tokens["push_only"])[0] == 403
