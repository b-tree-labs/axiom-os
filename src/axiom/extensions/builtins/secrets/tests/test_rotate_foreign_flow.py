# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.rotate <name>`` — the KEEP-executed foreign rotation flow.

Owner directive on #667: read current value from the store → RotationProvider
rotates at the issuer → write new value → probe-verify → report scrub
candidates. The whole step runs through ``guarded_act`` (consequential) so
every rotation — including refusals and failures — lands in the #665 action
ledger with metadata/handles ONLY (never values).

HTTP is mocked end-to-end (httpx.MockTransport); the value store is an
in-memory fake. Nothing here touches a keychain or a live issuer.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from axiom.extensions.builtins.secrets import skills as secrets_skills
from axiom.extensions.builtins.secrets.foreign.store import (
    ForeignCredentialStore,
)
from axiom.infra.skills import SkillContext

from .test_foreign_store import InMemoryValueStore

BASE = "https://gitlab.example.org"


@pytest.fixture(autouse=True)
def _jsonl_ledger(monkeypatch, tmp_path):
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    action_ledger._reset_backend_cache()
    yield
    action_ledger._reset_backend_cache()


@pytest.fixture
def value_store():
    return InMemoryValueStore()


@pytest.fixture
def store(tmp_path, value_store):
    s = ForeignCredentialStore(tmp_path, value_store=value_store)
    s.set(
        "hpc-gitlab-pat", b"glpat-OLD",
        provider="gitlab-pat", issuer_url=BASE, expires_at="2026-07-25",
    )
    return s


def _ctx(tmp_path, prompt=None):
    return SkillContext(
        registry=secrets_skills.bind_default(),
        state_dir=tmp_path,
        logger=logging.getLogger("test.rotate"),
        user_prompt=prompt,
    )


def _gitlab_http(rotate_status=200):
    state = {"current": "glpat-OLD"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/personal_access_tokens/self/rotate"):
            if rotate_status != 200:
                return httpx.Response(rotate_status, json={"message": "err"})
            if request.headers.get("PRIVATE-TOKEN") != state["current"]:
                return httpx.Response(401, json={"message": "401"})
            state["current"] = "glpat-NEW"
            return httpx.Response(200, json={
                "id": 7, "token": "glpat-NEW",
                "expires_at": str(request.url.params.get("expires_at")),
            })
        if request.url.path.endswith("/user"):
            if request.headers.get("PRIVATE-TOKEN") == state["current"]:
                return httpx.Response(200, json={"username": "ben"})
            return httpx.Response(401, json={"message": "401"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _rotate(tmp_path, store, http, **extra):
    ctx = _ctx(tmp_path)
    params = {
        "ref": "hpc-gitlab-pat", "_store": store, "_http": http,
        "expires_at": "2026-07-30", **extra,
    }
    return ctx.registry.invoke("secrets.rotate", params, ctx)


class TestForeignRotationFlow:
    def test_happy_path_rotates_probes_and_journals(
        self, tmp_path, store, value_store
    ):
        from axiom.policy.action_ledger import search_actions

        result = _rotate(tmp_path, store, _gitlab_http())
        assert result.ok, result.errors
        # New value written through the store...
        assert value_store.values["hpc-gitlab-pat"] == b"glpat-NEW"
        # ...metadata updated...
        meta = store.metadata("hpc-gitlab-pat")
        assert meta["expires_at"] == "2026-07-30"
        assert meta["last_rotated_at"]
        # ...probe verified...
        assert result.value["probe_ok"] is True
        # ...and journaled to the #665 ledger with a handle we can return.
        found = search_actions(
            op_class="secrets.rotate", state_dir=tmp_path,
        )
        assert found["count"] >= 1
        assert found["actions"][0]["outcome"] == "proceeded"
        assert result.value["action_id"]

    def test_no_secret_values_in_result_or_ledger(self, tmp_path, store):
        from axiom.policy.action_ledger import search_actions

        result = _rotate(tmp_path, store, _gitlab_http())
        blob = json.dumps({
            "value": result.value,
            "actions": result.actions_taken,
            "errors": result.errors,
        })
        assert "glpat-OLD" not in blob and "glpat-NEW" not in blob
        ledger_blob = json.dumps(
            search_actions(op_class="secrets.rotate", state_dir=tmp_path)
        )
        assert "glpat-OLD" not in ledger_blob
        assert "glpat-NEW" not in ledger_blob

    def test_scrub_candidates_reported_not_edited(self, tmp_path, store):
        result = _rotate(tmp_path, store, _gitlab_http())
        scrub = result.value["scrub_candidates"]
        assert scrub, "known plaintext location types must be listed"
        types = {c["location_type"] for c in scrub}
        assert "harness-settings-allow-rules" in types
        assert "memory-fragments" in types
        for c in scrub:
            assert c["hint"]  # remediation guidance, no auto-editing

    def test_issuer_failure_journals_failed_and_keeps_old_value(
        self, tmp_path, store, value_store
    ):
        from axiom.policy.action_ledger import search_actions

        result = _rotate(tmp_path, store, _gitlab_http(rotate_status=401))
        assert not result.ok
        assert value_store.values["hpc-gitlab-pat"] == b"glpat-OLD"
        found = search_actions(op_class="secrets.rotate", state_dir=tmp_path)
        assert found["count"] >= 1
        assert found["actions"][0]["outcome"] == "failed"

    def test_guard_pause_refuses_rotation(self, tmp_path, store, value_store):
        from axiom.policy.agent_action_guard import pause_action

        pause_action(
            state_dir=tmp_path, agent="keep", scope="secrets.rotate",
            by="tester", reason="unit test",
        )
        result = _rotate(tmp_path, store, _gitlab_http())
        assert not result.ok
        assert "paused" in " ".join(result.errors)
        assert value_store.values["hpc-gitlab-pat"] == b"glpat-OLD"

    def test_unknown_name_fails_cleanly(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.rotate", {
            "ref": "not-a-known-name", "_store": store,
        }, ctx)
        assert not result.ok

    def test_guided_fallback_used_when_no_api_provider(self, tmp_path):
        vs = InMemoryValueStore()
        s = ForeignCredentialStore(tmp_path, value_store=vs)
        s.set("openai-key", b"sk-OLD")  # no provider metadata → guided
        ctx = _ctx(tmp_path, prompt=lambda _m: "sk-NEW")
        result = ctx.registry.invoke("secrets.rotate", {
            "ref": "openai-key", "_store": s,
        }, ctx)
        assert result.ok, result.errors
        assert vs.values["openai-key"] == b"sk-NEW"

    def test_guided_fallback_headless_fails_closed(self, tmp_path):
        vs = InMemoryValueStore()
        s = ForeignCredentialStore(tmp_path, value_store=vs)
        s.set("openai-key", b"sk-OLD")
        ctx = _ctx(tmp_path, prompt=None)
        result = ctx.registry.invoke("secrets.rotate", {
            "ref": "openai-key", "_store": s,
        }, ctx)
        assert not result.ok
        assert vs.values["openai-key"] == b"sk-OLD"

    def test_existing_ref_based_rotation_still_works(self, tmp_path):
        # Regression: `secrets.rotate openbao://...` (ref form) keeps its
        # pre-#667 behavior — the name-based branch only engages when the
        # argument has no scheme.
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.rotate", {
            "ref": "env://SOME_VAR", "strategy": "provider-native",
        }, ctx)
        # env provider refuses rotation — the point is it routed down the
        # ref path (provider error), not "unknown foreign credential".
        assert not result.ok
        assert "foreign" not in " ".join(result.errors).lower()
