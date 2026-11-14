# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""LOOP-F11: every flag has an owner and a review date. A flag fully on past its review date,
or off and unreferenced, is reported to its owner as work to remove.

Pure report first (declarations, the record's rollouts and overrides, a reference search over
real files), then every surface (skill, CLI, MCP binding, HTTP), then the weekly schedule that
sends each owner one grouped message.
"""

from __future__ import annotations

import json
import tomllib
from datetime import date
from pathlib import Path

import pytest

from axiom.extensions.builtins.features.flag_registry import FlagDef, parse_manifest_flags
from axiom.extensions.builtins.flags import lifecycle

pytestmark = pytest.mark.acceptance("LOOP-F11")

TODAY = date(2026, 11, 12)
EXT = Path(__file__).resolve().parents[1]


def flag(key, **kw) -> FlagDef:
    return FlagDef.from_mapping({"key": key, **kw}, owner="ext")


# -- declarations ---------------------------------------------------------------


def test_a_flag_declares_an_owner_and_a_review_date():
    f = flag("new_editor", default="off", owner="@ana:site", review_by="2027-01-01")
    assert f.owned_by == "@ana:site" and f.review_by == date(2027, 1, 1)
    assert f.owner == "ext", "owner on the dataclass stays the declaring extension"


def test_a_release_flag_must_have_both_and_its_review_date_defaults_to_its_removal_date():
    with pytest.raises(ValueError, match="declares owner"):
        flag("r", kind="release", remove_by="2027-03-01")
    r = flag("r", kind="release", remove_by="2027-03-01", owner="@operator")
    assert r.review_by == date(2027, 3, 1)
    assert flag("r2", kind="release", remove_by="2027-03-01", owner="@operator",
                review_by="2027-02-01").review_by == date(2027, 2, 1)


def test_an_owner_that_is_not_a_principal_is_refused_and_a_flag_with_none_still_loads():
    with pytest.raises(ValueError, match="principal or a role"):
        flag("x", owner="somebody")
    assert flag("legacy").owned_by == "" and flag("legacy").review_by is None


# -- the report -----------------------------------------------------------------


def _report(flags, *, rollouts=None, overrides=None, referenced=()):
    return lifecycle.stale_report(
        {f.key: f for f in flags}, environment="production", today=TODAY,
        rollouts=rollouts or {}, overrides=overrides or [], referenced=lambda k: k in referenced,
    )


def _keys(report, case):
    return sorted(i["key"] for i in report["stale"] if i["case"] == case)


def test_fully_on_past_its_review_date_is_reported_and_not_before():
    flags = [
        flag("old_on", default="on", owner="@ana", review_by="2026-10-01"),
        flag("young_on", default="on", owner="@ana", review_by="2027-10-01"),
        flag("old_off", default="off", owner="@ana", review_by="2026-10-01"),
    ]
    r = _report(flags, referenced={"old_on", "young_on", "old_off"})
    assert _keys(r, lifecycle.ON_PAST_REVIEW) == ["old_on"]
    item = next(i for i in r["stale"] if i["key"] == "old_on")
    assert item["owner"] == "@ana" and item["review_by"] == "2026-10-01" and item["days_past"] == 42


def test_a_rollout_or_override_that_can_still_serve_off_means_it_is_not_fully_on():
    flags = [flag("partial", default="on", owner="@ana", review_by="2026-01-01"),
             flag("overridden", default="on", owner="@ana", review_by="2026-01-01"),
             flag("killed", default="on", owner="@ana", review_by="2026-01-01"),
             flag("everyone", default="off", owner="@ana", review_by="2026-01-01")]
    r = _report(
        flags,
        rollouts={"partial": {"rules": [{"cohort": "beta", "value": "off"}]},
                  "killed": {"rules": [], "kill_switch": True},
                  "everyone": {"rules": [{"value": "on"}]}},
        overrides=[{"key": "overridden", "value": "off", "expires_at": None}],
        referenced={"partial", "overridden", "killed", "everyone"},
    )
    assert _keys(r, lifecycle.ON_PAST_REVIEW) == ["everyone"], "a catch-all rule turns everybody on"


def test_off_with_no_reference_left_in_code_is_reported_whatever_its_date():
    flags = [flag("dead", default="off", owner="@ana"), flag("dormant", default="off", owner="@ana")]
    r = _report(flags, referenced={"dormant"})
    assert _keys(r, lifecycle.OFF_UNREFERENCED) == ["dead"]


def test_a_kill_switch_is_never_stale_and_an_unowned_flag_is_listed_not_failed():
    flags = [flag("panic", kind="kill_switch", default="on", owner="@ana", review_by="2020-01-01"),
             flag("orphan", default="off"), flag("orphan_on", default="on")]
    r = _report(flags)
    assert "panic" not in {i["key"] for i in r["stale"]}
    assert sorted(r["unowned"]) == ["orphan", "orphan_on"]
    assert _keys(r, lifecycle.OFF_UNREFERENCED) == ["orphan"]
    assert r["by_owner"][lifecycle.UNOWNED] == ["orphan"]
    assert r["ok"] is True, "the report never fails a flag for having no owner"


def test_the_reference_search_reads_code_and_skips_manifests_and_tests(tmp_path: Path):
    src = tmp_path / "pkg"
    (src / "tests").mkdir(parents=True)
    (src / "app.py").write_text('if flags.is_enabled("used_py"): pass\n')
    (src / "ui.tsx").write_text("const on = flags.used_ts;\n")
    (src / "env.sh").write_text("AXIOM_FLAG_USED_ENV=on\n")
    (src / "axiom-extension.toml").write_text('[[flags]]\nkey = "only_declared"\n')
    (src / "tests" / "test_x.py").write_text('assert flag("only_tested")\n')
    found = lifecycle.find_references(["used_py", "used_ts", "used_env", "only_declared", "only_tested"], [src])
    assert sorted(k for k, v in found.items() if v) == ["used_env", "used_py", "used_ts"]


# -- surfaces -------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path, monkeypatch, pinned_registry):
    from axiom.extensions.builtins.features.flag_registry import set_registry
    from axiom.extensions.builtins.flags.skills import bind
    from axiom.infra.skills import SkillContext, SkillRegistry

    set_registry([flag("old_on", default="on", owner="@ana", review_by="2026-01-01"),
                  flag("dead", default="off", owner="@bo"),
                  flag("dead_too", default="off", owner="@bo"),
                  flag("orphan", default="off")])
    reg = SkillRegistry()
    bind(reg)
    import logging

    return SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))


@pytest.fixture
def maintainers(tmp_path, monkeypatch):
    """Set the node's ``flags.maintainers`` (and any other setting) in a throwaway settings file."""
    from axiom.extensions.builtins.settings import store

    path = tmp_path / "settings.toml"
    monkeypatch.setattr(store, "_get_global_settings_path", lambda: path)
    monkeypatch.setattr(store, "_PROJECT_SETTINGS_PATH", tmp_path / "no-project-settings.toml")

    def configure(*handles, site_admin=""):
        lines = ["[flags]", "maintainers = [" + ", ".join(f'"{h}"' for h in handles) + "]"]
        if site_admin:
            lines += ["[notifications.roles]", f'site-admin = "{site_admin}"']
        path.write_text("\n".join(lines) + "\n")

    return configure


def _invoke(ctx, name, params):
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability

    return invoke_capability(ctx.registry, name, params, ctx, surface=CLI_SURFACE)


def test_the_stale_report_answers_without_a_database_and_says_so(ctx, tmp_path):
    res = _invoke(ctx, "flags.stale", {"today": "2026-11-12", "roots": [str(tmp_path)]})
    assert res.ok, res.errors
    assert {i["key"] for i in res.value["stale"]} == {"old_on", "dead", "dead_too", "orphan"}
    assert res.value["record"].startswith("unread"), "says the record could not be read rather than guessing"
    assert res.value["searched"] == [str(tmp_path)]


def test_the_cli_verb_is_flags_stale(ctx, tmp_path, capsys, monkeypatch):
    from axiom.extensions.builtins.flags import cli

    monkeypatch.setattr(cli, "_registry_context", lambda: ctx)
    assert cli.main(["--json", "stale", "--today", "2026-11-12", "--root", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["by_owner"]["@bo"] == ["dead", "dead_too"]
    assert cli.main(["stale", "--today", "2026-11-12", "--root", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "@bo" in text and "dead_too" in text and "unowned: orphan" in text


def test_the_weekly_notice_reaches_each_configured_maintainer_once_and_only_when_on(ctx, tmp_path, monkeypatch,
                                                                                  maintainers):
    sent: list[tuple[str, str, str]] = []

    def sender(recipient, summary, body, dedup):
        sent.append((recipient, summary, dedup))
        return f"r{len(sent)}"

    maintainers("@mo", "@kai:site")
    params = {"today": "2026-11-12", "roots": [str(tmp_path)], "_sender": sender}
    monkeypatch.setenv("AXIOM_FLAG_FLAG_REVIEW_NOTICES", "off")
    res = _invoke(ctx, "flags.stale_notify", params)
    assert res.ok and sent == [] and res.value["sent"] == [] and "off" in res.value["why"]

    monkeypatch.setenv("AXIOM_FLAG_FLAG_REVIEW_NOTICES", "on")
    res = _invoke(ctx, "flags.stale_notify", params)
    assert res.ok, res.errors
    assert sorted(r for r, _, _ in sent) == ["@kai:site", "@mo"], "one grouped notice per maintainer"
    assert all("4 flag" in summary for _, summary, _ in sent), "every stale flag, grouped in one message"
    assert next(d for r, _, d in sent if r == "@mo") == "flags.stale:@mo:2026-W46", "a retry that week sends nothing new"


def test_a_notice_never_reaches_a_site_admin(ctx, tmp_path, monkeypatch, maintainers):
    sent: list[str] = []
    maintainers("@mo", "@site-admin", "@operator", site_admin="@ana")
    monkeypatch.setenv("AXIOM_FLAG_FLAG_REVIEW_NOTICES", "on")
    res = _invoke(ctx, "flags.stale_notify", {"today": "2026-11-12", "roots": [str(tmp_path)],
                                              "_sender": lambda r, s, b, d: sent.append(r) or "r"})
    assert res.ok, res.errors
    assert sent == ["@mo"], "the site admin (here @ana) and the role handles are never recipients"
    assert "owner" not in res.value["sent"][0], "flags owned by a person or a role are not routed to that owner"


def test_with_no_maintainer_setting_nothing_is_sent_and_the_report_says_so(ctx, tmp_path, monkeypatch, maintainers):
    sent: list[str] = []
    monkeypatch.setenv("AXIOM_FLAG_FLAG_REVIEW_NOTICES", "on")
    params = {"today": "2026-11-12", "roots": [str(tmp_path)], "_sender": lambda r, s, b, d: sent.append(r) or "r"}
    res = _invoke(ctx, "flags.stale_notify", params)  # the setting is unset: the empty default
    assert res.ok and sent == [] and res.value["sent"] == []
    assert "no maintainer configured" in res.value["why"]
    report = _invoke(ctx, "flags.stale", {"today": "2026-11-12", "roots": [str(tmp_path)]}).value
    assert report["maintainers"] == [] and "no maintainer configured" in report["note"]
    assert "no maintainer configured" in "\n".join(__import__("axiom.extensions.builtins.flags.cli",
                                                           fromlist=["x"]).print_stale(report))


def test_the_schedule_is_declared_weekly_on_the_platform_scheduler():
    manifest = tomllib.loads((EXT / "axiom-extension.toml").read_text())
    sched = {s["name"]: s for s in manifest["extension"]["schedule"]}["flags_stale_weekly"]
    assert sched["action"] == "flags.stale_notify" and sched["hosted"] is True
    assert sched["cadence"]["kind"] == "cron" and sched["cadence"]["cron"].split()[-1] == "1"
    notices = {f.key: f for f in parse_manifest_flags(manifest, owner="flags")}["flag_review_notices"]
    assert notices.default == "off" and notices.review_by


# -- who reads it: site admins (read-only) and maintainers; who is told: maintainers only ---


def test_every_built_in_flag_is_owned_by_the_maintainers_with_its_kind_and_review_date():
    import axiom.extensions.builtins as builtins

    found = {}
    for manifest_path in Path(builtins.__file__).parent.glob("*/axiom-extension.toml"):
        manifest = tomllib.loads(manifest_path.read_text())
        for f in parse_manifest_flags(manifest, owner=manifest_path.parent.name):
            found[f.key] = f
    assert len(found) == 12, sorted(found)
    assert {f.owned_by for f in found.values()} == {lifecycle.MAINTAINERS}, "a group, never a person or the site admin"
    assert {k for k, f in found.items() if f.kind == "permission"} == {"library_labels", "approvals_signed"}
    assert {k for k, f in found.items() if f.kind == "operational"} == {"feedback_auto_stop"}
    assert {f.kind for k, f in found.items()} == {"release", "permission", "operational"}
    assert {f.review_by for f in found.values()} == {date(2027, 2, 10)}


def test_permission_flags_never_appear_as_stale_even_fully_on_and_long_past_review():
    perms = [flag("library_labels", kind="permission", default="on", owner="@maintainers", review_by="2020-01-01"),
             flag("approvals_signed", kind="permission", default="off", owner="@maintainers", review_by="2020-01-01"),
             flag("a_release", kind="release", default="on", owner="@maintainers", review_by="2020-01-01",
                  remove_by="2020-02-01")]
    r = _report(perms)
    assert [i["key"] for i in r["stale"]] == ["a_release"], "only the release flag is work to remove"


def test_mcp_refuses_a_caller_who_is_neither_a_site_admin_nor_a_maintainer_and_answers_both(ctx, tmp_path,
                                                                                         maintainers):
    from dataclasses import replace

    from axiom.extensions.builtins.features import approvals
    from axiom.infra.skill_dispatch import MCP_SURFACE, invoke_capability

    params = {"today": "2026-11-12", "roots": [str(tmp_path)]}
    approvals.set_role_resolver(lambda who: ["site-admin"] if who == "@ana:site" else ["viewer"])
    maintainers("@mo")
    try:
        res = invoke_capability(ctx.registry, "flags.stale", params, replace(ctx, actor="@bo:site"), surface=MCP_SURFACE)
        assert not res.ok and "site admin" in "; ".join(res.errors)
        res = invoke_capability(ctx.registry, "flags.stale", params, replace(ctx, actor="@ana:site"), surface=MCP_SURFACE)
        assert res.ok, res.errors
        res = invoke_capability(ctx.registry, "flags.stale", params, replace(ctx, actor="@mo:site"), surface=MCP_SURFACE)
        assert res.ok, "a maintainer who is no site admin reads the report too"
    finally:
        approvals.set_role_resolver(None)


def test_http_serves_the_report_to_a_site_admin_and_a_maintainer_and_refuses_everyone_else(ctx, monkeypatch,
                                                                                         maintainers):
    from dataclasses import replace

    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.flags import api

    monkeypatch.setattr(api, "_stale_ctx", lambda actor: replace(ctx, actor=actor))
    router = APIRouter()
    api.register_routes(router)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(api, "_claims", lambda request: {})
    assert TestClient(app).get("/api/v1/flags/stale").status_code == 401
    for roles in ([], ["viewer"], ["admin"]):
        monkeypatch.setattr(api, "_claims", lambda request, r=roles: {"sub": "bo", "roles": r})
        assert TestClient(app).get("/api/v1/flags/stale").status_code == 403, roles
    maintainers("@mo")
    monkeypatch.setattr(api, "_claims", lambda request: {"sub": "mo", "roles": ["viewer"]})
    assert TestClient(app).get("/api/v1/flags/stale", params={"today": "2026-11-12"}).status_code == 200
    monkeypatch.setattr(api, "_claims", lambda request: {"sub": "ana", "roles": ["operator"]})
    body = TestClient(app).get("/api/v1/flags/stale", params={"today": "2026-11-12"})
    assert body.status_code == 200, "a grant stored under the retired id still counts as site-admin (ADR-261)"
    assert body.json()["by_owner"]["@ana"] == ["old_on"]
