# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A laptop bound to a site node answers a data question by calling a verb on the node.

Found live, on a laptop bound to a node where a contributor's kit verb had been promoted and
approved, and where ``data kit-verbs`` and ``data kit-verb`` listed it and ran it:

- ``chat --ask "what does <verb> say for the last day?"`` answered "I don't have a <verb> tool"
  with ``verbs_called: []``. Chat's tools were the local ones; the node's data reads and the
  site's promoted verbs were not among them.
- ``chat --ask "what power ... is now, latest one please"`` answered with a number and
  ``verbs_called: []``: a value from the model's memory, served as if it had been read.

Here one process plays both ends. The node is the real data door (``read_api``) behind the real
gate (the authz hook resolving an issued, site-bound key, and the middleware holding each request
to the key's site), on a kit medallion with a promoted verb, plus a stand-in ``/v1`` model behind
the same gate. The laptop is bound to it and runs ``chat --ask --json``.

Needs Docker (or AXIOM_KIT_DSN), like the other kit-on-a-node tests.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import shutil
import uuid
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi import APIRouter, Request

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import host
from axiom.extensions.builtins.data_platform.kit.bundle import pack
from axiom.extensions.builtins.data_platform.kit.medallion import dsn

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("docker") or os.environ.get("AXIOM_KIT_DSN")),
        reason="needs Docker or AXIOM_KIT_DSN",
    ),
]

SITE = "chat-site"
VERB = "temperature_rise_by_day"  # the kit template's verb
FACE = "https://node.example.org"

_ON_NODE: contextvars.ContextVar[bool] = contextvars.ContextVar("on_node", default=False)


@pytest.fixture(scope="module")
def medallion(tmp_path_factory):
    """A node's medallion with the site's rows and its promoted, approved kit."""
    repo = tmp_path_factory.mktemp("chat-node")
    kit.init(repo, SITE)
    p = kit.load(repo)
    kit.up(p)
    kit.try_kit(p)
    d = dsn(p)
    host.setup(d)
    r = host.submit(d, pack(p).data, caller_site=None, principal=f"@c:{SITE}", home=True)
    assert r["state"] == "checked", r["problems"]
    host.approve(d, r["id"], approver="@operator:node")
    yield d
    kit.down(p)


class _Model:
    """The node's ``/v1`` model. ``mode`` says how it treats the tools a client offers."""

    def __init__(self) -> None:
        self.mode = "calls"
        self.window = 0
        self.seen: list[dict] = []

    def router(self):
        router = APIRouter()

        @router.get("/v1/models")
        def models() -> dict:
            item = {"id": "node-model", "object": "model"}
            if self.window:
                item["max_model_len"] = self.window
            return {"object": "list", "data": [item]}

        @router.post("/v1/chat/completions")
        async def complete(request: Request) -> dict:
            payload = await request.json()
            self.seen.append(payload)
            return self.answer(payload)

        return router

    def answer(self, payload: dict) -> dict:
        messages = payload.get("messages") or []
        offered = {t["function"]["name"] for t in payload.get("tools") or []}
        results = [m for m in messages if m.get("role") == "tool"]
        served: list = []
        if results:
            rows = (json.loads(results[-1]["content"]).get("value") or {}).get("rows") or []
            text = "From the verb: " + ", ".join(f"{r['day'][:10]} rise {r['rise']}" for r in rows)
        elif self.mode == "calls" and VERB in offered:
            return _completion("", tool_calls=[{
                "id": "call_1", "type": "function",
                "function": {"name": VERB, "arguments": json.dumps({"since": "2026-09-01"})}}])
        elif self.mode == "serves":
            # A node's chat endpoint that runs its own verbs and reports them.
            served = [{"verb": "status_at", "args": {}}]
            text = "The newest reading is 0.4 at 00:00."
        else:
            text = "It is shut down, about 0 W."  # from memory: no tool was called
        return _completion(text, served=served)


def _completion(text: str, *, tool_calls: list | None = None, served: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": "node-model",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    if served:
        body["x_verbs_called"] = served
    return body


@pytest.fixture
def node(medallion, tmp_path, monkeypatch):
    """The node: the real data door and a model, behind the real gate, and one issued site key."""
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.data_platform import read_api
    from axiom.extensions.builtins.data_platform.services.read_mount import mount_spec
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.middleware import MiddlewareConfig
    from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry
    from axiom.governance import Decision, Verdict
    from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

    monkeypatch.setenv("DP1_RAG_DSN", medallion)
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.setattr(read_api, "holds_medallion", lambda: True)

    keys = tmp_path / "api-keys.json"
    token, record = mint_api_key(principal=f"@contributor:{SITE}", scopes=("*:read", "gateway:invoke"), site=SITE)
    append_api_key_record(keys, record)

    def permit(_envelope):
        return Verdict.from_decision(Decision.PERMIT, "test permits", f"t-{uuid.uuid4().hex}")

    model = _Model()
    registry = RouterRegistry()
    registry.register(mount_spec())
    registry.register(MountSpec(prefix="/v1", router=model.router(), extension="gateway"))
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys)),
        decide_fn=permit,
    )
    app = compose_app(registry=registry, include_builtins=False,
                      middleware=MiddlewareConfig(authz=hook), auto_authz=False)

    @app.middleware("http")
    async def on_the_node(request, call_next):
        flag = _ON_NODE.set(True)
        try:
            return await call_next(request)
        finally:
            _ON_NODE.reset(flag)

    client = TestClient(app)
    door: list[tuple[str, int]] = []
    return SimpleNamespace(client=client, token=token, model=model, door=door)


@pytest.fixture
def laptop(node, tmp_path, monkeypatch):
    """A laptop bound to the node, with nothing of its own to answer from."""
    import requests

    from axiom.extensions.builtins.chat import bound_model, bound_tools, where
    from axiom.extensions.builtins.data_platform import read_target
    from axiom.llm import gateway as gw

    state = tmp_path / "laptop"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    for name in ("DATABASE_URL", "AXIOM_DB_URL", where.SAMPLE_DATA_ENV, "AXIOM_CHAT_TURN_DEADLINE"):
        monkeypatch.delenv(name, raising=False)
    (state / read_target.STATE_FILENAME).write_text(json.dumps({
        "current": SITE, "bindings": {SITE: {"site": SITE, "face_url": FACE}}}), encoding="utf-8")

    # One process is both ends: the laptop does not hold the medallion; inside a request the node does.
    monkeypatch.setattr(read_target, "holds_medallion", lambda: _ON_NODE.get())
    monkeypatch.setattr(read_target, "_key", lambda face: node.token)  # the binding's key, from the vault

    def post(target, *, verb, params):
        path = "/data/" + ("submit" if verb in read_target.SUBMITS else "query")
        r = node.client.post(path, json={"verb": verb, "params": params},
                             headers={"authorization": f"Bearer {read_target._key(target.face_url)}"})
        node.door.append((verb, r.status_code))
        if r.status_code >= 400:
            raise read_target.ReadError(f"HTTP {r.status_code}: {r.text}")
        return r.json()

    monkeypatch.setattr(read_target, "_post", post)

    def _routed(url: str) -> str:
        parts = urlparse(url)
        assert f"{parts.scheme}://{parts.netloc}" == FACE, f"asked a host the binding does not name: {url}"
        return parts.path

    real_get = requests.get

    def get(url, *args, headers=None, **kw):
        if not str(url).startswith(FACE):
            return real_get(url, *args, headers=headers, **kw)
        return node.client.get(_routed(url), headers=headers or {})

    def model_post(_requests, url, payload, headers, timeout=60, **kw):
        r = node.client.post(_routed(url), json=payload, headers=headers)
        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code}: {r.text}")
        return r

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", model_post)
    monkeypatch.setattr(gw.Gateway, "_load_config", lambda self: None)
    monkeypatch.setattr("axiom.rag.store_factory.resolve_store_url",
                        lambda **kw: SimpleNamespace(url="", site=""))
    monkeypatch.setattr(bound_model, "_CHOSEN", {})
    monkeypatch.setattr(bound_model, "_WINDOWS", {})
    bound_tools.reset_cache()
    yield node
    bound_tools.reset_cache()


def _ask(question: str, capsys) -> dict:
    from axiom.extensions.builtins.chat.commands.cli import run_one_shot

    capsys.readouterr()
    assert run_one_shot(question, as_json=True) == 0
    return json.loads(capsys.readouterr().out)


def _rises(medallion) -> list[str]:
    rows = host.run_registered_verb(medallion, site=SITE, name=VERB, params={"since": "2026-09-01"})
    assert rows, "the promoted verb has rows on the node"
    return [str(r["rise"]) for r in rows]


# -- 1. a promoted verb is a tool, and the model's call runs it on the node ----------------------


def test_a_promoted_verb_is_called_through_the_node_and_its_rows_reach_the_answer(
    laptop, medallion, capsys
):
    out = _ask("rise of temp each day since september, what it say?", capsys)

    assert laptop.model.seen, out
    offered = {t["function"]["name"] for t in laptop.model.seen[0]["tools"]}
    assert VERB in offered, "the site's promoted verb is one of chat's tools"
    assert {"data__latest", "data__series"} <= offered, "so are the node's served data reads"
    assert VERB in out["verbs_called"], out
    assert ("data.kit_verb", 200) in laptop.door, "the verb ran through the node's gate"
    assert any(rise in out["answer"] for rise in _rises(medallion)), out["answer"]
    assert out["measured"] is True and "not a measurement" not in out["footer"], out


def test_the_gate_is_real_a_request_without_the_key_is_refused(laptop):
    r = laptop.client.post("/data/query", json={"verb": "data.kit_verbs", "params": {}})
    assert r.status_code in (401, 403), r.text


def test_a_question_naming_the_verb_runs_it_even_when_the_model_ignores_tools(laptop, medallion, capsys):
    """The live node's model runs its own verbs and ignores a client's tools; the verb a question
    names still runs, and the model answers from its rows."""
    laptop.model.mode = "ignores"
    out = _ask(f"what does {VERB} say for the last day?", capsys)

    assert out["verbs_called"] == [VERB], out
    assert ("data.kit_verb", 200) in laptop.door
    assert any(rise in out["answer"] for rise in _rises(medallion)), out["answer"]
    assert "don't have" not in out["answer"].lower()


# -- 2. a data question answered without a verb call is not a measurement ------------------------


def test_a_data_answer_with_no_data_tool_called_is_marked_and_the_footer_says_so(laptop, capsys):
    laptop.model.mode = "ignores"
    out = _ask("what power is now, latest one please", capsys)

    assert out["verbs_called"] == [], out
    assert out["answer"].startswith("(not a measurement: no data tool was called)"), out["answer"]
    assert "not a measurement: no data tool was called" in out["footer"], out["footer"]
    assert out["measured"] is False
    nudges = [m for p in laptop.model.seen for m in p["messages"]
              if m.get("role") == "user" and "calling one of the data tools" in str(m.get("content"))]
    assert nudges, "the model was asked once to call a data tool before the answer was marked"
    assert "DATA ON chat-site" in laptop.model.seen[0]["messages"][0]["content"] or any(
        "DATA ON chat-site" in str(m.get("content")) for m in laptop.model.seen[0]["messages"]
    ), "the model was told how measurements are read here"


def test_verbs_the_node_ran_itself_count_and_are_reported(laptop, capsys):
    laptop.model.mode = "serves"
    out = _ask("what power is now, latest one please", capsys)

    assert out["verbs_called"] == ["node:status_at"], out
    assert out["measured"] is True and not out["answer"].startswith("(not a measurement"), out


def test_a_question_that_is_not_about_data_is_not_marked(laptop, capsys):
    laptop.model.mode = "ignores"
    out = _ask("hello there, who are you?", capsys)
    assert out["measured"] is None and "not a measurement" not in out["answer"], out


# -- 3. the tools are chosen to fit the model's context, and it says so ---------------------------


def test_tools_that_do_not_fit_the_window_are_chosen_by_the_question_and_said(laptop, capsys):
    laptop.model.mode = "ignores"
    _ask("hello there, who are you?", capsys)
    first = laptop.model.seen[-1]
    every = [t["function"]["name"] for t in first["tools"]]
    prompt_tokens = len(json.dumps(first["messages"])) // 4
    tool_tokens = len(json.dumps(first["tools"])) // 4
    laptop.model.window = prompt_tokens + 1024 + tool_tokens // 3

    from axiom.extensions.builtins.chat import bound_model, bound_tools

    bound_model._CHOSEN.clear()
    bound_tools.reset_cache()
    out = _ask(f"what does {VERB} say for the last day?", capsys)

    offered = [t["function"]["name"] for t in laptop.model.seen[-1]["tools"]]
    assert 0 < len(offered) < len(every), (len(offered), len(every))
    assert offered[0] == VERB, "the tool the question names comes first"
    assert re.search(rf"offered {len(offered)} of {len(every)} tools, chosen by the question", out["footer"]), out
    assert out["tool_selection"], out
