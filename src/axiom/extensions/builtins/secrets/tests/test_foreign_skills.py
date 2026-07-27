# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.set`` / ``list`` / ``rm`` / ``get`` / ``audit`` skills.

ADR-056 skill layer for the foreign-credential store. Values only ever
reach a SkillResult on an explicit ``get --reveal``; every other verb is
names + metadata. Tests inject an in-memory value store — no keychain.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.secrets import skills as secrets_skills
from axiom.extensions.builtins.secrets.foreign.store import (
    ForeignCredentialStore,
)
from axiom.infra.skills import SkillContext

from .test_foreign_store import InMemoryValueStore


@pytest.fixture
def value_store():
    return InMemoryValueStore()


@pytest.fixture
def store(tmp_path, value_store):
    return ForeignCredentialStore(tmp_path, value_store=value_store)


def _ctx(tmp_path, prompt=None, monkeypatch=None):
    return SkillContext(
        registry=secrets_skills.bind_default(),
        state_dir=tmp_path,
        logger=logging.getLogger("test.secrets"),
        user_prompt=prompt,
    )


@pytest.fixture(autouse=True)
def _jsonl_ledger(monkeypatch, tmp_path):
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    action_ledger._reset_backend_cache()
    yield
    action_ledger._reset_backend_cache()


class TestSet:
    def test_set_stores_and_returns_metadata_only(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.set", {
            "name": "hpc-gitlab-pat", "value": "glpat-SENTINEL",
            "provider": "gitlab-pat",
            "issuer_url": "https://gitlab.example.org",
            "expires_at": "2026-08-01",
            "_store": store,
        }, ctx)
        assert result.ok, result.errors
        assert "glpat-SENTINEL" not in json.dumps(result.value)
        assert "glpat-SENTINEL" not in " ".join(result.actions_taken)
        with store.get("hpc-gitlab-pat") as s:
            assert s.value == b"glpat-SENTINEL"

    def test_set_prompts_interactively_when_no_value(self, tmp_path, store):
        ctx = _ctx(tmp_path, prompt=lambda _msg: "pasted-value")
        result = ctx.registry.invoke("secrets.set", {
            "name": "n", "_store": store,
        }, ctx)
        assert result.ok, result.errors
        with store.get("n") as s:
            assert s.value == b"pasted-value"

    def test_set_headless_without_value_fails_closed(self, tmp_path, store):
        ctx = _ctx(tmp_path, prompt=None)
        result = ctx.registry.invoke("secrets.set", {
            "name": "n", "_store": store,
        }, ctx)
        assert not result.ok

    def test_set_requires_name(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.set", {
            "value": "v", "_store": store,
        }, ctx)
        assert not result.ok


class TestListRmGet:
    def test_list_names_and_metadata_never_values(self, tmp_path, store):
        store.set("a", b"VALSENTA", expires_at="2026-09-01")
        store.set("b", b"VALSENTB")
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.list", {"_store": store}, ctx)
        assert result.ok
        names = [i["name"] for i in result.value["items"]]
        assert names == ["a", "b"]
        blob = json.dumps(result.value)
        assert "VALSENTA" not in blob and "VALSENTB" not in blob

    def test_rm_with_yes_removes_and_journals(self, tmp_path, store):
        from axiom.policy.action_ledger import search_actions

        store.set("gone", b"v")
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.rm", {
            "name": "gone", "yes": True, "_store": store,
        }, ctx)
        assert result.ok, result.errors
        assert not store.exists("gone")
        found = search_actions(
            op_class="secrets.rm", text="gone", state_dir=tmp_path,
        )
        assert found["count"] >= 1
        assert result.value.get("action_id")

    def test_rm_headless_without_yes_fails_closed(self, tmp_path, store):
        store.set("keep", b"v")
        ctx = _ctx(tmp_path, prompt=None)
        result = ctx.registry.invoke("secrets.rm", {
            "name": "keep", "_store": store,
        }, ctx)
        assert not result.ok
        assert store.exists("keep")

    def test_rm_interactive_confirm_requires_exact_name(self, tmp_path, store):
        store.set("keep", b"v")
        ctx = _ctx(tmp_path, prompt=lambda _m: "wrong-name")
        result = ctx.registry.invoke("secrets.rm", {
            "name": "keep", "_store": store,
        }, ctx)
        assert not result.ok
        assert store.exists("keep")

    def test_get_default_returns_metadata_not_value(self, tmp_path, store):
        store.set("n", b"VALSENTINEL")
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.get", {
            "name": "n", "_store": store,
        }, ctx)
        assert result.ok
        assert "VALSENTINEL" not in json.dumps(result.value)

    def test_get_reveal_returns_value_with_warning(self, tmp_path, store):
        store.set("n", b"VALSENTINEL")
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.get", {
            "name": "n", "reveal": True, "_store": store,
        }, ctx)
        assert result.ok
        assert result.value["value"] == "VALSENTINEL"
        assert result.value["warning"]

    def test_get_missing_fails(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.get", {
            "name": "nope", "_store": store,
        }, ctx)
        assert not result.ok


class TestAudit:
    NOW = "2026-07-23T12:00:00+00:00"

    def _audit(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        return ctx.registry.invoke("secrets.audit", {
            "_store": store, "_now": self.NOW,
        }, ctx)

    def test_expired_expiring_and_ok_findings(self, tmp_path, store):
        store.set("dead", b"v", expires_at="2026-07-01")
        store.set("soon", b"v", expires_at="2026-07-30")
        store.set("fine", b"v", expires_at="2027-01-01")
        store.set("unknown", b"v")
        result = self._audit(tmp_path, store)
        assert result.ok
        by_name = {f["name"]: f for f in result.value["findings"]}
        assert by_name["dead"]["level"] == "expired"
        assert by_name["soon"]["level"] == "expiring"
        assert by_name["fine"]["level"] == "ok"
        assert by_name["unknown"]["level"] == "no_expiry"

    def test_findings_carry_no_values(self, tmp_path, store):
        store.set("dead", b"AUDITSENTINEL", expires_at="2026-07-01")
        result = self._audit(tmp_path, store)
        assert "AUDITSENTINEL" not in json.dumps(result.value)

    def test_not_ok_exit_when_expired_present(self, tmp_path, store):
        store.set("dead", b"v", expires_at="2026-07-01")
        result = self._audit(tmp_path, store)
        # audit is a report, not a failure — ok stays True, counts flag it
        assert result.ok
        assert result.value["counts"]["expired"] == 1
