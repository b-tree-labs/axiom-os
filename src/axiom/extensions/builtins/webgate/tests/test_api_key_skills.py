# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``gate`` API-key skills (ADR-056).

``gate.issue`` / ``gate.revoke`` / ``gate.list api-keys`` administer bearer
keys for NON-human API principals. The end-to-end property asserted here: a
key issued by the skill resolves through the same store the composed app's
authz hook reads — and a revoked key stops resolving immediately.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.webgate import skills as gate_skills
from axiom.infra.skills import SkillContext, SkillRegistry
from axiom.webauth.api_keys import JsonFileApiKeyStore


@pytest.fixture
def ctx(tmp_path: Path) -> SkillContext:
    reg = SkillRegistry()
    gate_skills.bind(reg)
    return SkillContext(
        registry=reg, state_dir=tmp_path, logger=logging.getLogger("test.gate")
    )


def _keys(tmp_path: Path) -> str:
    return str(tmp_path / "gate-api-keys.json")


def _issue(ctx, tmp_path, *, principal="@svc:org", scopes=("llm",), **extra):
    return ctx.registry.invoke("gate.issue", {
        "resource": "api-key", "principal": principal,
        "scope": list(scopes), "keys_file": _keys(tmp_path), **extra,
    }, ctx)


# ---------- issue -----------------------------------------------------------


def test_issue_prints_token_once_and_stores_hash(ctx, tmp_path):
    r = _issue(ctx, tmp_path, principal="@svc:org", scopes=("llm", "rag:read"),
               name="chat backend")
    assert r.ok, r.errors
    token = r.value["token"]
    assert token.startswith("axk_")
    assert r.value["principal"] == "@svc:org"
    assert r.value["scopes"] == ["llm", "rag:read"]
    assert r.value["key_id"]  # auto-generated, never caller-supplied
    # the plaintext token lives only in the result, never on disk
    assert token not in Path(_keys(tmp_path)).read_text()
    # and it resolves through the store the authz hook uses
    ident = JsonFileApiKeyStore(_keys(tmp_path)).resolve(token)
    assert ident is not None and ident.principal == "@svc:org"


def test_issue_requires_principal_and_scopes(ctx, tmp_path):
    r = ctx.registry.invoke("gate.issue", {
        "resource": "api-key", "scope": ["llm"],
        "keys_file": _keys(tmp_path)}, ctx)
    assert not r.ok and "principal" in r.errors[0]

    r = ctx.registry.invoke("gate.issue", {
        "resource": "api-key", "principal": "@svc:org",
        "keys_file": _keys(tmp_path)}, ctx)
    assert not r.ok and "scope" in r.errors[0]


def test_issue_rejects_bad_principal_handle(ctx, tmp_path):
    r = _issue(ctx, tmp_path, principal="svc:org")
    assert not r.ok
    r = _issue(ctx, tmp_path, principal="@a@b:c")
    assert not r.ok


def test_issue_rejects_malformed_scope(ctx, tmp_path):
    r = _issue(ctx, tmp_path, scopes=("llm:destroy",))
    assert not r.ok and "scope" in r.errors[0]


def test_issue_rejects_unknown_resource(ctx, tmp_path):
    r = ctx.registry.invoke("gate.issue", {
        "resource": "unicorn", "principal": "@svc:org", "scope": ["llm"],
        "keys_file": _keys(tmp_path)}, ctx)
    assert not r.ok


def test_issue_without_keys_file_errors(ctx, monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_API_KEYS_FILE", raising=False)
    r = ctx.registry.invoke("gate.issue", {
        "resource": "api-key", "principal": "@svc:org", "scope": ["llm"]}, ctx)
    assert not r.ok and "keys file" in r.errors[0]


# ---------- revoke ----------------------------------------------------------


def test_revoke_is_immediate(ctx, tmp_path):
    issued = _issue(ctx, tmp_path)
    token, key_id = issued.value["token"], issued.value["key_id"]
    store = JsonFileApiKeyStore(_keys(tmp_path))
    assert store.resolve(token) is not None

    r = ctx.registry.invoke("gate.revoke", {
        "resource": "api-key", "key_id": key_id,
        "keys_file": _keys(tmp_path)}, ctx)
    assert r.ok, r.errors
    store.reload()
    assert store.resolve(token) is None


def test_revoke_unknown_key_errors(ctx, tmp_path):
    _issue(ctx, tmp_path)
    r = ctx.registry.invoke("gate.revoke", {
        "resource": "api-key", "key_id": "nope",
        "keys_file": _keys(tmp_path)}, ctx)
    assert not r.ok and "no such key" in r.errors[0]


# ---------- list ------------------------------------------------------------


def test_list_api_keys_hides_hashes_and_marks_revoked(ctx, tmp_path):
    a = _issue(ctx, tmp_path, principal="@svc-a:org", name="a")
    _issue(ctx, tmp_path, principal="@svc-b:org", scopes=("rag:read",))
    ctx.registry.invoke("gate.revoke", {
        "resource": "api-key", "key_id": a.value["key_id"],
        "keys_file": _keys(tmp_path)}, ctx)

    r = ctx.registry.invoke("gate.list", {
        "resource": "api-keys", "keys_file": _keys(tmp_path)}, ctx)
    assert r.ok, r.errors
    items = r.value["items"]
    assert len(items) == 2
    by_principal = {i["principal"]: i for i in items}
    assert by_principal["@svc-a:org"]["revoked_at"]
    assert not by_principal["@svc-b:org"]["revoked_at"]
    assert by_principal["@svc-b:org"]["scopes"] == ["rag:read"]
    for i in items:
        assert "secret_hash" not in i and "token" not in i


def test_list_api_keys_empty_when_uncreated(ctx, tmp_path):
    r = ctx.registry.invoke("gate.list", {
        "resource": "api-keys", "keys_file": _keys(tmp_path)}, ctx)
    assert r.ok and r.value["items"] == []


def test_list_defaults_to_accounts(ctx, tmp_path):
    # the pre-existing account listing is the default resource — unchanged
    r = ctx.registry.invoke("gate.list", {
        "accounts_file": str(tmp_path / "gate-users.json")}, ctx)
    assert r.ok and r.value["items"] == []
    assert "accounts_file" in r.value
