# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Settings on every surface, with the two things a browser or an agent must never do.

Settings had a CLI and nothing else. These pin the skills every other surface
calls (MCP, the web panel) and the two refusals that make that safe: a safety
switch is changed only at the node's own CLI, and a credential-like value is
never handed back.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.settings import store
from axiom.extensions.builtins.settings.skills import (
    bind,
    get_setting,
    is_secretish,
    list_settings,
    set_setting,
)
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_get_global_settings_path", lambda: tmp_path / "global.toml")
    monkeypatch.setattr(store, "_PROJECT_SETTINGS_PATH", tmp_path / "project.toml")
    yield


def _ctx():
    reg = SkillRegistry()
    bind(reg)
    return SkillContext(registry=reg, state_dir=None, logger=logging.getLogger("t"))


def test_one_parser_for_every_surface():
    assert store.coerce("interface.stream", "false") is False
    assert store.coerce("raci.trust", "3") == 3
    assert store.coerce("routing.prefer_provider", "a, b") == ["a", "b"]
    assert store.coerce("x.y", "[a, 'b']") == ["a", "b"]
    assert store.coerce("x.y", True) is True


def test_list_reports_defaults_and_what_changed():
    set_setting({"key": "interface.stream", "value": "false"}, _ctx())
    rows = {r["key"]: r for r in list_settings({"prefix": "interface."}, _ctx()).value["settings"]}
    assert rows["interface.stream"] == {
        "key": "interface.stream",
        "value": False,
        "default": True,
        "changed": True,
        "safety_switch": False,
    }
    assert rows["interface.theme"]["changed"] is False


@pytest.mark.parametrize("key", ["autonomy.enabled", "raci.trust"])
def test_a_safety_switch_is_refused_and_the_cli_command_is_named(key):
    r = set_setting({"key": key, "value": "true"}, _ctx())
    assert not r.ok
    assert f"axi settings set {key}" in r.errors[0]
    assert store.SettingsStore().get(key) == store._DEFAULTS[key]


def test_a_credential_like_value_is_never_handed_back():
    store.SettingsStore().set("rag.database_url", "postgresql://u:hunter2@db/x", scope="global")
    got = get_setting({"key": "rag.database_url"}, _ctx()).value
    assert got["value"] == "(set)"
    listed = list_settings({"prefix": "rag."}, _ctx()).value["settings"]
    assert all("hunter2" not in str(r) for r in listed)
    assert (
        is_secretish("rag.database_url")
        and is_secretish("x.api_token")
        and not is_secretish("interface.stream")
    )


def test_an_unknown_key_says_where_to_look():
    r = get_setting({"key": "nope.nothing"}, _ctx())
    assert not r.ok and "axi settings" in r.errors[0]


def test_set_requires_a_value_and_a_real_scope():
    assert not set_setting({"key": "interface.stream"}, _ctx()).ok
    assert not set_setting(
        {"key": "interface.stream", "value": "true", "scope": "everywhere"}, _ctx()
    ).ok


class TestTheWebFace:
    def _client(self, monkeypatch, roles):
        from fastapi import APIRouter, FastAPI
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.settings import api

        monkeypatch.setattr(
            api,
            "_claims",
            lambda request: {"sub": "u", "roles": roles} if roles is not None else {},
        )
        router = APIRouter()
        api.register_routes(router)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        return TestClient(app)

    def test_it_shows_only_the_allowlist(self, monkeypatch):
        store.SettingsStore().set("rag.database_url", "postgresql://u:hunter2@db/x", scope="global")
        body = self._client(monkeypatch, ["viewer"]).get("/api/v1/settings").json()
        keys = [r["key"] for r in body["settings"]]
        assert "rag.database_url" not in keys and "hunter2" not in str(body)
        assert "autonomy.enabled" in keys
        assert body["can_edit"] is False
        assert all(not r["editable"] for r in body["settings"])

    def test_an_operator_may_change_an_editable_key(self, monkeypatch):
        c = self._client(monkeypatch, ["operator"])
        r = c.patch("/api/v1/settings", json={"key": "interface.stream", "value": False})
        assert r.status_code == 200, r.text
        assert store.SettingsStore().get("interface.stream") is False

    def test_a_viewer_may_not(self, monkeypatch):
        r = self._client(monkeypatch, ["viewer"]).patch(
            "/api/v1/settings", json={"key": "interface.stream", "value": False}
        )
        assert r.status_code == 403

    def test_not_even_an_operator_turns_autonomy_on_from_a_browser(self, monkeypatch):
        r = self._client(monkeypatch, ["owner"]).patch(
            "/api/v1/settings", json={"key": "autonomy.enabled", "value": True}
        )
        assert r.status_code == 403
        assert "axi settings set autonomy.enabled" in r.json()["detail"]
        assert store.SettingsStore().get("autonomy.enabled") is False

    def test_signed_out_is_refused(self, monkeypatch):
        assert self._client(monkeypatch, None).get("/api/v1/settings").status_code == 401


class TestTheCommandLine:
    def _run(self, monkeypatch, *argv):
        from axiom.extensions.builtins.settings import cli

        calls = []
        real = __import__("axiom.infra.skill_dispatch", fromlist=["invoke_capability"]).invoke_capability

        def spy(registry, capability, params, ctx, **kw):
            calls.append(capability)
            return real(registry, capability, params, ctx, **kw)

        monkeypatch.setattr("axiom.infra.skill_dispatch.invoke_capability", spy)
        monkeypatch.setattr(cli, "reconcile_autonomy_registration", lambda enabled: None)
        monkeypatch.setattr("sys.argv", ["axi-settings", *argv])
        cli.main()
        return calls

    def test_an_ordinary_key_goes_through_the_dispatch_chokepoint(self, monkeypatch):
        assert self._run(monkeypatch, "--global", "set", "interface.stream", "false") == ["settings.set"]
        assert store.SettingsStore().get("interface.stream") is False

    def test_a_safety_switch_is_still_settable_here_and_only_here(self, monkeypatch):
        assert self._run(monkeypatch, "--global", "set", "raci.trust", "3") == []
        assert store.SettingsStore().get("raci.trust") == 3
