# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The node says what client it needs; an older laptop is told once, in words (node_compat).

An install older than its node met the difference as an HTTP 403 or a 400 for a verb it had spelt
the old way. The node now advertises ``min_client`` and its ``capabilities`` on the public
``/api/v1/version``, and the laptop asks the first time a command reaches the node in a day:

- an older client is warned, with the one command that fixes it, and only once;
- a current client hears nothing;
- a node that does not answer changes nothing, and is not asked again on every command.
"""

from __future__ import annotations

import pytest

from axiom.infra import node_compat, serving_mode

NODE = "https://node.example.org"


@pytest.fixture
def compat(monkeypatch, tmp_path):
    """The check switched on, its cache in a temp folder, and a fake node behind the probe."""
    monkeypatch.delenv(node_compat.DISABLE_ENV, raising=False)
    monkeypatch.delenv(node_compat.TTL_ENV, raising=False)
    monkeypatch.setattr(node_compat, "cache_path", lambda: tmp_path / "node-compat.json")
    monkeypatch.setattr(node_compat, "_cli_name", lambda: "neut")
    calls: list[str] = []
    state = {"answer": (200, {"min_client": "0.70.9", "platform": "0.71.0",
                              "capabilities": ["chat", "data_reads"]})}

    def fake_get(url, headers, timeout):
        calls.append(url)
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(serving_mode, "_http_get", fake_get)
    return state, calls


def test_an_older_client_is_told_what_to_run(compat):
    said: list[str] = []
    text = node_compat.check_once(NODE, mine="0.70.4", emit=said.append, now=1000.0)
    assert said == [text]
    assert text == ("node.example.org needs axiom 0.70.9 or later; this computer has 0.70.4. "
                    "Run: neut update")


def test_it_is_said_once_not_on_every_command(compat):
    _, calls = compat
    said: list[str] = []
    for i in range(5):
        node_compat.check_once(NODE, mine="0.70.4", emit=said.append, now=1000.0 + i)
    assert len(said) == 1, said
    assert len(calls) == 1, "the node was asked again inside the TTL"


def test_it_is_said_again_the_next_day(compat):
    said: list[str] = []
    node_compat.check_once(NODE, mine="0.70.4", emit=said.append, now=1000.0)
    node_compat.check_once(NODE, mine="0.70.4", emit=said.append, now=1000.0 + node_compat.TTL_S + 1)
    assert len(said) == 2


def test_a_current_client_hears_nothing(compat):
    said: list[str] = []
    assert node_compat.check_once(NODE, mine="0.70.9", emit=said.append, now=1000.0) is None
    assert node_compat.check_once(NODE, mine="0.71.2", emit=said.append, now=1001.0) is None
    assert said == []


def test_an_unreachable_node_does_nothing_and_is_not_asked_on_every_command(compat):
    state, calls = compat
    state["answer"] = OSError("no route to host")
    said: list[str] = []
    for i in range(3):
        assert node_compat.check_once(NODE, mine="0.1.0", emit=said.append, now=1000.0 + i) is None
    assert said == []
    assert len(calls) == 1, "an offline laptop paid the timeout on every command"


def test_a_node_that_refuses_the_version_read_does_nothing(compat):
    state, _ = compat
    state["answer"] = (403, {})
    said: list[str] = []
    assert node_compat.check_once(NODE, mine="0.1.0", emit=said.append, now=1000.0) is None
    assert said == []


def test_a_node_that_advertises_nothing_does_nothing(compat):
    state, _ = compat
    state["answer"] = (200, {"platform": "0.68.0"})  # a node older than the advertisement
    said: list[str] = []
    assert node_compat.check_once(NODE, mine="0.1.0", emit=said.append, now=1000.0) is None


def test_the_check_never_raises(compat, monkeypatch):
    monkeypatch.setattr(node_compat, "_load", lambda path: 1 / 0)
    assert node_compat.check_once(NODE, mine="0.1.0", emit=print, now=1.0) is None


def test_it_can_be_switched_off(compat, monkeypatch):
    _, calls = compat
    monkeypatch.setenv(node_compat.DISABLE_ENV, "1")
    assert node_compat.check_once(NODE, mine="0.1.0", emit=print) is None
    assert calls == []


def test_the_node_name_comes_from_the_binding_not_a_constant():
    assert node_compat.warning("https://lab-node.example.org:8443", "1.2.3", "1.0.0", cli="axi") == (
        "lab-node.example.org needs axiom 1.2.3 or later; this computer has 1.0.0. Run: axi update")


def test_a_refusal_already_received_names_the_likely_cause(compat):
    node_compat.check_once(NODE, mine="0.70.4", emit=lambda _t: None, now=1000.0)
    assert "neut update" in node_compat.known_older(NODE, mine="0.70.4")
    assert node_compat.known_older(NODE, mine="0.70.9") == ""
    assert node_compat.capabilities(NODE) == ["chat", "data_reads"]


def test_versions_compare_as_releases_not_text():
    assert node_compat.older("0.70.9", "0.70.10")
    assert not node_compat.older("0.70.10", "0.70.9")
    assert not node_compat.older("1.0", "1.0.0")
    assert not node_compat.older("", "1.0")


# ------------------------------------------------------------------------- the node's side


def test_the_node_advertises_a_floor_by_default_and_an_operator_may_change_it(monkeypatch):
    monkeypatch.delenv(node_compat.MIN_CLIENT_ENV, raising=False)
    assert node_compat.min_client() == node_compat.MIN_CLIENT_DEFAULT
    monkeypatch.setenv(node_compat.MIN_CLIENT_ENV, "0.72.0")
    assert node_compat.min_client() == "0.72.0"
    monkeypatch.setenv(node_compat.MIN_CLIENT_ENV, "0")
    assert node_compat.min_client() == ""


def test_capabilities_are_read_off_what_is_mounted():
    paths = ["/v1/chat/completions", "/api/v1/rag/search", "/data/query", "/data/submit"]
    assert node_compat.advertised_capabilities(paths, holds_medallion=True) == [
        "chat", "data_reads", "kit_promotion", "retrieval"]
    # The data door answers 404 where the medallion is not held, so it is not promised there.
    assert node_compat.advertised_capabilities(paths, holds_medallion=False) == ["chat", "retrieval"]


def test_the_version_endpoint_carries_min_client_and_capabilities(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.webapp.api.routers import build_api_router

    monkeypatch.delenv(node_compat.MIN_CLIENT_ENV, raising=False)
    app = FastAPI()
    app.include_router(build_api_router())

    @app.post("/v1/chat/completions")
    def _chat() -> dict:
        return {}

    body = TestClient(app).get("/api/v1/version").json()
    assert body["min_client"] == node_compat.MIN_CLIENT_DEFAULT
    assert "chat" in body["capabilities"]
    assert "retrieval" not in body["capabilities"]


def test_a_read_through_the_binding_runs_the_check_first(monkeypatch):
    """The hook is in ``read_target.ask``: the one function every bound read goes through."""
    from axiom.extensions.builtins.data_platform import read_target

    checked: list[str] = []
    monkeypatch.setattr(node_compat, "check_once", lambda url, **kw: checked.append(url))
    monkeypatch.setattr(read_target, "_post", lambda target, verb, params: {"ok": True, "value": []})
    target = read_target.ReadTarget("binding", site="example-loop", face_url=NODE)
    assert read_target.ask(target, verb="data.tables").ok
    assert checked == [NODE]
