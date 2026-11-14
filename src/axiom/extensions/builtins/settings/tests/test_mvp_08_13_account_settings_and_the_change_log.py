# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Account, Settings and the change log, with the audit trail beneath (MVP-08, MVP-13).

What holds here:

- each Account and Settings section is present for the roles in the table and
  absent for others, and the web route answers the same table (MVP-08-A1);
- every change appears in the change log with who, what, old → new and when,
  read back from the one action chain (MVP-08-A2);
- Account holds nothing that changes the site, and Settings nothing personal
  (MVP-08-A4);
- rescoping your own key from Account keeps the same token working, and you
  cannot rescope somebody else's or widen yours past your roles (MVP-08-A5);
- every MVP write (settings, flags, roles, keys, invitations, deletes and a
  denial) produces exactly one audit row (MVP-13-A1);
- the chain verifies, and fails on a tampered or removed row, naming where
  (MVP-13-A2, with its negative controls).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.settings import pages, store
from axiom.extensions.builtins.settings import skills as settings_skills
from axiom.infra import audit_trail
from axiom.infra.skills import SkillContext, SkillRegistry

PERSON = "@jsmith:home-site"
ADMIN = "@admin:home-site"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_get_global_settings_path", lambda: tmp_path / "global.toml")
    monkeypatch.setattr(store, "_PROJECT_SETTINGS_PATH", tmp_path / "project.toml")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    monkeypatch.setenv("AXIOM_SITE", "home-site")
    monkeypatch.delenv(audit_trail.HMAC_KEY_ENV, raising=False)
    audit_trail._reset_for_tests()
    yield
    audit_trail._reset_for_tests()


def _ctx(tmp_path: Path, actor: str | None = ADMIN, registry: SkillRegistry | None = None) -> SkillContext:
    reg = registry or SkillRegistry()
    if registry is None:
        settings_skills.bind(reg)
    return SkillContext(registry=reg, state_dir=tmp_path / "state", logger=logging.getLogger("t"),
                        actor=actor)


def _chain(tmp_path: Path) -> list[dict]:
    return audit_trail.read_chain(audit_trail.audit_path(tmp_path / "state"))


# --- MVP-08-A1: sections by role ----------------------------------------------------------


@pytest.mark.acceptance("MVP-08-A1")
@pytest.mark.parametrize("roles", [[], ["viewer"], ["site-admin"], ["owner"], ["admin", "viewer"]])
def test_each_section_is_present_for_its_roles_and_absent_for_others(roles):
    got = pages.sections_for(roles)
    is_admin = bool(set(roles) & pages.ADMIN_ROLES)
    # Account: everyone has every section.
    assert [s["id"] for s in got["account"]] == [s.id for s in pages.ACCOUNT]
    # Settings: administrators have every section; nobody else has any.
    assert [s["id"] for s in got["settings"]] == ([s.id for s in pages.SETTINGS] if is_admin else [])


@pytest.mark.acceptance("MVP-08-A1")
def test_the_sections_are_the_spec_table():
    assert [s.label for s in pages.ACCOUNT] == [
        "Overview", "Profile", "Sign-in", "Keys", "Notifications", "Preferences", "My agents",
        "Data", "Devices", "Qualifications",
    ]
    labels = [s.label for s in pages.SETTINGS]
    for wanted in ("Flags", "Data and retention", "Health", "Change log"):
        assert wanted in labels
    # Team's work on people is not in Settings.
    assert not any(w in " ".join(labels).lower() for w in ("invitation", "roster", "role"))


def _client(monkeypatch, claims: dict | None):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    import axiom.webauth as webauth
    from axiom.extensions.builtins.settings.api import register_routes

    monkeypatch.setattr(webauth, "session_from_cookies", lambda cookies: claims)
    router = APIRouter()
    register_routes(router)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.acceptance("MVP-08-A1")
def test_the_web_route_answers_the_same_table_for_the_callers_roles(monkeypatch):
    viewer = _client(monkeypatch, {"sub": "jsmith", "email": "jsmith@example.org", "roles": ["viewer"]})
    body = viewer.get("/api/v1/settings/pages").json()
    assert body["settings"] == [] and len(body["account"]) == len(pages.ACCOUNT)
    admin = _client(monkeypatch, {"sub": "admin", "email": "admin@example.org", "roles": ["site-admin"]})
    assert len(admin.get("/api/v1/settings/pages").json()["settings"]) == len(pages.SETTINGS)
    assert _client(monkeypatch, None).get("/api/v1/settings/pages").status_code == 401


# --- MVP-08-A4: Account is personal, Settings is the site's --------------------------------


@pytest.mark.acceptance("MVP-08-A4")
def test_account_holds_nothing_site_wide_and_settings_nothing_personal():
    for s in pages.ACCOUNT:
        assert s.roles is None, f"Account › {s.label} is everyone's own page"
        assert all(whose == pages.PERSONAL for _, whose in s.writes), s.label
    for s in pages.SETTINGS:
        assert s.roles == pages.ADMIN_ROLES, f"Settings › {s.label} is for administrators"
        assert all(whose == pages.SITE for _, whose in s.writes), s.label
    # And by content: no personal write is offered from Settings, no site write from Account.
    account_targets = " ".join(t for s in pages.ACCOUNT for t, _ in s.writes)
    settings_targets = " ".join(t for s in pages.SETTINGS for t, _ in s.writes)
    assert "scope=node" not in account_targets and "/api/v1/settings" not in account_targets
    assert "/gate/keys" not in settings_targets and "/gate/profile" not in settings_targets


# --- MVP-08-A2: the change log ----------------------------------------------------------------


@pytest.mark.acceptance("MVP-08-A2")
def test_every_change_appears_with_who_what_old_new_and_when(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.registry.invoke("settings.set", {"key": "interface.stream", "value": "false"}, ctx)
    ctx.registry.invoke("settings.set", {"key": "interface.stream", "value": "true"}, ctx)
    rows = settings_skills.list_changes({}, ctx).value["changes"]
    assert [(r["who"], r["kind"], r["what"], r["old"], r["new"]) for r in rows] == [
        (ADMIN, "setting", "interface.stream", False, True),   # newest first
        (ADMIN, "setting", "interface.stream", True, False),
    ]
    assert all(r["when"] and r["detail"] for r in rows)


@pytest.mark.acceptance("MVP-08-A2")
def test_a_credential_like_setting_is_logged_as_set_never_by_value(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.registry.invoke("settings.set", {"key": "rag.database_url", "value": "postgresql://u:hunter2@db/x"}, ctx)
    (row,) = settings_skills.list_changes({}, ctx).value["changes"]
    assert row["new"] == "(set)" and "hunter2" not in json.dumps(_chain(tmp_path))


@pytest.mark.acceptance("MVP-08-A2")
def test_reading_the_change_log_writes_nothing(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.registry.invoke("settings.set", {"key": "interface.stream", "value": "false"}, ctx)
    before = len(_chain(tmp_path))
    ctx.registry.invoke("settings.changes", {"audit": True}, ctx)
    assert len(_chain(tmp_path)) == before


@pytest.mark.acceptance("MVP-08-A2")
def test_mine_is_only_my_changes_and_the_site_log_is_for_administrators(tmp_path, monkeypatch):
    settings_skills.bind(reg := SkillRegistry())
    for who in (PERSON, ADMIN):
        c = _ctx(tmp_path, actor=who, registry=reg)
        reg.invoke("settings.set", {"key": "chat.advisor.enabled", "value": "true"}, c)
    mine = settings_skills.list_changes({"mine": True}, _ctx(tmp_path, actor=PERSON, registry=reg))
    assert {r["who"] for r in mine.value["changes"]} == {PERSON}

    claims = {"sub": "jsmith", "email": "jsmith@example.org", "site": "home-site", "roles": ["viewer"]}
    viewer = _client(monkeypatch, claims)
    assert viewer.get("/api/v1/settings/changes").status_code == 403
    assert viewer.get("/api/v1/settings/changes?mine=true").status_code == 200
    assert viewer.get("/api/v1/settings/changes?mine=true&audit=true").status_code == 403


@pytest.mark.acceptance("MVP-08-A2")
def test_a_web_change_is_logged_as_the_person_who_made_it(monkeypatch):
    claims = {"sub": "admin", "email": "admin@example.org", "site": "home-site", "roles": ["site-admin"]}
    client = _client(monkeypatch, claims)
    assert client.patch("/api/v1/settings", json={"key": "interface.stream", "value": False}).status_code == 200
    body = client.get("/api/v1/settings/changes?audit=true").json()
    (row,) = body["changes"]
    assert row["who"].startswith("@admin") and row["what"] == "interface.stream"
    assert (row["old"], row["new"]) == (True, False)
    assert body["verification"]["ok"] is True


# --- MVP-13-A1: exactly one audit row per MVP write --------------------------------------------


def _gate_registry():
    from axiom.extensions.builtins.features import skills as flag_skills
    from axiom.extensions.builtins.webgate import skills as gate_skills

    reg = SkillRegistry()
    settings_skills.bind(reg)
    gate_skills.bind(reg)
    flag_skills.bind(reg)
    return reg


class _Overrides:
    def __init__(self):
        self.rows: dict[tuple[str, str, str], str] = {}

    def overrides_for(self, scope, scope_id):
        return {k: v for (s, i, k), v in self.rows.items() if s == scope and i == scope_id}

    def put(self, scope, scope_id, key, value, *, by):
        self.rows[(scope, scope_id, key)] = value
        return {"scope": scope, "scope_id": scope_id, "key": key, "value": value, "updated_by": by}

    def delete(self, scope, scope_id, key):
        return self.rows.pop((scope, scope_id, key), None) is not None


@pytest.fixture
def flags_in_memory(monkeypatch):
    # The flag verbs live in the features extension (ADR-254); ``flags.set`` is their alias.
    from axiom.extensions.builtins.features import flag_registry
    from axiom.extensions.builtins.features.flag_registry import FlagDef
    from axiom.extensions.builtins.features.resolver import FlagService
    from axiom.extensions.builtins.features.skills import _flags as flag_skills

    flag_registry.set_registry((FlagDef(key="needs_you", description="d", default="off", owner="t"),))
    overrides = _Overrides()
    monkeypatch.setattr(flag_skills, "_service", lambda: FlagService(overrides=overrides, variant=None))
    yield overrides
    flag_registry.set_registry(None)


@pytest.mark.acceptance("MVP-13-A1")
def test_every_mvp_write_produces_exactly_one_audit_row(tmp_path, flags_in_memory, monkeypatch):
    from axiom.extensions.builtins.webgate.skills._accounts import KEYS_ENV
    from axiom.infra.hooks import HookBus, HookSpec, deny
    from axiom.infra.skill_dispatch import invoke_capability
    from axiom.webauth import append_api_key_record, mint_api_key, upsert_user_record

    keys, accounts, invites = tmp_path / "keys.json", tmp_path / "accounts.json", tmp_path / "inv.json"
    monkeypatch.setenv(KEYS_ENV, str(keys))
    upsert_user_record(accounts, email="jsmith@example.org", password_hash="scrypt$test", roles=["viewer"])
    _, record = mint_api_key(principal="@jsmith", scopes=("*:read", "chart:read"), site="home-site")
    append_api_key_record(keys, record)
    key_id = record["key_id"]

    reg = _gate_registry()
    ctx = _ctx(tmp_path, registry=reg)
    writes = [
        ("settings.set", {"key": "interface.stream", "value": "false"}),
        ("flags.set", {"key": "needs_you", "value": "on", "scope": "node", "scope_id": "node-a"}),
        ("gate.role", {"email": "jsmith@example.org", "add": ["site-admin"], "accounts_file": str(accounts)}),
        ("gate.issue", {"principal": "@jsmith", "scope": ["chart:read"], "keys_file": str(keys)}),
        ("gate.rescope", {"key_id": key_id, "remove_scope": ["chart:read"], "keys_file": str(keys)}),
        ("gate.invite", {"principal": "@new", "scope": ["chart:read"], "invitations_file": str(invites)}),
        ("gate.revoke", {"key_id": key_id, "keys_file": str(keys)}),
    ]
    for name, params in writes:
        before = len(_chain(tmp_path))
        result = reg.invoke(name, params, ctx)
        rows = _chain(tmp_path)[before:]
        assert len(rows) == 1, f"{name}: {len(rows)} audit rows ({result.errors})"
        assert rows[0]["skill"] == name and rows[0]["principal"] == ADMIN

    # A denied write is still exactly one row, outcome "denied", and the skill never ran.
    bus = HookBus()
    bus.register(HookSpec(event="tool.pre_invoke", entry=lambda c: deny(reason="site rule"),
                          priority=10, fail_mode="abort", source="test"))
    before = len(_chain(tmp_path))
    out = invoke_capability(reg, "settings.set", {"key": "interface.stream", "value": "true"}, ctx,
                            surface="web", hookbus=bus)
    rows = _chain(tmp_path)[before:]
    assert not out.ok and [r["outcome"] for r in rows] == ["denied"]
    assert store.SettingsStore().get("interface.stream") is False

    # A read is not a write: no row.
    before = len(_chain(tmp_path))
    reg.invoke("settings.list", {}, ctx)
    reg.invoke("settings.changes", {}, ctx)
    assert len(_chain(tmp_path)) == before


# --- MVP-13-A2: chain verification, with negative controls -------------------------------------


def _three_changes(tmp_path):
    ctx = _ctx(tmp_path)
    for v in ("false", "true", "false"):
        ctx.registry.invoke("settings.set", {"key": "interface.stream", "value": v}, ctx)
    return audit_trail.audit_path(tmp_path / "state")


@pytest.mark.acceptance("MVP-13-A2")
def test_the_chain_verifies(tmp_path):
    _three_changes(tmp_path)
    got = settings_skills.list_changes({"audit": True}, _ctx(tmp_path)).value["verification"]
    assert got == {"ok": True, "records": 3, "broken_at": None, "reason": ""}


@pytest.mark.acceptance("MVP-13-A2")
def test_a_tampered_row_fails_verification_and_is_named(tmp_path):
    path = _three_changes(tmp_path)
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["change"]["new"] = "tampered"  # rewrite history: an edited old → new
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    got = audit_trail.verify_report(path)
    assert got["ok"] is False and got["broken_at"] == 2 and "edited" in got["reason"]


@pytest.mark.acceptance("MVP-13-A2")
def test_a_removed_row_fails_verification(tmp_path):
    path = _three_changes(tmp_path)
    lines = path.read_text().splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n")
    got = audit_trail.verify_report(path)
    assert got["ok"] is False and got["broken_at"] == 3


@pytest.mark.acceptance("MVP-13-A2")
def test_audit_trail_verb_reports_the_break(tmp_path):
    from axiom.extensions.builtins.authz import skills as audit_skills

    path = _three_changes(tmp_path)
    reg = SkillRegistry()
    audit_skills.bind(reg)
    ctx = _ctx(tmp_path, registry=reg)
    assert reg.invoke("audit.trail", {"verify": True}, ctx).ok
    path.write_text(path.read_text().replace('"ok"', '"error"', 1))
    bad = reg.invoke("audit.trail", {"verify": True}, ctx)
    assert not bad.ok and "broken at record 1" in bad.errors[0]


@pytest.mark.acceptance("MVP-13-A1")
def test_uploads_and_deletes_are_audited_writes_and_the_seam_writes_one_row(tmp_path):
    """Library's uploads and deletes reach the same seam: registered as writes, one row each."""
    from axiom.extensions.builtins.library import skills as library_skills
    from axiom.infra.skills import SkillResult

    reg = SkillRegistry()
    library_skills.bind(reg)
    for name in ("library.upload", "library.delete"):
        assert reg.has(name) and reg.is_mutating(name), name
    for name in ("library.list", "library.show", "library.search"):
        assert not reg.is_mutating(name), name

    # The seam, with the store stubbed: one row per call, whichever outcome.
    stub = SkillRegistry()
    stub.register("library.upload", lambda p, c: SkillResult(ok=True))
    stub.register("library.delete", lambda p, c: SkillResult(ok=False, errors=["held"]))
    ctx = _ctx(tmp_path, registry=stub)
    stub.invoke("library.upload", {"name": "a.pdf"}, ctx)
    stub.invoke("library.delete", {"id": "x"}, ctx)
    assert [(r["skill"], r["outcome"]) for r in _chain(tmp_path)] == [
        ("library.upload", "ok"), ("library.delete", "error")]
