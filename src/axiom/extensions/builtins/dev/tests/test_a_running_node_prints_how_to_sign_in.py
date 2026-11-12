# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`dev up` on a node that is already running prints its address and how to sign in.

A contributor ran `dev up` a second time and got `command 'dev' failed: 'email'`:
the already-running answer carried no email, and printing it raised a KeyError.
"""

from __future__ import annotations

from axiom.extensions.builtins.dev import node
from axiom.extensions.builtins.dev.cli import _render
from axiom.infra.skills import SkillResult


def test_an_already_running_answer_without_an_email_still_prints() -> None:
    out = _render(
        "up",
        SkillResult(
            value={"url": "http://127.0.0.1:8830/app/", "already_running": True, "lane": "x"}
        ),
    )
    assert "http://127.0.0.1:8830/app/" in out
    assert "already running" in out


def test_the_already_running_answer_names_the_email_and_the_password_command(
    tmp_path, monkeypatch
) -> None:
    from axiom.extensions.builtins.dev.skills import up

    monkeypatch.setattr(
        up,
        "resolve_lane",
        lambda params: (
            {
                "name": "lane1",
                "front": 8829,
                "api": 8830,
                "database": "axiom_dev_lane1",
                "isolated": True,
                "root": str(tmp_path),
            },
            [],
        ),
    )
    monkeypatch.setattr(
        node, "read_record", lambda home: {"pid": 1, "url": "http://127.0.0.1:8830/app/"}
    )
    monkeypatch.setattr(node, "alive", lambda pid: True)
    monkeypatch.setattr(node, "answers", lambda url: True)

    class Ctx:
        state_dir = tmp_path

    result = up.run({"email": "person@example.org"}, Ctx())
    assert result.ok
    assert result.value["email"] == "person@example.org"
    assert result.value["password_secret"] == node.secret_name("lane1")
    out = _render("up", result)
    assert "person@example.org" in out
    assert node.secret_name("lane1") in out
