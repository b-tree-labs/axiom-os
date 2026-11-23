# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A lane serves its web dev server on front and its node on api, with its own database.

Everything goes through the real lane registry (a temp file) and real
processes; only the node's own start-up and the Postgres server are replaced.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from axiom.extensions.builtins.dev import config, node, schema, web
from axiom.extensions.builtins.dev.skills import check, db, down, resolve_lane, status, up
from axiom.extensions.builtins.lane import doctor
from axiom.extensions.builtins.lane.registry import UNMANAGED
from axiom.extensions.builtins.lane.skills import claim
from axiom.infra.skills import SkillResult

from .conftest import Ctx, _git_checkout

# -- dev.toml ----------------------------------------------------------------


def test_dev_toml_is_optional(tmp_path):
    assert config.load(tmp_path) == config.DevConfig()


def test_dev_toml_reads_the_web_and_dev_sections(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "cfg").mkdir()
    (tmp_path / "dev.toml").write_text(
        textwrap.dedent(
            """
            [web]
            cwd = "ui"
            command = "npm run dev -- --port {port}"
            ready_path = "app/"
            env = { VITE_API_TARGET = "{node_url}" }
            [dev]
            config_dir = "cfg"
            seed = "pkg.mod:load"
            shots = "make shots"
            """
        )
    )
    cfg = config.load(tmp_path)
    assert cfg.web.ready_path == "/app/" and cfg.web.cwd == "ui"
    assert cfg.config_dir == "cfg" and cfg.seed == "pkg.mod:load" and cfg.shots == "make shots"
    assert (
        config.fill("--port {port} {node_url}", port=1, node_url="http://x") == "--port 1 http://x"
    )


@pytest.mark.parametrize("body", ["[web]\ncwd='.'\n", "[web\n", "[dev]\nconfig_dir='nope'\n"])
def test_a_bad_dev_toml_says_what_to_fix(tmp_path, body):
    (tmp_path / "dev.toml").write_text(body)
    with pytest.raises(config.DevConfigError):
        config.load(tmp_path)


# -- lanes by role, through the real registry --------------------------------


def test_two_checkouts_get_distinct_lanes_ports_and_databases(tmp_path, lanes):
    a = resolve_lane({"root": str(_git_checkout(tmp_path / "alpha"))})[0]
    b = resolve_lane({"root": str(_git_checkout(tmp_path / "beta"))})[0]
    assert len({a["front"], a["api"], b["front"], b["api"]}) == 4
    assert a["database"] != b["database"]
    assert a["database"].startswith("axiom_dev_") and a["isolated"]
    assert lanes.get(a["name"]).ports == (a["front"], a["api"])  # the registry chose, dev did not


def test_a_second_call_reuses_the_lane(tmp_path, lanes):
    root = str(_git_checkout(tmp_path / "alpha"))
    assert resolve_lane({"root": root})[0] == resolve_lane({"root": root})[0]
    assert len(lanes.all()) == 1


def test_an_unmanaged_lane_is_refused_with_the_claim_command(tmp_path, lanes, monkeypatch):
    root = _git_checkout(tmp_path / "alpha")
    claim.run({"root": str(root), "dsn_var": UNMANAGED}, None)
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h:5432/x")
    result = up.run({"root": str(root), "email": "a@example.org"}, Ctx(tmp_path / "state"))
    assert not result.ok
    assert "lane claim --replace" in "\n".join(result.errors)


# -- web server on front, node on api ----------------------------------------


def _free_pair_checkout(tmp_path, lanes, with_web: bool):
    root = _git_checkout(tmp_path / "site")
    (root / "ui").mkdir()
    (root / "ui" / "tokens.css").write_text(":root { --a: 1; }\n")
    if with_web:
        (root / "dev.toml").write_text(
            '[web]\ncwd = "ui"\ncommand = "PY -m http.server {port} --bind 127.0.0.1"\n'
            'ready_path = "/tokens.css"\nenv = { TARGET = "{node_url}" }\n'.replace(
                "PY", sys.executable
            )
        )
    return root


def test_up_runs_the_node_on_api_and_the_web_server_on_front(tmp_path, lanes, stubbed_node):
    root = _free_pair_checkout(tmp_path, lanes, with_web=True)
    ctx = Ctx(tmp_path / "state")
    result = up.run({"root": str(root), "email": "a@example.org", "sign_in": "none"}, ctx)
    try:
        assert result.ok, result.errors
        lane = lanes.get("site")
        assert stubbed_node[0].port == lane.api
        assert result.value["url"] == f"http://127.0.0.1:{lane.front}/tokens.css"
        assert result.value["web"]["up"] and result.value["api"] == lane.api
        # the node got the lane's own database, never the shared one
        assert stubbed_node[0].env["AXIOM_DB_URL"].endswith("/" + lane.database)
        assert "shared" not in stubbed_node[0].env["AXIOM_DB_URL"]
    finally:
        out = down.run({"root": str(root)}, ctx)
    assert out.value["web"]["stopped"]
    rec_gone = web.read_record(node.home_for(ctx.state_dir, "site"))
    assert rec_gone is None


def test_without_a_web_section_the_printed_url_is_the_nodes(tmp_path, lanes, stubbed_node):
    root = _free_pair_checkout(tmp_path, lanes, with_web=False)
    result = up.run(
        {"root": str(root), "email": "a@example.org", "sign_in": "none"}, Ctx(tmp_path / "s")
    )
    assert result.ok, result.errors
    assert result.value["url"] == f"http://127.0.0.1:{lanes.get('site').api}/"
    assert result.value["web"]["up"] is False


def test_status_json_reports_lane_ports_web_and_db(tmp_path, lanes, stubbed_node, monkeypatch):
    root = _free_pair_checkout(tmp_path, lanes, with_web=True)
    ctx = Ctx(tmp_path / "state")
    up.run({"root": str(root), "email": "a@example.org", "sign_in": "none"}, ctx)
    try:
        monkeypatch.setattr(node, "alive", lambda pid: True)
        monkeypatch.setattr(node, "answers", lambda url, timeout=2.0: True)
        monkeypatch.setattr(
            node, "read_record", lambda home: {"pid": 1, "url": "http://127.0.0.1:1/"}
        )
        v = status.run({"root": str(root)}, ctx).value
        lane = lanes.get("site")
        assert (v["front"], v["api"]) == (lane.front, lane.api)
        assert v["web"]["up"] and v["urls"]["front"].startswith(f"http://127.0.0.1:{lane.front}")
        assert v["db"]["name"] == lane.database and v["state"] == "up"
    finally:
        monkeypatch.undo()
        down.run({"root": str(root)}, ctx)


# -- hot-reload check --------------------------------------------------------


def test_check_proves_the_loop_and_restores_the_file(tmp_path, lanes, stubbed_node):
    root = _free_pair_checkout(tmp_path, lanes, with_web=True)
    ctx = Ctx(tmp_path / "state")
    before = (root / "ui" / "tokens.css").read_text()
    result = check.run({"root": str(root), "email": "a@example.org", "sign_in": "none"}, ctx)
    assert result.ok, result.errors
    assert result.value["seconds"] < 3
    assert (root / "ui" / "tokens.css").read_text() == before
    assert web.read_record(node.home_for(ctx.state_dir, "site")) is None  # stopped what it started


def test_check_fails_when_the_server_never_serves_the_change(
    tmp_path, lanes, stubbed_node, monkeypatch
):
    root = _free_pair_checkout(tmp_path, lanes, with_web=True)
    monkeypatch.setattr(check, "BUDGET_S", 0.5)

    def never(root_, cfg):  # a change the server will not show
        p = root_ / "ui" / "tokens.css"
        original = p.read_text()
        p.write_text(original + "/* edit */")
        return check.Override(p, "/tokens.css", "marker-that-is-not-there", original)

    result = check.run(
        {"root": str(root), "email": "a@example.org", "sign_in": "none", "override": never},
        Ctx(tmp_path / "state"),
    )
    assert not result.ok and "hot reload failed" in result.errors[0]
    assert "edit" not in (root / "ui" / "tokens.css").read_text()


def test_check_without_a_web_section_is_a_node_only_smoke(
    tmp_path, lanes, stubbed_node, monkeypatch
):
    from axiom.extensions.builtins.dev.skills import db as dbskill

    root = _free_pair_checkout(tmp_path, lanes, with_web=False)
    hits = []
    monkeypatch.setattr(check, "_get", lambda url: hits.append(url) or 200)
    monkeypatch.setattr(
        dbskill, "status", lambda p, c: SkillResult(value={"drift": []}, actions_taken=["ok"])
    )
    stubbed_node.clear()
    result = check.run(
        {"root": str(root), "email": "a@example.org", "sign_in": "none"}, Ctx(tmp_path / "s")
    )
    assert result.ok, result.errors
    assert [u.rsplit(":", 1)[1].split("/", 1)[1] for u in hits] == ["gate/login", "app/"]
    assert "no web server declared" in "\n".join(result.actions_taken)
    assert "component kit" in check.NO_WEB


def test_node_only_check_fails_when_a_page_does_not_answer_or_the_schema_drifted(
    tmp_path, lanes, stubbed_node, monkeypatch
):
    from axiom.extensions.builtins.dev.skills import db as dbskill

    root = _free_pair_checkout(tmp_path, lanes, with_web=False)
    monkeypatch.setattr(check, "_get", lambda url: 502 if "gate" in url else 200)
    drift = [{"extension": "x", "kind": "ahead", "cause": "newer branch"}]
    monkeypatch.setattr(dbskill, "status", lambda p, c: SkillResult(value={"drift": drift}))
    result = check.run(
        {"root": str(root), "email": "a@example.org", "sign_in": "none"}, Ctx(tmp_path / "s")
    )
    assert not result.ok
    said = "\n".join(result.errors)
    assert "/gate/login answered 502" in said and "x: ahead" in said


# -- schema drift ------------------------------------------------------------


def _r(cur, heads, revs):
    return {"x": {"current": cur, "heads": heads, "revisions": revs}}


def test_drift_is_classified_behind_ahead_diverged_and_current():
    assert schema.classify(_r("0002", ["0002"], ["0001", "0002"])) == []
    assert schema.classify(_r("", ["0001"], ["0001"]))[0].kind == "behind"
    assert schema.classify(_r("0001", ["0002"], ["0001", "0002"]))[0].kind == "behind"
    ahead = schema.classify(_r("0009_fake", ["0002"], ["0001", "0002"]))[0]
    assert ahead.kind == "ahead" and "0009_fake" in schema.describe(ahead)
    assert schema.classify(_r("0001", ["a", "b"], ["0001", "a", "b"]))[0].kind == "diverged"
    assert [d.kind for d in schema.blocking(schema.classify(_r("0009", ["0002"], ["0002"])))] == [
        "ahead"
    ]


def test_the_lane_dsn_swaps_only_the_database_name():
    dsn = schema.lane_dsn("postgresql://u:p@h:6543/prod", "axiom_dev_x")
    assert dsn.endswith("@h:6543/axiom_dev_x") and "prod" not in dsn
    with pytest.raises(schema.SchemaError):
        schema.lane_dsn("sqlite:///x.db", "axiom_dev_x")


def test_up_stops_on_a_database_ahead_and_names_it(tmp_path, lanes, stubbed_node, monkeypatch):
    def ahead(*a, **k):
        raise schema.SchemaError(
            "x: ahead: database 0009_fake, checkout 0002: ...\nrun `axi dev db reset --yes`"
        )

    monkeypatch.setattr(schema, "sync", ahead)
    root = _free_pair_checkout(tmp_path, lanes, with_web=True)
    result = up.run(
        {"root": str(root), "email": "a@example.org", "sign_in": "none"}, Ctx(tmp_path / "s")
    )
    assert (
        not result.ok
        and "0009_fake" in "\n".join(result.errors)
        and "db reset" in "\n".join(result.errors)
    )
    assert stubbed_node == []  # nothing was started


def test_reset_needs_yes_and_only_its_own_database(tmp_path, lanes, monkeypatch):
    root = _free_pair_checkout(tmp_path, lanes, with_web=False)
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u:p@h:5432/shared")
    ctx = Ctx(tmp_path / "s")
    no_yes = db.reset({"root": str(root)}, ctx)
    assert not no_yes.ok and "--yes" in no_yes.errors[0]
    other = db.reset({"root": str(root), "yes": True, "database": "shared"}, ctx)
    assert not other.ok and "only this lane's own database" in other.errors[0]
    with pytest.raises(schema.SchemaError):
        schema.reset_database("postgresql://u@h/shared", "axiom_dev_site")
    with pytest.raises(schema.SchemaError):
        schema.reset_database("postgresql://u@h/axiom_dev_site", "")


def test_seed_needs_a_declared_loader(tmp_path, lanes, monkeypatch):
    root = _free_pair_checkout(tmp_path, lanes, with_web=False)
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u:p@h:5432/shared")
    r = db.seed({"root": str(root)}, Ctx(tmp_path / "s"))
    assert not r.ok and "[dev] seed" in r.errors[0]


# -- lane doctor sees what a dead worktree left ------------------------------


def test_doctor_flags_a_dev_server_and_database_left_by_a_dead_worktree(tmp_path, lanes):
    root = _git_checkout(tmp_path / "gone")
    lane = resolve_lane({"root": str(root)})[0]
    state = tmp_path / "state"
    home = node.home_for(state, lane["name"])
    home.mkdir(parents=True)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (home / "web.json").write_text(json.dumps({"pid": sleeper.pid}))
        (home / "schema.json").write_text("{}")
        import shutil

        shutil.rmtree(root)
        found = doctor.check_dev_leftovers(lanes.all(), state)
        text = "\n".join(f.render() for f in found)
        assert f"pid {sleeper.pid}" in text and "is gone" in text
        assert lane["database"] in text
        assert doctor.check_dev_leftovers(lanes.all(), None) == []
    finally:
        sleeper.kill()
        sleeper.wait()


# -- the real thing, against a Postgres you point it at ----------------------

PG = os.environ.get("AXIOM_TEST_PG_SERVER", "")


@pytest.mark.skipif(
    not PG, reason="set AXIOM_TEST_PG_SERVER=postgresql://user:pw@host:port/postgres"
)
def test_schema_lifecycle_against_a_real_server(tmp_path):
    root = Path(__file__).resolve().parents[6]
    dsn = schema.lane_dsn(PG, "axiom_dev_pytest_lifecycle")
    schema.reset_database(dsn, "axiom_dev_pytest_lifecycle")
    try:
        roots = node.source_roots(root)
        out = schema.sync(root, roots, dsn, tmp_path)
        assert out["extensions"] and (tmp_path / "schema.json").exists()
        assert schema.blocking(schema.classify(schema.probe(root, roots, dsn))) == []
        from sqlalchemy import create_engine, text

        eng = create_engine(schema.lane_dsn(PG, "axiom_dev_pytest_lifecycle"))
        first = next(iter(out["extensions"]))
        with eng.begin() as c:
            c.execute(
                text(
                    f"UPDATE \"{first}\".alembic_version SET version_num = '9999_from_a_newer_branch'"
                )
            )
        eng.dispose()
        with pytest.raises(schema.SchemaError, match="ahead"):
            schema.sync(root, roots, dsn, tmp_path)
        schema.reset_database(dsn, "axiom_dev_pytest_lifecycle")
        schema.sync(root, roots, dsn, tmp_path)
    finally:
        schema.reset_database(dsn, "axiom_dev_pytest_lifecycle")
        from sqlalchemy import text

        eng = schema._admin(dsn)
        with eng.connect() as c:
            c.execute(text('DROP DATABASE IF EXISTS "axiom_dev_pytest_lifecycle"'))
