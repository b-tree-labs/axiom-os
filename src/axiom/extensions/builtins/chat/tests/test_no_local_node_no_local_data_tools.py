# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node's own data tools are offered only where there is a node to read.

On a laptop with no node, chat still offered the ``data`` namespace. Every
call reached an empty or absent local database and answered "no data", and
the model reported that as a fact about the deployment the person asked
about. A tool that can only answer "nothing here" from this machine is
withheld here, unless the operator says otherwise (``chat.local_data_tools``:
``auto``, the default, ``always`` or ``never``).
"""

from __future__ import annotations

import socket

import pytest

from axiom.extensions.builtins.chat import tools
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec


def _ok(params, ctx):
    return SkillResult(ok=True)


@pytest.fixture
def registry(monkeypatch):
    r = SkillRegistry()
    for name in ("data.gold_tables", "press.draft"):
        r.register_skill(
            SkillSpec(name=name, fn=_ok, description=name, surfaces=("agent_tool",), side_effects=False)
        )
    monkeypatch.setattr(tools, "_skill_registry", lambda: r)
    monkeypatch.setattr(tools, "_skill_namespaces", lambda: ["data", "press"])
    return r


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def listening():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    yield s.getsockname()[1]
    s.close()


def _names():
    return set(tools._scan_skill_tools())


def test_no_node_database_no_data_tools(registry, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", f"postgresql://u:p@127.0.0.1:{_closed_port()}/db")
    names = _names()
    assert not any(n.startswith("data") for n in names)
    assert any(n.startswith("press") for n in names)


def test_a_reachable_node_database_offers_them(registry, monkeypatch, listening):
    monkeypatch.setenv("AXIOM_DB_URL", f"postgresql://u:p@127.0.0.1:{listening}/db")
    assert any(n.startswith("data") for n in _names())


@pytest.mark.parametrize(("setting", "offered"), [("always", True), ("never", False)])
def test_the_operator_setting_overrides_detection(registry, monkeypatch, listening, setting, offered):
    port = _closed_port() if offered else listening
    monkeypatch.setenv("AXIOM_DB_URL", f"postgresql://u:p@127.0.0.1:{port}/db")
    monkeypatch.setattr(tools, "_local_data_tools_setting", lambda: setting)
    assert any(n.startswith("data") for n in _names()) is offered
