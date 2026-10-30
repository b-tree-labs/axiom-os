# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Chat on a laptop bound to a site node asks that node's model.

Found in a colleague's first hour: ``chat --ask`` on a bound laptop used only the laptop's own
providers. With none configured it printed "LLM extraction unavailable"; with a stray server on
localhost it answered from a model that knew nothing about the site, and the answer read as real.

One process plays both installs. The node is the composed serving app, in-process, with a fake
OpenAI-compatible ``/v1`` mounted behind the real authz hook and a key minted the way the node mints
one. The laptop holds no medallion and no provider; its ``site-bindings.json`` names the node and a
``token_ref`` the vault resolves to that key. The laptop's HTTP goes to the node's app.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from axiom.extensions.builtins.chat import bound_model, where
from axiom.extensions.builtins.chat.commands import cli as chat_cli
from axiom.extensions.builtins.data_platform import read_target
from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry
from axiom.governance import Decision, Verdict
from axiom.llm.gateway import Gateway, LLMProvider
from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

SITE = "alpha-site"
FACE = "https://node.example.org"
NODE_MODEL = "site-assistant-7b"
NODE_ANSWER = "The node's model answered this, from the site's own context."
REF = "openbao://kv/data/alpha-reader"


# --------------------------------------------------------------------------- the node


def _fake_v1(behaviour: dict) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    def models() -> dict:
        return {"object": "list", "data": [{"id": NODE_MODEL, "object": "model"}]}

    @router.post("/v1/chat/completions")
    async def completions(request: Request) -> JSONResponse:
        behaviour["asked"].append(await request.json())
        if behaviour.get("refuse"):
            return JSONResponse(status_code=403, content={"error": {"message": "this key may not chat"}})
        return JSONResponse(content={
            "choices": [{"message": {"role": "assistant", "content": NODE_ANSWER}, "finish_reason": "stop"}],
            "model": NODE_MODEL,
            "usage": {"prompt_tokens": 3, "completion_tokens": 9},
        })

    return router


@pytest.fixture
def node(tmp_path, monkeypatch):
    for name in ("AXIOM_HOME_SITES", "AXIOM_FED_DEV_ACCEPT", "DATABASE_URL", "AXIOM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    keys_file = tmp_path / "node-keys.json"
    token, record = mint_api_key(principal="@reader", scopes=["*:read", "gateway:invoke"], name="laptop", site=SITE)
    append_api_key_record(keys_file, record)

    behaviour: dict = {"asked": []}
    registry = RouterRegistry()
    registry.register(MountSpec(prefix="/v1", router=_fake_v1(behaviour), extension="gateway"))
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys_file)),
        decide_fn=lambda _env: Verdict.from_decision(Decision.PERMIT, "test permits", "t-1"),
    )
    app = compose_app(
        registry=registry, include_builtins=False, middleware=MiddlewareConfig(authz=hook), auto_authz=False
    )
    return SimpleNamespace(client=TestClient(app), key=token, behaviour=behaviour)


# --------------------------------------------------------------------------- the laptop


@pytest.fixture
def laptop(node, tmp_path, monkeypatch):
    state = tmp_path / "laptop-state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    (state / read_target.STATE_FILENAME).write_text(json.dumps({
        "current": SITE,
        "bindings": {SITE: {"site": SITE, "face_url": FACE, "token_ref": REF}},
    }), encoding="utf-8")
    monkeypatch.setattr(read_target, "holds_medallion", lambda: False)

    # The vault: the binding's token_ref resolves to the key the node issued, and nothing else does.
    import axiom.extensions.builtins.secrets as secrets

    def resolve(ref):
        if str(getattr(ref, "path", "")).endswith("alpha-reader"):
            return SimpleNamespace(value=node.key)
        raise KeyError(ref)

    monkeypatch.setattr(secrets, "resolve", resolve)

    import requests

    def _strip(url: str) -> str:
        assert url.startswith(FACE), f"the laptop called {url}, not its node"
        return url[len(FACE):]

    def get(url, headers=None, timeout=None, **_):
        return node.client.get(_strip(url), headers=headers or {})

    def post(url, json=None, headers=None, timeout=None, **_):
        return node.client.post(_strip(url), json=json, headers=headers or {})

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    return state


def _gateway(*providers: LLMProvider, discovered: tuple[str, ...] = ()) -> Gateway:
    gw = Gateway.__new__(Gateway)
    gw.providers = list(providers)
    gw._provider_override = None
    gw._model_override = None
    gw._ec_audit_enabled = False
    gw._gateway_config = {}
    gw.auto_discovered = set(discovered)
    gw.last_selected = None
    return gw


def _stray_local() -> LLMProvider:
    return LLMProvider(name="axiom-local", uid="axiom-local-x", endpoint="http://localhost:8080",
                       model="tiny-local", api_key_env="", priority=50, routing_tier="public")


# --------------------------------------------------------------------------- the behaviour


def test_a_bound_laptop_with_no_provider_asks_the_nodes_model(laptop, node):
    gw = _gateway()
    applied = bound_model.apply_bound_model(gw)

    assert applied is not None and applied.model == NODE_MODEL and applied.probe_error == ""
    provider = gw.preview_provider("any")
    assert provider.endpoint == f"{FACE}/v1" and provider.model == NODE_MODEL
    assert provider.api_key == node.key  # the binding's own key, nothing host-derived

    response = gw.complete_with_tools([{"role": "user", "content": "max power yesterday?"}], tools=None)
    assert NODE_ANSWER in response.text
    assert node.behaviour["asked"], "the node was never asked"


def test_a_stray_local_server_does_not_answer_for_the_site(laptop, node):
    gw = _gateway(_stray_local(), discovered=("axiom-local",))
    bound_model.apply_bound_model(gw)
    assert [p.name for p in gw.providers] == [f"{SITE} node"]


def test_an_explicitly_configured_provider_still_wins(laptop, node, monkeypatch):
    monkeypatch.setenv("MY_PROVIDER_KEY", "k")
    mine = LLMProvider(name="mine", uid="mine-1", endpoint="https://llm.example.com/v1", model="chosen",
                       api_key_env="MY_PROVIDER_KEY", priority=1)
    gw = _gateway(mine)
    assert bound_model.apply_bound_model(gw) is None
    assert gw.preview_provider("any").name == "mine"
    assert not node.behaviour["asked"]


def test_local_and_provider_flags_keep_this_machines_models(laptop, node):
    assert bound_model.apply_bound_model(_gateway(), local=True) is None
    gw = _gateway()
    gw._provider_override = "something"
    assert bound_model.apply_bound_model(gw) is None


def test_the_banner_names_the_site_the_model_and_the_node(laptop, node):
    bound_model.apply_bound_model(_gateway())
    assert where.banner_line() == f"BOUND ({SITE}): model {NODE_MODEL} on node.example.org"


def test_ask_prints_the_banner_the_answer_and_the_footer(laptop, node, monkeypatch, capsys):
    monkeypatch.setattr("axiom.infra.gateway.Gateway", lambda: _gateway())

    assert chat_cli.run_one_shot("max power yesterday?") == 0
    out, err = capsys.readouterr()
    assert NODE_ANSWER in out
    assert f"BOUND ({SITE}): model {NODE_MODEL} on node.example.org" in err
    footer = [line for line in err.splitlines() if line.startswith("↳ ")][-1]
    assert footer.startswith("↳ data: alpha-site via node.example.org · documents: ")
    assert f"model: {NODE_MODEL} (REMOTE node.example.org)" in footer
    # This node answers a data question without running a verb, or saying it ran one, so the answer
    # is not a measurement and the footer says so (test_a_bound_chat_answers_with_the_nodes_tools).
    assert footer.endswith("not a measurement: no data tool was called"), footer


def test_a_refusal_from_the_node_is_said_plainly_never_answered_locally(laptop, node, monkeypatch, capsys):
    node.behaviour["refuse"] = True
    monkeypatch.setattr("axiom.infra.gateway.Gateway", lambda: _gateway(_stray_local(), discovered=("axiom-local",)))

    code = chat_cli.run_one_shot("max power yesterday?")
    out, _err = capsys.readouterr()
    assert code == 1
    assert out.strip() == (
        f"No answer: {SITE} node (node.example.org) refused the request "
        "(HTTP 403: this key may not chat). Nothing was answered in its place."
    )
    assert "tiny-local" not in out


def test_a_timeout_from_the_node_is_said_plainly(laptop, node, monkeypatch, capsys):
    import requests

    from axiom.llm import gateway as gateway_module

    def timed_out(url, json=None, headers=None, timeout=None, **_):
        raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=180)")

    monkeypatch.setattr(requests, "post", timed_out)
    monkeypatch.setattr(gateway_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr("axiom.infra.gateway.Gateway", lambda: _gateway(_stray_local(), discovered=("axiom-local",)))

    assert chat_cli.run_one_shot("max power yesterday?") == 1
    out, _err = capsys.readouterr()
    assert out.strip() == (
        f"No answer: {SITE} node (node.example.org) did not answer in time. Nothing was answered in its place."
    )
