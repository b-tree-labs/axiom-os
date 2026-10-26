# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Dress rehearsal: where a data read goes for an install that does not hold the medallion.

The binding file is the one a site bind already writes. These tests do not
modify a real home directory. Decision tests use a stand-in face and do not
open the network. The live call runs only when ``AXI_REHEARSE_PUBLIC_FACE=1``
and ``AXI_PUBLIC_FACE`` names a face that answers from anywhere. That address
stays in the environment, not in this file.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform import read_target as rt
from axiom.extensions.builtins.data_platform.skills import gold
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult

#: A stand-in face. Not a customer, and not a host on a private network.
FACE = "https://face.example"


def _bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, site: str, face: str) -> None:
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    for name in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL", "AXIOM_KIT_DSN"):
        monkeypatch.delenv(name, raising=False)
    payload = {
        "current": site,
        "bindings": {
            site: {
                "site": site,
                "face_url": face,
                "token_ref": "",
                "bound_at": "2026-10-13T20:35:20Z",
                "note": "",
                "config_path": "",
            }
        },
    }
    (tmp_path / "site-bindings.json").write_text(json.dumps(payload), encoding="utf-8")


def _declare(tmp_path: Path, functions: list[str]) -> None:
    (tmp_path / "node.toml").write_text(
        "[node]\nfunctions = [" + ", ".join(f'"{name}"' for name in functions) + "]\n",
        encoding="utf-8",
    )


def test_a_bound_laptop_follows_the_binding(tmp_path, monkeypatch):
    """A laptop with no medallion follows its current binding."""
    _bind(tmp_path, monkeypatch, site="line-a", face=FACE)
    target = rt.resolve_read_target()
    assert target.where == "binding"
    assert target.site == "line-a"
    assert target.face_url == FACE


def test_a_host_node_reads_its_own_medallion_even_when_a_binding_exists(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, site="line-a", face=FACE)
    _declare(tmp_path, ["ingest", "data_platform", "front_end", "assistant"])
    target = rt.resolve_read_target()
    assert target.where == "medallion"
    assert target.reason == "this install holds the medallion"


def test_a_configured_database_is_the_medallion_before_any_function_list(tmp_path, monkeypatch):
    """A node already in service points at its database by URL. That stays."""
    _bind(tmp_path, monkeypatch, site="plant", face="https://plant.example")
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://medallion.internal/axiom")
    assert rt.resolve_read_target().where == "medallion"


def test_an_unbound_install_keeps_the_medallion_on_this_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    for name in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL"):
        monkeypatch.delenv(name, raising=False)
    target = rt.resolve_read_target()
    assert target.where == "medallion"
    assert "no binding" in target.reason


def test_a_sandbox_does_not_steal_the_data_read(tmp_path, monkeypatch):
    """kit-up's database is the sandbox. A data read does not fall into it."""
    _bind(tmp_path, monkeypatch, site="line-a", face="https://plant.example")
    monkeypatch.setenv("AXIOM_KIT_DSN", "postgresql://sandbox.internal/kit")
    target = rt.resolve_read_target()
    assert target.where == "binding"
    assert target.site == "line-a"


def test_an_archive_node_with_its_database_configured_reads_that_medallion(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, site="company", face="https://company.example")
    _declare(tmp_path, ["acquire", "transmit"])
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://archive.internal/axiom")
    assert rt.resolve_read_target().where == "medallion"


def test_a_bound_read_does_not_open_postgres(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, site="plant-box", face=FACE)

    class _Outcome:
        allowed = True
        budget = None

        def release(self):
            return None

    class _Guard:
        def admit(self, **kwargs):
            return _Outcome()

    def _refuse_postgres(*args, **kwargs):
        raise AssertionError("a bound laptop must not open postgres")

    monkeypatch.setattr(gold, "_guard", lambda: _Guard())
    monkeypatch.setattr(
        "psycopg2.connect",
        _refuse_postgres,
        raising=False,
    )
    monkeypatch.setattr(
        rt,
        "_post",
        lambda target, verb, params: {"ok": True, "value": {"verb": verb, "site": target.site}},
    )
    ctx = SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("dress-rehearsal"),
    )
    result = gold.tables({}, ctx)
    assert result.ok is True
    assert result.value["site"] == "plant-box"
    assert result.value["verb"] == "data.tables"


def test_an_unreachable_binding_names_the_node_and_not_localhost():
    target = rt.ReadTarget("binding", site="line-a", face_url=FACE)

    def _down(target, verb, params):
        raise rt.ReadError(
            rt._unreachable(target, "The node could not be reached (connection refused).")
        )

    import axiom.extensions.builtins.data_platform.read_target as module

    original = module._post
    module._post = _down
    try:
        result = rt.ask(target, verb="data.tables", params={})
    finally:
        module._post = original
    assert result.ok is False
    text = " ".join(result.errors)
    assert FACE in text
    assert "localhost" not in text
    assert isinstance(result, SkillResult)


@pytest.mark.skipif(
    os.environ.get("AXI_REHEARSE_PUBLIC_FACE") != "1" or not os.environ.get("AXI_PUBLIC_FACE"),
    reason="set AXI_REHEARSE_PUBLIC_FACE=1 and AXI_PUBLIC_FACE to call a face that answers from anywhere",
)
def test_a_partner_node_reaches_a_face_that_answers_from_anywhere(tmp_path, monkeypatch):
    """Live. A binding, no database URL, POST to the face named by the environment.

    The face is up and does not serve data reads. The result must say that,
    and must not mention a database on this machine.
    """
    face = os.environ["AXI_PUBLIC_FACE"].rstrip("/")
    _bind(tmp_path, monkeypatch, site="line-a", face=face)
    result = rt.ask(rt.resolve_read_target(), verb="data.tables", params={})
    text = " ".join(result.errors)
    assert result.ok is False
    assert "line-a" in text
    assert face in text
    assert "does not serve data reads yet" in text
    assert "localhost" not in text
    assert "5432" not in text
