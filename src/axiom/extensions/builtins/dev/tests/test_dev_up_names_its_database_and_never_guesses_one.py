# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`dev up` says which database it uses, and refuses to guess one (#1788).

A fresh contributor ran `dev up` in a terminal that had not loaded the
checkout's env file. With ``AXIOM_DB_URL`` unset the node fell back to the
platform default, ``localhost:5432``, and reported up: on a machine running
somebody else's stack it would have read and written their database. Nothing
said which database it was using, so nothing looked wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from axiom.extensions.builtins.dev import node
from axiom.extensions.builtins.dev.cli import _render
from axiom.extensions.builtins.dev.skills import up

from .conftest import Ctx


def _params(**kw: Any) -> dict[str, Any]:
    return {"email": "person@example.org", "sign_in": "none", **kw}


def test_without_a_database_url_dev_up_refuses_and_names_the_fix(harness, monkeypatch):
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    result = up.run(_params(), harness["ctx"])
    assert not result.ok
    said = "\n".join(result.errors)
    assert "AXIOM_DB_URL" in said
    assert "--db-url" in said
    assert "5432" in said  # says what it would otherwise have silently used
    assert harness["started"] == []  # no node, and so no database touched
    assert harness["vault"].values == {}  # no account made either


def test_the_refusal_points_at_the_checkouts_env_file_when_it_has_one(harness, monkeypatch):
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    (harness["root"] / ".envrc.local").write_text(
        "export AXIOM_DB_URL=postgresql://me:s3cretpw@localhost:5433/mine\n", encoding="utf-8"
    )
    result = up.run(_params(), harness["ctx"])
    assert not result.ok
    said = "\n".join(result.errors)
    assert ".envrc.local" in said
    assert "s3cretpw" not in said  # the file's password is never echoed


def test_dev_up_reports_the_database_it_uses_without_credentials(harness, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://axiom:hunter2@db.example:6543/axiom_lane1")
    result = up.run(_params(), harness["ctx"])
    assert result.ok, result.errors
    assert result.value["database"] == "db.example:6543/axiom_lane1"
    out = _render("up", result)
    assert "db.example:6543/axiom_lane1" in out
    assert "hunter2" not in out and "axiom:" not in out


def test_a_url_passed_to_dev_up_is_the_one_the_node_uses(harness, monkeypatch):
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    url = "postgresql://dev:s3cretpw@127.0.0.1:5544/scratch"
    result = up.run(_params(db_url=url), harness["ctx"])
    assert result.ok, result.errors
    [plan] = harness["started"]
    assert plan.env["AXIOM_DB_URL"] == url
    assert result.value["database"] == "127.0.0.1:5544/scratch"
    assert "s3cretpw" not in repr(result.value)


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("postgresql+psycopg2://u:p@h:1/d", "h:1/d"),
        ("postgresql://u@h/d", "h:5432/d"),
        ("postgresql://h.example/d?password=p", "h.example:5432/d"),
        ("not a url", "an unreadable AXIOM_DB_URL"),
    ],
)
def test_a_database_is_described_by_host_port_and_name_only(url, shown):
    assert node.describe_database(url) == shown


def test_an_already_running_node_still_says_which_database(tmp_path, monkeypatch):
    monkeypatch.setattr(
        up, "resolve_lane", lambda params: ({"name": "lane1", "root": str(tmp_path)}, [])
    )
    monkeypatch.setattr(
        node,
        "read_record",
        lambda home: {"pid": 1, "url": "http://127.0.0.1:8830/app/", "database": "h:1/d"},
    )
    monkeypatch.setattr(node, "alive", lambda pid: True)
    monkeypatch.setattr(node, "answers", lambda url: True)
    result = up.run({"email": "person@example.org"}, Ctx(tmp_path))
    assert "h:1/d" in _render("up", result)
