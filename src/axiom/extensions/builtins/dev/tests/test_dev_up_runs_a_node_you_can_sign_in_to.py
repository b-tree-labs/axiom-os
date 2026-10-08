"""`dev up` starts a real node from this checkout that a person can sign in to.

No part of this is faked: a node process starts, the app is fetched, a
password sign-in is posted, and the node is stopped. Each step is one that a
hand-assembled local node got wrong at least once: an index page at `/`, a
sign-in that refused every password, a password printed into the terminal.
"""

from __future__ import annotations

import http.cookiejar
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from axiom.extensions.builtins.dev import node
from axiom.extensions.builtins.dev.skills import down, status, up
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.infra.skills import SkillContext
from axiom.webauth import load_user_records

CHECKOUT = Path(__file__).resolve().parents[6]

pytestmark = pytest.mark.slow


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.setenv("AXIOM_LANES_FILE", str(tmp_path / "lanes.json"))
    for key in [k for k in os.environ if k.startswith("AXIOM_GATE_OIDC_")]:
        monkeypatch.delenv(key)
    return SkillContext(
        registry=None, state_dir=tmp_path / "state", logger=logging.getLogger("test")
    )


def _params(**kw):
    return {"root": str(CHECKOUT), "email": "person@example.org", "sign_in": "none", **kw}


def test_up_serves_the_app_and_the_first_account_signs_in(ctx):
    started = up.run(_params(), ctx)
    try:
        assert started.ok, started.errors
        v = started.value
        base = f"http://127.0.0.1:{v['port']}"

        # The front door lands on an app the node actually mounts, never a 404,
        # and the URL printed is where it lands.
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        with opener.open(base + "/", timeout=10) as r:
            assert r.status == 200 and r.geturl() == v["url"]
            assert r.geturl() != base + "/"

        # The password went to the vault and nowhere else.
        assert "password" not in v
        vault = ForeignCredentialStore(ctx.state_dir)
        with vault.get(v["password_secret"]) as secret:
            password = secret.as_str()
        assert password not in repr(started)

        form = urllib.parse.urlencode(
            {
                "email": "person@example.org",
                "password": password,
                "next": v["url"].removeprefix(base),
            }
        ).encode()
        with opener.open(urllib.request.Request(base + "/gate/login", data=form), timeout=10) as r:
            assert r.status == 200 and r.geturl() == v["url"]

        accounts = load_user_records(node.home_for(ctx.state_dir, v["lane"]) / "users.json")
        assert [list(a["roles"]) for a in accounts] == [["owner"]]

        again = up.run(_params(), ctx)
        assert again.ok and again.value["already_running"] and again.value["url"] == v["url"]
        assert status.run(_params(), ctx).value["state"] == "up"
    finally:
        stopped = down.run(_params(), ctx)
    assert stopped.value["stopped"]
    assert status.run(_params(), ctx).value["state"] == "down"


def test_a_second_up_keeps_the_same_password(ctx):
    plan = node.build_plan(lane="x", root=CHECKOUT, port=1, state_dir=ctx.state_dir)
    vault = ForeignCredentialStore(ctx.state_dir)
    first = node.ensure_owner(plan, "person@example.org", vault)
    with vault.get(first["secret"]) as s:
        before = s.as_str()
    second = node.ensure_owner(plan, "person@example.org", vault)
    with vault.get(second["secret"]) as s:
        assert s.as_str() == before
    assert first["created"] and not second["created"]


def test_the_node_runs_this_checkouts_source(ctx):
    plan = node.build_plan(lane="x", root=CHECKOUT, port=1, state_dir=ctx.state_dir, base_env={})
    assert plan.env["PYTHONPATH"].split(os.pathsep)[0] == str(CHECKOUT / "src")
