# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MVP-25-A6: ``sites.checklist`` with ``kind=first_run`` is five steps, read from real sources.

Every test is hermetic: probes are injected, or the state directory is a tmp_path.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.sites import first_run
from axiom.extensions.builtins.sites.skills import bind_default
from axiom.infra.skills import SkillContext

IDS = ["connect_data", "invite_people", "set_branding", "pick_landing", "first_chart"]


def _all(done: bool):
    return {
        k: (lambda d=done: (d, "x"))
        for k in (
            "connect_data_probe",
            "invite_probe",
            "branding_probe",
            "landing_probe",
            "chart_probe",
        )
    }


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    for var in (
        "AXIOM_GATE_INVITATIONS_FILE",
        "AXIOM_GATE_API_KEYS_FILE",
        "AXIOM_BRAND_NAME",
        "AXIOM_BRAND_ACCENT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_exactly_five_steps_in_order_with_actions():
    doc = first_run.report(**_all(False))
    assert [s["id"] for s in doc["steps"]] == IDS
    assert [s["label"] for s in doc["steps"]] == [
        "Connect data",
        "Invite people",
        "Set branding",
        "Pick a landing page",
        "First chart from chat",
    ]
    for s in doc["steps"]:
        assert set(s) == {"id", "label", "state", "detail", "action"}
        assert set(s["action"]) == {"label", "prompt"}
    assert doc["steps"][0]["action"] == {
        "label": "+ Connect data",
        "prompt": "Help me connect data with a data kit",
    }
    assert (doc["done"], doc["total"], doc["complete"]) == (0, 5, False)


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_tally_counts_done_steps():
    doc = first_run.report(**{**_all(False), "branding_probe": lambda: (True, "named")})
    assert [s["state"] for s in doc["steps"]] == ["todo", "todo", "done", "todo", "todo"]
    assert (doc["done"], doc["complete"]) == (1, False)
    full = first_run.report(**_all(True))
    assert (full["done"], full["complete"]) == (5, True)


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_a_probe_that_raises_is_todo_and_says_what_could_not_be_read():
    def boom():
        raise OSError("disk gone")

    doc = first_run.report(**{**_all(True), "invite_probe": boom})
    step = doc["steps"][1]
    assert step["state"] == "todo" and "could not read" in step["detail"] and "disk gone" in step["detail"]
    assert doc["done"] == 4


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_branding_reads_the_web_brand(monkeypatch):
    assert first_run.set_branding()[0] is False
    monkeypatch.setenv("AXIOM_BRAND_NAME", "Pilot Site")
    assert first_run.set_branding()[0] is True


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_invite_reads_the_invitations_file(tmp_path, monkeypatch):
    path = tmp_path / "invitations.json"
    monkeypatch.setenv("AXIOM_GATE_INVITATIONS_FILE", str(path))
    assert first_run.invite_people()[0] is False  # absent file: nobody invited
    from datetime import timedelta

    from axiom.webauth.invitations import append_invitation, mint_invitation

    _, rec = mint_invitation(
        principal="@pat", roles=["viewer"], scopes=["read"], site=None, expires_in=timedelta(days=7)
    )
    append_invitation(path, rec)
    ok, detail = first_run.invite_people()
    assert ok is True and "1 invitation" in detail


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_landing_reads_the_stored_setting(tmp_path, monkeypatch):
    from axiom.extensions.builtins.settings import store

    monkeypatch.setattr(store, "_PROJECT_SETTINGS_PATH", tmp_path / "project.toml")
    assert first_run.pick_landing()[0] is False
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "settings.toml").write_text('[interface]\nlanding = "charts"\n')
    ok, detail = first_run.pick_landing()
    assert ok is True and "charts" in detail


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_first_chart_reads_the_saved_catalogue(monkeypatch):
    from axiom.extensions.builtins.charts import saved

    assert first_run.first_chart("")[0] is False
    monkeypatch.setattr(saved, "listing", lambda who: [])
    assert first_run.first_chart("@pat")[0] is False
    monkeypatch.setattr(saved, "listing", lambda who: [{"id": "c1"}])
    assert first_run.first_chart("@pat")[0] is True


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_connect_data_is_done_with_a_gold_dataset_and_never_reads_silver(monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import gold
    from axiom.infra.skills import SkillResult

    seen = []

    def tables(params, ctx):
        seen.append("gold.tables")
        return SkillResult(ok=True, value={"data": {"tables": [{"table": "signals"}]}})

    monkeypatch.setattr(gold, "tables", tables)
    ok, detail = first_run.connect_data(ctx=object())
    assert ok is True and seen == ["gold.tables"] and "1 gold" in detail


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_connect_data_unreadable_is_todo_naming_the_failure(monkeypatch):
    from axiom.extensions.builtins.data_platform.kit import host
    from axiom.extensions.builtins.data_platform.skills import gold
    from axiom.infra.skills import SkillResult

    monkeypatch.setattr(gold, "tables", lambda p, c: SkillResult(ok=False, errors=["no database"]))

    def down(dsn, **kw):
        raise RuntimeError("registry down")

    monkeypatch.setattr(host, "review", down)
    ok, detail = first_run.connect_data(ctx=object())
    assert ok is False and "could not read" in detail and "no database" in detail


def _ctx():
    import logging

    reg = bind_default()
    return reg, SkillContext(registry=reg, state_dir=__import__("pathlib").Path("."), logger=logging.getLogger("t"))


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_the_skill_serves_first_run_without_a_site(monkeypatch):
    monkeypatch.setattr(first_run, "connect_data", lambda ctx=None: (False, "x"))
    reg, ctx = _ctx()
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability

    r = invoke_capability(reg, "sites.checklist", {"kind": "first_run"}, ctx, surface=CLI_SURFACE)
    assert r.ok and [s["id"] for s in r.value["steps"]] == IDS and r.value["total"] == 5
    assert "First run" in r.value["text"]
    # Without the kind, the site checklist is unchanged: it still requires a site.
    r2 = invoke_capability(reg, "sites.checklist", {}, ctx, surface=CLI_SURFACE)
    assert not r2.ok and "site" in r2.errors[0]


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_the_cli_reaches_the_same_skill(monkeypatch, capsys):
    from axiom.extensions.builtins.sites import cli

    monkeypatch.setattr(first_run, "connect_data", lambda ctx=None: (False, "x"))
    code = cli.main(["checklist", "--first-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and [s["id"] for s in out["steps"]] == IDS


@pytest.mark.acceptance("MVP-25-A6")
def test_mvp_25_a6_get_api_v1_first_run(monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.sites import api

    seen = {}
    real = first_run.report

    def fake(principal="", ctx=None, **kw):
        seen["principal"] = principal
        return real(**_all(False))

    monkeypatch.setattr(first_run, "report", fake)
    monkeypatch.setattr(api, "_claims", lambda request: {"sub": "@pat"})
    router = APIRouter()
    api.register_routes(router)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    r = TestClient(app).get("/api/v1/first-run")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"kind", "steps", "done", "total", "complete"}
    assert [s["id"] for s in body["steps"]] == IDS and seen["principal"] == "@pat"
