# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The heartbeat goes where the data goes, with the data's credential, and lands.

End to end through a real face: the sender posts, the router keeps it per
site and node, and the read side reports it online. Nothing reaches bronze.
"""

from __future__ import annotations


from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform.daq.beat import HeartbeatSender
from axiom.extensions.builtins.data_platform.ingest_sink import heartbeat as hb
from axiom.extensions.builtins.data_platform.ingest_sink.api import build_tabular_ingest_router
from axiom.extensions.builtins.data_platform.ingest_sink.tabular import TabularIngestSink
from axiom.extensions.builtins.data_platform.ingest_sink.tenancy import TenancyPolicy


class _Unwritable:
    def write(self, *a, **k):  # pragma: no cover
        raise AssertionError("a heartbeat reached bronze")


class _ClientTransport:
    """Posts through FastAPI's test client: a real router, no socket."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.seen_auth: list[str] = []

    def post(self, url, body, headers):
        self.seen_auth.append(headers.get("Authorization", ""))
        path = url.split("://", 1)[-1].split("/", 1)[1]
        r = self.client.post("/" + path, content=body, headers=headers)
        return r.status_code, r.text, dict(r.headers)


def test_a_beat_lands_and_the_platform_sees_the_node_online(tmp_path, monkeypatch):
    monkeypatch.setenv(hb.DIR_ENV, str(tmp_path / "hb"))
    app = FastAPI()
    app.include_router(build_tabular_ingest_router(TabularIngestSink(writer=_Unwritable()), tenancy=TenancyPolicy()))  # type: ignore[arg-type]
    client = TestClient(app)
    transport = _ClientTransport(client)
    sender = HeartbeatSender(face_url="https://face.example/", source="site-a-push",
                             bearer=lambda: "k-123", transport=transport)
    assert sender.due()
    assert sender.send({"node": "daq-pc-1", "collector": "running", "readings_today": 5})
    assert not sender.due()
    assert transport.seen_auth == ["Bearer k-123"]
    (node,) = client.get("/ingest/heartbeat").json()["nodes"]
    assert node["node"] == "daq-pc-1" and node["state"] == "online"


def test_a_refused_beat_is_reported_not_raised(tmp_path):
    class Refuses:
        def post(self, url, body, headers):
            return 401, "no key", {}

    sender = HeartbeatSender(face_url="https://f", source="s", bearer=lambda: None, transport=Refuses())
    assert sender.send({"node": "n"}) is False
    assert sender.last_status == 401 and "401" in sender.last_error


def test_an_unreachable_face_is_reported_not_raised():
    class Down:
        def post(self, url, body, headers):
            raise OSError("no route to host")

    sender = HeartbeatSender(face_url="https://f", source="s", bearer=lambda: None, transport=Down())
    assert sender.send({"node": "n"}) is False
    assert "no route to host" in sender.last_error
