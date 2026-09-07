# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Role bundles: named scope sets, narrow-only overrides, JIT default role,
and the ``--apply-bundle`` wiring on the ``gate.adduser`` / ``gate.issue``
skills (S1 mechanism).

The platform ships only two generic bundles (``admin``, ``viewer``); the tests
here drive the mechanism, not any consumer's role vocabulary.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.webgate import skills as gate_skills
from axiom.extensions.builtins.webgate.role_bundles import (
    BundleRegistry,
    OverrideWidensError,
    ScopeBundle,
    default_bundle_registry,
    jit_default_role,
    load_overrides,
    register_default_bundles,
)
from axiom.infra.skills import SkillContext, SkillRegistry
from axiom.webauth import JsonFileUserStore


@pytest.fixture
def ctx(tmp_path: Path) -> SkillContext:
    reg = SkillRegistry()
    gate_skills.bind(reg)
    return SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("test.bundles"))


# ---------- ScopeBundle ---------------------------------------------------


def test_scope_bundle_is_frozen_and_validates_grammar():
    b = ScopeBundle(role="viewer", scopes=("*:read",), description="read-only")
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.role = "other"  # type: ignore[misc]
    with pytest.raises(ValueError):
        ScopeBundle(role="bad", scopes=("llm:delete",))  # not a governance verb
    with pytest.raises(ValueError):
        ScopeBundle(role="", scopes=("llm",))


# ---------- BundleRegistry ------------------------------------------------


def test_registry_register_get_roles_and_duplicate_refusal():
    reg = BundleRegistry()
    b = ScopeBundle(role="auditor", scopes=("docs:read",))
    reg.register(b)
    assert reg.get("auditor") is b
    assert reg.get("missing") is None
    assert reg.roles() == ("auditor",)
    with pytest.raises(ValueError):
        reg.register(ScopeBundle(role="auditor", scopes=("llm:read",)))


def test_resolve_unions_sorts_and_dedupes():
    reg = BundleRegistry()
    reg.register(ScopeBundle(role="a", scopes=("llm:read", "docs:read")))
    reg.register(ScopeBundle(role="b", scopes=("docs:read", "chat:invoke")))
    assert reg.resolve(["a", "b"]) == ("chat:invoke", "docs:read", "llm:read")
    # unknown roles contribute nothing rather than failing resolution
    assert reg.resolve(["a", "ghost"]) == ("docs:read", "llm:read")
    assert reg.resolve([]) == ()


def test_default_registry_ships_admin_and_viewer_only():
    reg = default_bundle_registry()
    assert reg.get("admin") is not None and reg.get("admin").scopes == ("*",)
    assert reg.get("viewer") is not None and reg.get("viewer").scopes == ("*:read",)


def test_register_default_bundles_adds_consumer_roles():
    marker = "consumer_role_for_test"
    register_default_bundles([ScopeBundle(role=marker, scopes=("llm:invoke",))])
    try:
        assert default_bundle_registry().get(marker).scopes == ("llm:invoke",)
    finally:
        default_bundle_registry()._unregister_for_tests(marker)


# ---------- site overrides (narrow-only) ----------------------------------


def _write_toml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "roles.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_override_narrows_an_existing_bundle(tmp_path: Path):
    reg = default_bundle_registry()
    p = _write_toml(tmp_path, '[role.viewer]\nscopes = ["llm:read"]\n')
    narrowed = load_overrides(p, reg)
    assert narrowed.resolve(["viewer"]) == ("llm:read",)
    # the input registry is untouched
    assert reg.resolve(["viewer"]) == ("*:read",)


def test_override_adds_a_new_narrow_role(tmp_path: Path):
    reg = BundleRegistry()
    reg.register(ScopeBundle(role="viewer", scopes=("*:read",)))
    p = _write_toml(tmp_path, '[role.auditor]\nscopes = ["docs:read", "llm:read"]\n')
    out = load_overrides(p, reg)
    assert out.resolve(["auditor"]) == ("docs:read", "llm:read")
    assert out.get("viewer") is not None  # defaults carried forward


def test_override_that_widens_is_refused_with_the_scope_named(tmp_path: Path):
    reg = BundleRegistry()
    reg.register(ScopeBundle(role="viewer", scopes=("*:read",)))
    # a brand-new role reaching beyond anything the defaults can grant
    p = _write_toml(tmp_path, '[role.auditor]\nscopes = ["llm:invoke"]\n')
    with pytest.raises(OverrideWidensError, match="llm:invoke"):
        load_overrides(p, reg)


def test_override_widening_an_existing_bundle_is_refused(tmp_path: Path):
    # viewer is read-only; an override may not hand it the write verbs
    p = _write_toml(tmp_path, '[role.viewer]\nscopes = ["llm"]\n')
    with pytest.raises(OverrideWidensError, match="llm"):
        load_overrides(p, default_bundle_registry())


# ---------- JIT default role ----------------------------------------------


def test_jit_default_role_defaults_to_viewer(monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_JIT_ROLE", raising=False)
    assert jit_default_role() == "viewer"


def test_jit_default_role_honors_env(monkeypatch):
    monkeypatch.setenv("AXIOM_GATE_JIT_ROLE", "member")
    assert jit_default_role() == "member"
    monkeypatch.setenv("AXIOM_GATE_JIT_ROLE", "")
    assert jit_default_role() == ""  # explicit opt-out: no default role


# ---------- gate.adduser --apply-bundle -----------------------------------


def test_adduser_apply_bundle_records_resolved_scopes(ctx: SkillContext, tmp_path: Path):
    f = str(tmp_path / "gate-users.json")
    r = ctx.registry.invoke(
        "gate.adduser",
        {
            "email": "v@example.org",
            "password": "Correct-Horse-9",
            "role": ["viewer"],
            "apply_bundle": True,
            "accounts_file": f,
        },
        ctx,
    )
    assert r.ok, r.errors
    assert r.value["bundle_scopes"] == ["*:read"]
    user = JsonFileUserStore(f).get_by_email("v@example.org")
    assert user is not None
    assert user.attributes["bundle_scopes"] == ["*:read"]


def test_adduser_without_flag_keeps_existing_behavior(ctx: SkillContext, tmp_path: Path):
    f = str(tmp_path / "gate-users.json")
    r = ctx.registry.invoke(
        "gate.adduser",
        {
            "email": "plain@example.org",
            "password": "Correct-Horse-9",
            "role": ["viewer"],
            "accounts_file": f,
        },
        ctx,
    )
    assert r.ok, r.errors
    assert "bundle_scopes" not in (r.value or {})
    user = JsonFileUserStore(f).get_by_email("plain@example.org")
    assert "bundle_scopes" not in user.attributes


def test_adduser_apply_bundle_with_no_matching_role_errors(ctx: SkillContext, tmp_path: Path):
    r = ctx.registry.invoke(
        "gate.adduser",
        {
            "email": "x@example.org",
            "password": "Correct-Horse-9",
            "role": ["ghost"],
            "apply_bundle": True,
            "accounts_file": str(tmp_path / "u.json"),
        },
        ctx,
    )
    assert not r.ok
    assert "bundle" in r.errors[0]


# ---------- gate.issue --apply-bundle -------------------------------------


def test_issue_apply_bundle_unions_role_scopes_into_the_key(ctx: SkillContext, tmp_path: Path):
    f = str(tmp_path / "gate-keys.json")
    r = ctx.registry.invoke(
        "gate.issue",
        {
            "resource": "api-key",
            "principal": "@svc:context",
            "role": ["viewer"],
            "apply_bundle": True,
            "keys_file": f,
        },
        ctx,
    )
    assert r.ok, r.errors
    assert r.value["scopes"] == ["*:read"]
    assert r.value["token"]


def test_issue_apply_bundle_unions_with_explicit_scopes(ctx: SkillContext, tmp_path: Path):
    r = ctx.registry.invoke(
        "gate.issue",
        {
            "resource": "api-key",
            "principal": "@svc:context",
            "role": ["viewer"],
            "apply_bundle": True,
            "scope": ["llm:invoke"],
            "keys_file": str(tmp_path / "k.json"),
        },
        ctx,
    )
    assert r.ok, r.errors
    assert r.value["scopes"] == ["*:read", "llm:invoke"]


def test_issue_role_without_apply_bundle_errors(ctx: SkillContext, tmp_path: Path):
    r = ctx.registry.invoke(
        "gate.issue",
        {
            "resource": "api-key",
            "principal": "@svc:context",
            "role": ["viewer"],
            "scope": ["llm"],
            "keys_file": str(tmp_path / "k.json"),
        },
        ctx,
    )
    assert not r.ok
    assert "apply-bundle" in r.errors[0]


def test_issue_without_role_or_scope_still_requires_a_scope(ctx: SkillContext, tmp_path: Path):
    r = ctx.registry.invoke(
        "gate.issue",
        {
            "resource": "api-key",
            "principal": "@svc:context",
            "keys_file": str(tmp_path / "k.json"),
        },
        ctx,
    )
    assert not r.ok
    assert "scope" in r.errors[0]
